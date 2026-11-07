"""Verification-lane audits: each audit passes on frozen samples and catches a seeded defect."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from research.schemas.export import SCHEMA_DIR
from research.verification import budget, ledger, metrics, provenance, schema, splits, teardown
from research.verification.checkpoints import audit_checkpoint_hashes, check_deterministic
from research.verification.gates import render_markdown

SPEC = Path(__file__).resolve().parents[3] / "research" / "spec"


def sample(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMA_DIR / "samples" / f"{name}.json").read_text())
    return data


def protocol() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SPEC / "protocol-v1.json").read_text())
    return data


def policy() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SPEC / "spend-policy.json").read_text())
    return data


def codes(gate: Any) -> set[str]:
    return {f.code for f in gate.findings if f.severity == "error"}


def test_independent_oracle_matches_production_domain() -> None:
    assert provenance.independent_oracle([], 0) == 0
    assert provenance.independent_oracle([1], 50) == 1
    assert provenance.independent_oracle([3], 50) == 2
    assert provenance.independent_oracle([10000] * 8, 100) == 0
    for prices, d in (([1, 2, 3], 33), ([999, 1], 67), ([5], 10)):
        assert provenance.trusted_expected({"prices_cents": prices, "discount_percent": d}) >= 0
    with pytest.raises(ValueError, match="domain"):
        provenance.independent_oracle([1] * 9, 0)
    with pytest.raises(ValueError, match="domain"):
        provenance.independent_oracle([1], 101)
    with pytest.raises(ValueError, match="domain"):
        provenance.independent_oracle([True], 0)


@pytest.mark.parametrize("model", ["CaseManifest", "LabelFile", "RankingObservation",
                                   "GeneratorExample", "RawResultRow", "LedgerEntry",
                                   "SweSmithTask", "ProtocolV1", "SpendPolicy"])
def test_schema_samples_conform(model: str) -> None:
    assert schema.audit_rows([sample(model)], model).passed


def test_schema_violation_and_frozen_schemas() -> None:
    bad = sample("RawResultRow")
    bad["snap_routing"] = "enabled"
    assert "schema_violation" in codes(schema.audit_rows([bad], "RawResultRow"))
    assert schema.audit_frozen_schemas().passed
    assert schema.audit_file(SPEC / "protocol-v1.json", "ProtocolV1").passed


def _cases() -> list[dict[str, Any]]:
    base = sample("CaseRecord")
    out = []
    for i, split in enumerate(("train", "validation", "test")):
        c = copy.deepcopy(base)
        c.update(case_id=f"case-{split}-{i:04d}", split=split, family_id=f"fam-{i}",
                 defect_origin=f"origin-{i}", near_dup_cluster=f"cluster-{i}",
                 template_id=f"tmpl-{i}", params_sha256=f"{i:064x}",
                 base_source_sha256=f"{i + 10:064x}", head_source_sha256=f"{i + 20:064x}")
        out.append(c)
    return out


def test_split_audit_detects_group_and_near_duplicate_leakage() -> None:
    cases = _cases()
    srcs = {c["case_id"]: (b"def f(x):\n    return x\n",
                           f"def f(x):\n    return x + {i} * {i} - {i}\n".encode())
            for i, c in enumerate(cases)}
    ok = splits.audit_manifest_splits(cases, sources=srcs, bench_hashes=["f" * 64])
    assert ok.passed, ok.findings
    leaky = copy.deepcopy(cases)
    leaky[2]["family_id"] = leaky[0]["family_id"]
    leaky[2]["near_dup_cluster"] = leaky[1]["near_dup_cluster"]
    srcs[leaky[2]["case_id"]] = srcs[leaky[0]["case_id"]]
    bad = splits.audit_manifest_splits(leaky, sources=srcs, bench_hashes=[
        leaky[1]["head_source_sha256"]])
    assert {"family_id_overlap", "near_dup_cluster_overlap", "near_duplicate_change",
            "bench_source_overlap"} <= codes(bad)


def test_training_rows_and_artifacts_cannot_reach_test(tmp_path: Path) -> None:
    cases = _cases()
    rows = [{"case_id": cases[0]["case_id"], "split": "train"}]
    assert splits.audit_rows_against_manifest(rows, cases, name="obs").passed
    rows.append({"case_id": cases[2]["case_id"], "split": "test"})
    assert "forbidden_split_case" in codes(
        splits.audit_rows_against_manifest(rows, cases, name="obs"))
    art = tmp_path / "train.jsonl"
    art.write_text(json.dumps({"case_id": cases[0]["case_id"]}) + "\n")
    assert splits.audit_training_artifacts(cases, [tmp_path]).passed
    art.write_text(json.dumps({"note": cases[2]["head_source_sha256"]}) + "\n")
    assert "test_token_in_training_artifact" in codes(
        splits.audit_training_artifacts(cases, [tmp_path]))


def test_track_b_rows_stay_design_only() -> None:
    task = sample("SweSmithTask")
    assert splits.audit_swe_smith_splits([task]).passed
    task["executed"] = True
    assert "track_b_executed_or_labelled" in codes(splits.audit_swe_smith_splits([task]))


def test_label_and_observation_provenance() -> None:
    manifest, lf = sample("CaseManifest"), sample("LabelFile")
    files = {lf["split"]: lf}
    in_split = [c for c in manifest["cases"] if c["split"] == lf["split"]]
    gate = provenance.audit_label_files(in_split if len(in_split) == len(lf["labels"])
                                        else manifest["cases"], files,
                                        manifest["cases_sha256"])
    real = codes(gate) - {"unlabelled_case", "manifest_cases_sha256"}
    assert not real, gate.findings
    tampered = copy.deepcopy(lf)
    for lab in tampered["labels"]:
        if lab.get("witness"):
            lab["witness"]["expected_cents"] += 1
    assert "witness_expected" in codes(provenance.audit_label_files(
        manifest["cases"], {lf["split"]: tampered}, manifest["cases_sha256"]))
    obs = sample("RankingObservation")
    labels = {lab["case_id"]: lab for lab in lf["labels"]}
    labels.setdefault(obs["case_id"], {"kind": "introduced_regression"})
    assert provenance.audit_observations([obs], labels).passed
    bad = copy.deepcopy(obs)
    bad["features"]["observed_head"] = 1.0
    bad["relevance"] = "positive" if obs["relevance"] != "positive" else "zero"
    assert {"leaky_feature", "relevance_mismatch"} <= codes(
        provenance.audit_observations([bad], labels))


def test_budget_audit_on_sample_and_seeded_violations() -> None:
    row = sample("RawResultRow")
    gate = budget.audit_raw_results([row], protocol())
    assert gate.passed, gate.findings
    bad = copy.deepcopy(row)
    bad["seed"] = 7
    bad["invocations"]["confirm"] = 5
    bad["censored"] = not row["censored"]
    assert {"unregistered_seed", "confirm_count", "censoring"} <= codes(
        budget.audit_raw_results([row, bad], protocol()))


def test_metric_recomputation_round_trip() -> None:
    row = sample("RawResultRow")
    labels = {row["case_id"]: {"kind": "introduced_regression"}}
    cases = {row["case_id"]: {"family_id": "fam", "split": row["split"]}}
    comp = metrics.recompute([row], labels, cases, protocol(), bootstrap=False)
    key = f"{row['split']}/{row['measurement_mode']}/{row['arm_id']}/recall@6"
    assert key in comp
    assert metrics.compare_reported(comp, {key: comp[key]}).passed
    wrong = 0.5 if comp[key] != 0.5 else 0.25
    assert "metric_mismatch" in codes(metrics.compare_reported(comp, {key: wrong}))
    ci = metrics.group_bootstrap({"a": 1.0, "b": 0.0}, {}, resamples=200, seed=1)
    assert ci is not None
    assert ci[1] <= ci[0] <= ci[2]


def test_ledger_and_teardown() -> None:
    entry = sample("LedgerEntry")
    assert ledger.audit_ledger([entry], policy()).passed
    if entry["paid"] and entry.get("teardown_verified") is not True:
        assert not teardown.audit_ledger_teardown([entry], policy()).passed
    done = copy.deepcopy(entry)
    done.update(entry_id="e-teardown", paid=False, teardown_verified=True, cost_status="measured",
                cost_usd=0.0, estimated_job_usd=None, recorded_at="2099-01-01T00:00:00Z")
    assert teardown.audit_ledger_teardown([entry, done], policy()).passed
    paid = copy.deepcopy(entry)
    paid.update(entry_id="e-paid", paid=True, provider="runpod", resource_id="pod-1",
                estimated_job_usd=1.0, cost_status="measured", cost_usd=5.0,
                teardown_verified=False, pricing_reference="x", pricing_date="2026-11-07")
    assert "estimate_overrun" in codes(ledger.audit_ledger([paid], policy()))
    assert {"resource_not_torn_down", "no_final_listing"} <= codes(
        teardown.audit_ledger_teardown([paid], policy()))
    live = {"runpod": {"checked": True, "pods": [{"id": "p", "status": "RUNNING"}],
                       "network_volumes": [{"id": "v"}]},
            "fireworks": {"checked": False, "reason": "unset"}, "openai": {"checked": False}}
    assert {"runpod_pod_active", "runpod_volume"} <= codes(teardown.audit_live_listing(live))
    assert teardown.live_listing({})["runpod"]["checked"] is False


def test_checkpoint_hash_and_determinism(tmp_path: Path) -> None:
    (tmp_path / "ranker.txt").write_text("tree\n")
    from research.verification.artifacts import sha256_file

    h = sha256_file(tmp_path / "ranker.txt")
    assert audit_checkpoint_hashes({"ranker.txt": h}, tmp_path).passed
    bad = audit_checkpoint_hashes({"ranker.txt": "0" * 64}, tmp_path,
                                  [{"models": {"ranker_checkpoint_sha256": "1" * 64}}])
    assert {"checkpoint_hash_mismatch", "unverified_checkpoint_reference"} <= codes(bad)
    assert check_deterministic(lambda: 3, lambda m: m * 2, name="const").passed
    counter = iter(range(10))
    assert not check_deterministic(lambda: next(counter), lambda m: m, name="drift").passed
    assert "| Gate |" in render_markdown([bad]) or "Gate" in render_markdown([bad])
