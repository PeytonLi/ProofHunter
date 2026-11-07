from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("numpy", reason="research lock only; run with --project research")

from research.metrics.scoring import bootstrap_ci, paired_difference
from research.metrics.scoring import score as _score
from research.metrics.synthetic import load_protocol, make_labels
from research.schemas.models import CaseLabel, RawResultRow

PROTOCOL = load_protocol()


def score(*args: Any) -> Any:
    return _score(*args)


SEEDS = PROTOCOL.replicate_seeds[:2]


def _probe(step: int, outcome: str, confirmation: str = "not_run") -> dict[str, Any]:
    return {
        "step": step,
        "candidate_input_sha256": f"{step:064x}",
        "outcome": outcome,
        "confirmation": confirmation,
        "retries": 0,
    }


def episode(
    case: str,
    arm: str,
    seed: int,
    *,
    step: int | None = None,
    verdict: str | None = None,
    reason: str | None = None,
    mode: str = "offline",
    cost: float | None = 0.0,
    wall: float = 10.0,
    existing: bool = False,
) -> RawResultRow:
    if existing and step is not None:
        probes = [_probe(s, "match") for s in range(1, step)] + [_probe(step, "base_mismatch")]
        verdict, first = "confirmed_existing_defect", None
    elif step is not None:
        probes = [_probe(s, "match") for s in range(1, step)]
        probes.append(_probe(step, "mismatch", "reproduced"))
        verdict, first = verdict or "confirmed_regression", step
    else:
        probes = [_probe(s, "match") for s in range(1, 7)]
        first = None
        verdict = verdict or "no_defect_found"
    n = len(probes)
    confirm = int(any(p["confirmation"] != "not_run" for p in probes))
    data = {
        "schema_version": "research-raw-result-v1",
        "protocol_version": "research-protocol-v1",
        "run_id": f"{mode}-{arm}-{case}-{seed}",
        "implementation_sha": "1" * 40,
        "track": "track_a",
        "case_id": case,
        "split": "validation",
        "arm_id": arm,
        "seed": seed,
        "repetition": 0,
        "measurement_mode": mode,
        "snap_routing": "disabled",
        "pool_sha256": None,
        "models": dict.fromkeys(
            (
                "generator_model_id",
                "generator_revision",
                "adapter_sha256",
                "ranker_checkpoint_sha256",
                "frontier_model_id",
                "prompt_sha256",
            )
        ),
        "probes": probes,
        "invocations": {"base": n, "head": n, "confirm": confirm, "failed": 0},
        "verdict": verdict,
        "reason_code": reason or verdict,
        "pairs_to_first_confirmed_witness": first,
        "censored": first is None,
        "timings": {
            "wall_ms": wall,
            **dict.fromkeys(
                (
                    "queue_ms",
                    "generation_ms",
                    "feature_ms",
                    "ranking_ms",
                    "sandbox_startup_ms",
                    "execution_ms",
                    "confirmation_ms",
                    "cleanup_ms",
                    "cold_start",
                )
            ),
        },
        "tokens": dict.fromkeys(("input_tokens", "output_tokens", "reasoning_tokens")),
        "cost_usd": cost,
        "cost_status": "unknown" if cost is None else "measured",
    }
    return RawResultRow.model_validate_json(json.dumps(data))


@pytest.fixture
def labels() -> list[CaseLabel]:
    # syn-rg-0000..0003 (families per_line, half_even, ignored_discount, item_count),
    # syn-ex-0004 (aggregate_order, existing defect), syn-cl-0000..0001 clean.
    return make_labels(4, 2, 1)


def _arm(arm: str, steps: dict[str, int | None], **kw: Any) -> list[RawResultRow]:
    return [episode(c, arm, s, step=st, **kw) for c, st in steps.items() for s in SEEDS]


