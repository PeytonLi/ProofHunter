from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bench.arms import ARMS, ArmInput, run_arm
from bench.corpus import ROOT, load, oracle
from bench.harness import BenchRunner
from bench.holdout import validate
from bench.run import adversarial, enrolled, execute, main, prepare, seeds
from bench.scoring import bootstrap, paired, score, summary
from proofhunter.contracts import canonical_sha256
from proofhunter.ports import RunnerPort


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()
                if k not in {"wall_ms", "duration_ms"}}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def test_manifest_and_witnesses() -> None:
    manifest = load()
    assert len(manifest.cases) == 12
    assert sum(c.label == "clean" for c in manifest.cases) >= 3
    assert len(manifest.sample) >= 6
    for case in manifest.cases:
        if case.witness is not None:
            witness = case.witness
            assert oracle(witness.prices_cents, witness.discount_percent) == witness.expected
            module = ModuleType("witness")
            exec(compile((ROOT / case.head_path).read_bytes(), "witness", "exec"), module.__dict__)
            assert module.total_cents(witness.prices_cents,
                                      witness.discount_percent) == witness.head
        else:
            assert case.review_rationale


def test_manifest_drift(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(ROOT, root)
    case = load(root).cases[0]
    (root / case.head_path).write_text("tampered")
    with pytest.raises(ValueError, match="source hash drift"):
        load(root)
    shutil.copyfile(ROOT / case.head_path, root / case.head_path)
    data = json.loads((root / "manifest.json").read_text())
    data["version"] = "tampered"
    (root / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="manifest hash drift"):
        load(root)


async def test_runner_denies_unknown_and_protocol() -> None:
    manifest = load()
    inputs = await prepare(manifest, ROOT, "per_line", 17)
    runner = BenchRunner({}, inputs.enrollment)
    assert isinstance(runner, RunnerPort)
    row = await run_arm(inputs, "eig_deterministic", 17, runner)
    assert row["censored"]
    assert row["failures_by_reason"] == {"denied": 12}
    runner = BenchRunner(enrolled(manifest, ROOT), inputs.enrollment)
    row = await run_arm(inputs, "eig_deterministic", 17, runner)
    assert row["verdict"] == "confirmed_regression"
    assert row["confirm_invocations"] == 1
    assert runner.requests[-1].side.value == "confirm"
    assert len({r.attempt_id for r in runner.requests}) == len(runner.requests)


def test_labels_only_join_at_scoring() -> None:
    assert set(ArmInput.__dataclass_fields__) == {"base", "head", "enrollment", "plan", "bank"}
    source = inspect.getsource(run_arm)
    assert "witness" not in source.replace("pairs_to_first_confirmed_witness", "")
    assert "label" not in source
    assert "case" not in inspect.signature(run_arm).parameters
    row = {"censored": False}
    case = next(c for c in load().cases if c.label == "clean")
    assert score(row, case)["false_positive"] is True
    assert "label" not in row


async def test_same_budget_bank_and_determinism() -> None:
    first, _ = await execute("sample", 17, ROOT)
    second, _ = await execute("sample", 17, ROOT)
    assert normalize(first) == normalize(second)
    assert len(first) == 6 * 4 * 5
    assert set(r["arm"] for r in first) == set(ARMS)
    assert len(seeds(17)) == len(set(seeds(17))) >= 5
    for case in load().sample:
        rows = [r for r in first if r["case_id"] == case]
        assert len({r["bank_sha256"] for r in rows}) == 1
        assert all(r["budget"] == {"max_pairs": 6, "max_confirmations": 1} for r in rows)
        assert len({r["meta_plan_sha256"] for r in rows}) == 1
        assert all(r["base_invocations"] <= 6 and r["head_invocations"] <= 6 and
                   r["confirm_invocations"] <= 1 for r in rows)
    assert not any(r["false_positive"] for r in first)
    misses = [r for r in first if r["case_id"] == "item_count"]
    assert all(r["censored"] and r["pairs_to_first_confirmed_witness"] is None for r in misses)
    existing = [r for r in first if r["case_id"] == "existing" and not r["censored"]]
    assert existing
    assert all(r["verdict"] == "confirmed_existing_defect" for r in existing)


def test_bootstrap_and_accounting() -> None:
    assert bootstrap([0.0, 1.0, -1.0]) == bootstrap([0.0, 1.0, -1.0])
    assert bootstrap([1.0] * 3) == (1.0, 1.0, 1.0)
    assert bootstrap([]) == (0.0, 0.0, 0.0)
    mean, low, high = bootstrap([0.0, 1.0, -1.0])
    assert -1 <= low <= mean <= high <= 1
    rows = [{"case_id": "a", "label": "defective", "arm": arm,
             "pairs_to_first_confirmed_witness": first}
            for arm, first in (("eig_deterministic", 1), ("random", None))]
    assert paired(rows, "eig_deterministic", "random", 3) == (1.0, 1.0, 1.0)
    assert "Misses are right-censored" in summary([])


async def test_adversarial_honest_reasons() -> None:
    rows = {r["scenario"]: r for r in await adversarial(17)}
    assert rows["timeout_head"]["failures_by_reason"] == {"timeout": 6}
    assert rows["invalid_output"]["failures_by_reason"] == {"invalid": 6}
    assert rows["unknown_source"]["failures_by_reason"] == {"denied": 12}
    assert rows["flaky_confirmation"]["reason_code"] == "nondeterministic"
    assert rows["isolation_unverified"]["reason_code"] == "isolation_unverified"
    assert rows["isolation_unverified"]["base_invocations"] == 0
    assert all(r["verdict"] == "inconclusive" for r in rows.values())


def test_cli_valid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    assert main(["--offline", "--split", "sample", "--seed", "17", "--output", str(path)]) == 0
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 120
    assert path.with_suffix(".summary.md").is_file()
    assert path.with_suffix(".csv").is_file()
    assert path.with_suffix(".adversarial.jsonl").is_file()
    with pytest.raises(SystemExit):
        main(["--output", str(path)])


def test_holdout_overlap_refusal(tmp_path: Path) -> None:
    root = tmp_path / "holdout"
    shutil.copytree(ROOT, root)
    data = json.loads((root / "manifest.json").read_text())
    for case in data["cases"]:
        case["split"] = "holdout"
    data["manifest_sha256"] = canonical_sha256(
        {k: v for k, v in data.items() if k != "manifest_sha256"})
    (root / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="overlaps dev/sample"):
        validate(root)


def test_no_network_imports() -> None:
    for path in (ROOT.parent / "arms.py", ROOT.parent / "harness.py"):
        assert "httpx" not in path.read_text()
    inputs = asyncio.run(prepare(load(), ROOT, "per_line", 17))
    with pytest.raises(ValueError, match="unknown arm"):
        asyncio.run(run_arm(inputs, "unknown", 17, BenchRunner({}, inputs.enrollment)))
