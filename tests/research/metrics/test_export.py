from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy", reason="research lock only; run with --project research")
pytest.importorskip("jsonschema")

import jsonschema  # type: ignore[import-untyped]

from research.cli import main as cli
from research.metrics.export import (
    REFERENCES,
    SCHEMA_PATH,
    MetricsDocument,
    main,
    metrics_document,
    schema_json,
)
from research.metrics.ledger import read_ledger
from research.metrics.results import read_degraded, read_label_files, read_raw_results
from research.metrics.scoring import score
from research.metrics.synthetic import load_protocol, write_demo

PROTOCOL = load_protocol()
METRIC_KEYS = {"estimate", "ci_low", "ci_high", "n", "n_runs", "n_clusters", "ci"}


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return write_demo(tmp_path_factory.mktemp("demo"), regressions=12, clean=4, existing=2)


@pytest.fixture(scope="module")
def metrics(demo: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("out")
    argv = ["report", "--results", str(demo["results"]), "--labels", str(demo["labels"])]
    argv += ["--ledger", str(demo["ledger"]), "--degraded", str(demo["degraded"])]
    assert cli([*argv, "--out", str(out)]) == 0
    data: dict[str, Any] = json.loads((out / "metrics.json").read_text())
    return data


def _metrics(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        if set(node) == METRIC_KEYS:
            return [node]
        return [m for v in node.values() for m in _metrics(v)]
    return []


def test_committed_schema_is_current() -> None:
    assert SCHEMA_PATH.read_text() == schema_json()
    assert main(["--check"]) == 0


def test_metrics_json_validates_against_schema(metrics: dict[str, Any]) -> None:
    jsonschema.validate(metrics, json.loads(SCHEMA_PATH.read_text()))
    MetricsDocument.model_validate(metrics)
    assert metrics["schema_version"] == "research-metrics-v1"
    assert metrics["bootstrap"]["group_by"] == "family_id"
    assert metrics["bootstrap"]["resamples"] == 10000


def test_every_mode_and_split_is_present_and_unpooled(metrics: dict[str, Any]) -> None:
    assert set(metrics["modes"]) == {"offline", "recorded", "live"}
    for mode in metrics["modes"].values():
        assert {"validation", "test"} <= set(mode["splits"])
        assert mode["splits"]["validation"]["available"] is True
        assert mode["splits"]["test"] == {
            "available": False,
            "defect_cases": 0,
            "clean_cases": 0,
            "descriptive_only": True,
            "arms": {},
        }


def test_arm_metrics_have_estimate_ci_and_n(metrics: dict[str, Any]) -> None:
    arms = metrics["modes"]["live"]["splits"]["validation"]["arms"]
    arm = arms["frontier_agent"]
    assert set(arm["recall"]) == {"1", "3", "6"}
    for name in ("precision", "clean_fp_rate", "inconclusive_rate", "censored_misses"):
        assert set(arm[name]) == METRIC_KEYS
    assert set(arm["latency_ms"]) == {"p50", "p95"}
    assert set(arm["cost"]) == {"measured", "estimated", "unknown"}
    present = {ref for ref in REFERENCES if ref in arms}
    assert "builtin_eig" in present
    assert set(arm["paired"]) == present
    assert set(arms["builtin_eig"]["paired"]) == present - {"builtin_eig"}
    paired = arm["paired"]["builtin_eig"]
    assert set(paired["recall"]) == {"1", "3", "6"}
    assert paired["shared_episodes"] == arm["runs"]
    for m in _metrics(metrics):
        if m["estimate"] is None or m["ci"] == "none" or m["ci_low"] is None:
            continue
        assert m["n"] >= 1
        assert m["ci_low"] <= m["ci_high"]
    for m in _metrics(arm["recall"]):
        assert m["ci_low"] <= m["estimate"] <= m["ci_high"]
        assert m["n_clusters"] < m["n"]  # grouped by family, not by case


def test_values_match_score_and_costs_stay_separate(
    demo: dict[str, Path], metrics: dict[str, Any]
) -> None:
    rows = read_raw_results(demo["results"])
    labels = [lab for f in read_label_files([demo["labels"]]) for lab in f.labels]
    degraded = read_degraded(demo["degraded"])
    scores: Any = score(rows, labels, PROTOCOL, degraded)
    for mode in ("offline", "recorded", "live"):
        legacy = scores["modes"][mode]["validation"]["arms"]
        new = metrics["modes"][mode]["splits"]["validation"]["arms"]
        assert set(new) == set(legacy)
        for arm, s in legacy.items():
            for k in ("1", "3", "6"):
                expected = s["recall"][k]["grouped"]["estimate"]
                assert new[arm]["recall"][k]["estimate"] == pytest.approx(expected)
            assert new[arm]["degraded_rate"]["estimate"] == pytest.approx(s["degraded_rate"])
            assert new[arm]["latency_ms"]["p95"]["estimate"] == pytest.approx(s["wall_ms"]["p95"])
    recorded = metrics["modes"]["recorded"]["splits"]["validation"]["arms"]["frontier_agent"]
    live = metrics["modes"]["live"]["splits"]["validation"]["arms"]["frontier_agent"]
    assert recorded["cost"]["measured"]["runs"] == 0
    assert recorded["cost"]["estimated"]["runs"] == recorded["runs"]
    assert live["cost"]["estimated"]["runs"] == 0
    assert live["cost"]["measured"]["usd_per_case"]["estimate"] == pytest.approx(
        live["cost"]["measured"]["total_usd"] / live["cost"]["measured"]["runs"]
    )
    split = metrics["modes"]["recorded"]["splits"]["validation"]["arms"]
    degraded_arms = [a for a, m in split.items() if m["degraded_by_reason"]]
    assert degraded_arms
    assert all(a.startswith("frontier") for a in degraded_arms)
    ledger = read_ledger(demo["ledger"])
    components = metrics["modes"]["live"]["component_costs"]
    live_known = sum(e.cost_usd or 0.0 for e in ledger if e.measurement_mode.value == "live")
    assert sum(
        c["measured_usd"] + c["estimated_usd"] for c in components.values()
    ) == pytest.approx(live_known)


def test_rejects_stray_degraded_records(demo: dict[str, Path]) -> None:
    rows = read_raw_results(demo["results"])
    labels = [lab for f in read_label_files([demo["labels"]]) for lab in f.labels]
    with pytest.raises(ValueError, match="unknown run_ids"):
        metrics_document(rows, labels, PROTOCOL, degraded={"nope": "timeout"})