def test_recall_precision_fp_censoring(labels: list[CaseLabel]) -> None:
    rows = _arm(
        "builtin_eig",
        {
            "syn-rg-0000": 1,
            "syn-rg-0001": 3,
            "syn-rg-0002": 6,
            "syn-rg-0003": None,
            "syn-cl-0000": None,
        },
    )
    rows += [episode("syn-ex-0004", "builtin_eig", s, step=2, existing=True) for s in SEEDS]
    rows += [episode("syn-cl-0001", "builtin_eig", s, step=2) for s in SEEDS]  # false positive
    arm = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["arms"]["builtin_eig"]
    recall = {b: arm["recall"][b]["grouped"]["estimate"] for b in ("1", "3", "6")}
    assert recall == pytest.approx({"1": 1 / 5, "3": 3 / 5, "6": 4 / 5})
    assert arm["recall"]["6"]["grouped"]["n_clusters"] == 5
    assert arm["censored_defect_runs"] == 2
    assert arm["censored_at_pairs"] == 6
    assert arm["median_pairs_to_witness_detected_only"] == pytest.approx(2.5)
    assert arm["precision"] == pytest.approx(8 / 10)
    assert arm["clean_false_positive_runs"] == 2
    assert arm["clean_runs"] == 4
    assert arm["clean_fp_rate"]["estimate"] == pytest.approx(0.5)
    assert arm["confirmation_rate"] == 1.0
    assert arm["usd_per_case"] == 0.0


def test_wrong_kind_confirmation_is_not_success(labels: list[CaseLabel]) -> None:
    rows = [episode("syn-ex-0004", "builtin_eig", s, step=1) for s in SEEDS]
    arm = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["arms"]["builtin_eig"]
    assert arm["recall"]["6"]["case"]["estimate"] == 0.0
    assert arm["precision"] == 0.0


def test_inconclusive_reasons_and_unknown_cost(labels: list[CaseLabel]) -> None:
    rows = [
        episode(
            "syn-rg-0000",
            "frontier_agent",
            SEEDS[0],
            verdict="inconclusive",
            reason="missing_coverage",
            cost=None,
        ),
        episode(
            "syn-rg-0000",
            "frontier_agent",
            SEEDS[1],
            verdict="failed",
            reason="infrastructure_failure",
            cost=0.5,
        ),
    ]
    arm = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["arms"]["frontier_agent"]
    assert arm["inconclusive_by_reason"] == {
        "failed:infrastructure_failure": 1,
        "inconclusive:missing_coverage": 1,
    }
    assert arm["inconclusive_rate"] == 1.0
    assert arm["known_cost_usd"] == 0.5
    assert arm["unknown_cost_runs"] == 1
    assert arm["usd_per_case"] is None
    assert arm["usd_per_confirmed_defect"] is None


def test_modes_are_never_pooled(labels: list[CaseLabel]) -> None:
    offline = _arm("builtin_eig", {"syn-rg-0000": 1})
    live = _arm("builtin_eig", {"syn-rg-0000": None}, mode="live", cost=0.2, wall=900.0)
    scores = score(offline + live, labels, PROTOCOL)
    assert set(scores["modes"]) == {"offline", "live"}
    off = scores["modes"]["offline"]["validation"]["arms"]["builtin_eig"]
    on = scores["modes"]["live"]["validation"]["arms"]["builtin_eig"]
    assert off["recall"]["1"]["case"]["estimate"] == 1.0
    assert on["recall"]["1"]["case"]["estimate"] == 0.0
    assert on["wall_ms"]["p50"] == 900.0
    assert off["wall_ms"]["p50"] == 10.0
    with pytest.raises(ValueError, match="measurement modes"):
        paired_difference(offline, live, PROTOCOL)


def test_paired_difference_grouped_and_labelled(labels: list[CaseLabel]) -> None:
    eig = _arm(
        "builtin_eig",
        {"syn-rg-0000": 1, "syn-rg-0001": 2, "syn-rg-0002": None, "syn-cl-0000": None},
    )
    rnd = _arm(
        "uniform_random",
        {"syn-rg-0000": 4, "syn-rg-0001": 2, "syn-rg-0002": None, "syn-cl-0000": 3},
    )
    by_case = {lab.case_id: lab for lab in labels}
    diff = paired_difference(eig, rnd, PROTOCOL, labels=by_case)
    r1: Any = diff["recall_at_1"]
    assert r1["grouped"]["estimate"] == pytest.approx(1 / 3)
    assert r1["shared_episodes"] == 6
    fp: Any = diff["clean_fp_rate"]
    assert fp["case"]["estimate"] == -1.0
    unlabelled: Any = paired_difference(eig, rnd, PROTOCOL)["recall_at_6"]
    assert unlabelled["case"]["estimate"] == pytest.approx(-1 / 4)
    contrasts = score(eig + rnd, labels, PROTOCOL)["modes"]["offline"]["validation"]["paired"]
    assert [(p["a"], p["b"]) for p in contrasts] == [("builtin_eig", "uniform_random")]


