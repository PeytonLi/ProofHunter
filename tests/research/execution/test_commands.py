"""Integration lane: CLI wiring for run-pilot and evaluate (offline self-test doubles)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research import cli
from research.schemas.models import RawResultRow


def test_cli_resolves_integration_commands() -> None:
    assert cli.resolve("run-pilot") is not None
    assert cli.resolve("evaluate") is not None


def test_run_pilot_smoke_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run-pilot", "--doubles", "--smoke", "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "pilot-report.json").read_text())
    assert report["cases"] == 3
    assert report["seeds"] == report["seeds"][:1]
    assert report["skipped_arms"] == {"simple_heuristic": "no double for selector "
                                      "'simple_heuristic'"}
    assert report["projection"]["worst_case_executions_total"] == (
        report["projection"]["cases"] * report["projection"]["arms"] * 5 * 15)
    rows = (tmp_path / "rows.jsonl").read_text().splitlines()
    assert len(rows) == 3 * 3
    assert all(RawResultRow.model_validate_json(r) for r in rows)
    assert (tmp_path / "pilot-report.md").read_text().startswith("# R1 feasibility pilot")
    assert json.loads(capsys.readouterr().out)["out"] == str(tmp_path)


def test_pilot_refuses_test_split_and_bad_caps(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run-pilot", "--doubles", "--split", "test", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        cli.main(["run-pilot", "--doubles", "--cases", "51", "--out", str(tmp_path)])


def test_pilot_without_store_reports_missing_lane(tmp_path: Path) -> None:
    assert cli.main(["run-pilot", "--out", str(tmp_path)]) == 1


def test_evaluate_frozen_config(tmp_path: Path) -> None:
    config = tmp_path / "frozen.json"
    config.write_text(json.dumps({"arms": ["fixed_order", "builtin_eig", "builtin_ranker"]}))
    out = tmp_path / "eval"
    assert cli.main(["evaluate", "--doubles", "--config", str(config), "--max-cases", "2",
                     "--out", str(out)]) == 0
    manifest = json.loads((out / "run-manifest.json").read_text())
    assert manifest["arms_run"] == ["builtin_eig", "fixed_order"]
    assert set(manifest["arms_skipped"]) == {"builtin_ranker"}
    assert manifest["rows"] == 2 * 2 * 5
    assert manifest["training"] == "disabled"


def test_evaluate_test_split_runs_in_full(tmp_path: Path) -> None:
    config = tmp_path / "frozen.json"
    config.write_text("{}")
    with pytest.raises(SystemExit):
        cli.main(["evaluate", "--doubles", "--config", str(config), "--split", "test",
                  "--max-cases", "1", "--out", str(tmp_path)])


def test_evaluate_rejects_unknown_config_keys(tmp_path: Path) -> None:
    config = tmp_path / "frozen.json"
    config.write_text(json.dumps({"labels": "x"}))
    assert cli.main(["evaluate", "--doubles", "--config", str(config),
                     "--out", str(tmp_path)]) == 1
