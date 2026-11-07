"""Frozen research schema checks (setup lane)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from proofhunter.contracts import canonical_sha256
from research.schemas.export import SCHEMA_DIR, render
from research.schemas.export import main as export_main
from research.schemas.models import SCHEMA_MODELS

SAMPLES = SCHEMA_DIR / "samples"


def sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SAMPLES / f"{name}.json").read_text())
    return data


def check(model: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    return model.model_validate_json(json.dumps(data))


def reject(model: type[BaseModel], data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        check(model, data)


@pytest.mark.parametrize("name", sorted(SCHEMA_MODELS))
def test_schema_file_is_frozen(name: str) -> None:
    assert (SCHEMA_DIR / f"{name}.schema.json").read_text() == render(name)


def test_export_check_passes() -> None:
    assert export_main(["--check"]) == 0


@pytest.mark.parametrize("name", sorted(SCHEMA_MODELS))
def test_sample_validates_with_pydantic(name: str) -> None:
    check(SCHEMA_MODELS[name], sample(name))


@pytest.mark.parametrize("name", sorted(SCHEMA_MODELS))
def test_sample_validates_with_json_schema(name: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(sample(name))


def test_extra_fields_rejected() -> None:
    data = sample("RawResultRow") | {"label": "defective"}
    reject(SCHEMA_MODELS["RawResultRow"], data, "Extra inputs")


def test_manifest_rejects_family_split_leakage() -> None:
    model = SCHEMA_MODELS["CaseManifest"]
    data = sample("CaseManifest")
    twin = copy.deepcopy(data["cases"][0]) | {
        "case_id": "a-per_line-0002",
        "split": "test",
        "head_source_sha256": "9" * 64,
    }
    data["cases"].append(twin)
    data["cases_sha256"] = canonical_sha256(data["cases"])
    reject(model, data, "more than one split")


def test_manifest_hash_binds_cases() -> None:
    data = sample("CaseManifest")
    data["cases"][0]["candidate_seed"] = 1
    reject(SCHEMA_MODELS["CaseManifest"], data, "cases_sha256")


def test_case_params_hash_binds_params() -> None:
    data = sample("CaseRecord")
    data["params"]["round_at"] = "total"
    reject(SCHEMA_MODELS["CaseRecord"], data, "params_sha256")


def test_test_split_labels_are_sealed() -> None:
    data = sample("LabelFile") | {"split": "test"}
    reject(SCHEMA_MODELS["LabelFile"], data, "sealed")


def test_label_witness_rules() -> None:
    model = SCHEMA_MODELS["CaseLabel"]
    data = sample("CaseLabel")
    bad = copy.deepcopy(data)
    bad["witness"]["base_cents"] = 42
    reject(model, bad, "introduced regression")
    clean = data | {"kind": "clean", "expected_verdict": "no_defect_found"}
    reject(model, clean, "clean controls")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"outcome": "match"}, "outcome inconsistent"),
        ({"confirmation": "not_reproduced"}, "relevance must be unknown"),
        ({"relevance": "zero"}, "relevance must be positive"),
        ({"exploration": True, "selection_probability": None}, "selection_probability"),
        ({"split": "test"}, "split"),
        ({"remaining_pairs_before": 5}, "remaining_pairs_before"),
    ],
)
def test_observation_label_rules(change: dict[str, Any], match: str) -> None:
    reject(SCHEMA_MODELS["RankingObservation"], sample("RankingObservation") | change, match)


def test_observation_zero_and_unknown() -> None:
    model = SCHEMA_MODELS["RankingObservation"]
    match = sample("RankingObservation") | {
        "head_cents": 41,
        "outcome": "match",
        "confirmation": "not_run",
        "relevance": "zero",
    }
    check(model, match)
    unusable = match | {"outcome": "unusable", "unusable_reason": "timeout", "relevance": "unknown"}
    check(model, unusable)
    reject(model, unusable | {"relevance": "zero"}, "relevance must be unknown")


@pytest.mark.parametrize(
    "feature", ["label", "outcome_prev", "expected_cents", "family_id", "witness_hit", "head_value"]
)
def test_observation_rejects_leaky_features(feature: str) -> None:
    data = sample("RankingObservation")
    data["features"] = data["features"] | {feature: 1}
    reject(SCHEMA_MODELS["RankingObservation"], data, "leaks")


def test_generator_example_rules() -> None:
    model = SCHEMA_MODELS["GeneratorExample"]
    reject(model, sample("GeneratorExample") | {"contains_fix_patch": True}, "contains_fix_patch")
    reject(model, sample("GeneratorExample") | {"split": "test"}, "split")
    data = sample("GeneratorExample")
    data["target"][0]["grounding_witness_sha256"] = None
    reject(model, data, "grounded")


def test_raw_result_censoring_and_budget() -> None:
    model = SCHEMA_MODELS["RawResultRow"]
    reject(model, sample("RawResultRow") | {"censored": True}, "censored")
    reject(model, sample("RawResultRow") | {"snap_routing": "enabled"}, "snap_routing")
    data = sample("RawResultRow")
    data["invocations"] = {"base": 8, "head": 7, "confirm": 1, "failed": 0}
    reject(model, data, "episode bound")
    miss = sample("RawResultRow")
    miss["probes"][0] |= {"outcome": "match", "confirmation": "not_run"}
    miss |= {
        "pairs_to_first_confirmed_witness": None,
        "censored": True,
        "verdict": "inconclusive",
        "reason_code": "insufficient_coverage",
    }
    check(model, miss)


def test_ledger_paid_guards() -> None:
    model = SCHEMA_MODELS["LedgerEntry"]
    reject(model, sample("LedgerEntry") | {"estimated_job_usd": 80.0}, "check-in")
    check(
        model,
        sample("LedgerEntry")
        | {"estimated_job_usd": 80.0, "coordinator_checkin_ref": "coord-msg-1"},
    )
    reject(model, sample("LedgerEntry") | {"teardown_verified": None}, "teardown")
    reject(model, sample("LedgerEntry") | {"pricing_reference": None}, "pricing")
    reject(model, sample("LedgerEntry") | {"recorded_at": "2026-11-07T14:30:00-08:00"}, "UTC")


def test_track_b_cannot_be_marked_executed() -> None:
    model = SCHEMA_MODELS["SweSmithTask"]
    reject(model, sample("SweSmithTask") | {"executed": True}, "executed")
    reject(model, sample("SweSmithTask") | {"execution_status": "ready"}, "execution_status")


def test_spend_policy_uncapped_with_guards() -> None:
    model = SCHEMA_MODELS["SpendPolicy"]
    reject(model, sample("SpendPolicy") | {"total_cap_usd": 50}, "total_cap_usd")
    reject(model, sample("SpendPolicy") | {"single_job_checkin_usd": 100.0}, "single_job")