def test_factorial_effects(labels: list[CaseLabel]) -> None:
    cases = ("syn-rg-0000", "syn-rg-0001")
    rows = []
    for arm, hit in (
        ("qwen_untuned_eig", None),
        ("qwen_untuned_ranker", None),
        ("qwen_tuned_eig", None),
        ("qwen_tuned_ranker", 1),
    ):
        rows += _arm(arm, dict.fromkeys(cases, hit))
    fact = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["factorial"]
    effects = fact["recall_at_1"]
    assert effects["generator_tuning"]["case"]["estimate"] == pytest.approx(0.5)
    assert effects["ranker_vs_eig"]["case"]["estimate"] == pytest.approx(0.5)
    assert effects["interaction"]["case"]["estimate"] == pytest.approx(1.0)
    assert effects["shared_episodes"] == 4


def test_bootstrap_is_deterministic_and_clustered() -> None:
    values = {"a": [1.0, 1.0, 1.0], "b": [0.0], "c": [0.0, 1.0]}
    first = bootstrap_ci(values, resamples=2000, seed=1)
    assert first == bootstrap_ci(values, resamples=2000, seed=1)
    assert first[0] == pytest.approx(4 / 6)
    assert first[1] <= first[0] <= first[2]
    assert bootstrap_ci({"x": [0.5, 0.5]}, resamples=100, seed=0) == (0.5, 0.5, 0.5)
    with pytest.raises(ValueError, match="non-empty"):
        bootstrap_ci({}, resamples=10, seed=0)


def test_rejects_unlabelled_unknown_arm_and_duplicate(labels: list[CaseLabel]) -> None:
    with pytest.raises(ValueError, match="no label"):
        score([episode("nope", "builtin_eig", 1)], labels, PROTOCOL)
    with pytest.raises(ValueError, match="not in the frozen protocol"):
        score([episode("syn-rg-0000", "made_up_arm", 1)], labels, PROTOCOL)
    row = episode("syn-rg-0000", "builtin_eig", 1)
    twin = RawResultRow.model_validate_json(
        row.model_dump_json().replace(row.run_id, row.run_id + "-b")
    )
    with pytest.raises(ValueError, match="duplicate episodes"):
        score([row, twin], labels, PROTOCOL)


def test_degraded_episodes_stay_in_arm_and_are_counted(labels: list[CaseLabel]) -> None:
    rows = _arm("frontier_reviewer", {"syn-rg-0000": 1, "syn-rg-0001": None}, cost=0.25)
    degraded = {rows[0].run_id: "timeout", rows[2].run_id: "timeout", rows[3].run_id: "empty"}
    plain = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["arms"]
    scores = score(rows, labels, PROTOCOL, degraded)
    arm = scores["modes"]["offline"]["validation"]["arms"]["frontier_reviewer"]
    base = plain["frontier_reviewer"]
    assert arm["runs"] == base["runs"] == 4
    assert arm["recall"] == base["recall"]
    assert arm["known_cost_usd"] == base["known_cost_usd"] == 1.0
    assert arm["degraded_runs"] == 3
    assert arm["degraded_rate"] == 0.75
    assert arm["degraded_by_reason"] == {
        "empty": {"runs": 1, "rate": 0.25},
        "timeout": {"runs": 2, "rate": 0.5},
    }
    assert arm["degraded_known_cost_usd"] == 0.75
    assert arm["degraded_confirmed_true_defect_runs"] == 1
    assert scores["degraded_records"] == 3
    assert base["degraded_runs"] == 0
    with pytest.raises(ValueError, match="unknown run_ids"):
        score(rows, labels, PROTOCOL, {"nope": "timeout"})


def test_estimated_costs_are_reported_separately(labels: list[CaseLabel]) -> None:
    rows = _arm("frontier_agent", {"syn-rg-0000": 1}, cost=0.5)
    data = rows[0].model_dump(mode="json")
    data["cost_status"] = "estimated"
    rows[0] = RawResultRow.model_validate_json(json.dumps(data))
    arm = score(rows, labels, PROTOCOL)["modes"]["offline"]["validation"]["arms"]["frontier_agent"]
    assert arm["known_cost_usd"] == 1.0
    assert arm["estimated_cost_usd"] == 0.5
    assert arm["estimated_cost_runs"] == 1
    assert arm["cost_status_runs"] == {"estimated": 1, "measured": 1}
