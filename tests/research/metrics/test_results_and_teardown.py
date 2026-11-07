from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from research.cli import main as cli
from research.metrics.ledger import JsonlLedger
from research.metrics.results import (
    ResultsError,
    read_label_files,
    read_raw_results,
    write_raw_results,
)
from research.metrics.synthetic import (
    label_file,
    load_protocol,
    make_labels,
    make_ledger_entries,
    make_rows,
)
from research.metrics.teardown import verify_teardown
from research.schemas.models import LedgerEntry, MeasurementMode, SpendPolicy, Split

POLICY = Path(__file__).resolve().parents[3] / "research" / "spec" / "spend-policy.json"


def _policy() -> SpendPolicy:
    return SpendPolicy.model_validate_json(POLICY.read_text())


def test_raw_results_round_trip_jsonl_and_csv(tmp_path: Path) -> None:
    rows = make_rows(make_labels(3, 2, 1), load_protocol(), mode=MeasurementMode.OFFLINE)
    jsonl, csv_path = write_raw_results(tmp_path / "r.jsonl", rows)
    assert read_raw_results(jsonl) == rows
    with csv_path.open() as stream:
        table = list(csv.DictReader(stream))
    assert len(table) == len(rows)
    assert {"timings.wall_ms", "invocations.base", "probes", "measurement_mode"} <= set(table[0])
    with pytest.raises(ResultsError, match="duplicate run_id"):
        write_raw_results(tmp_path / "d.jsonl", [rows[0], rows[0]])
    data = json.loads(rows[0].model_dump_json())
    data["censored"] = not data["censored"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(data) + "\n")
    with pytest.raises(ResultsError, match="invalid result row"):
        read_raw_results(bad)


def test_sealed_labels_need_explicit_unseal(tmp_path: Path) -> None:
    path = tmp_path / "test.json"
    path.write_text(label_file(make_labels(1, 1), Split.TEST).model_dump_json())
    with pytest.raises(ResultsError, match="sealed"):
        read_label_files([path])
    assert read_label_files([path], allow_sealed=True)[0].sealed


def _resource(entry_id: str, verified: bool, when: str) -> LedgerEntry:
    data = json.loads(make_ledger_entries(MeasurementMode.LIVE)[2].model_dump_json())
    data.update(entry_id=entry_id, teardown_verified=verified, recorded_at=when)
    return LedgerEntry.model_validate_json(json.dumps(data))


def test_verify_teardown_flags_unverified_and_unguarded(tmp_path: Path) -> None:
    ledger = JsonlLedger(tmp_path / "l.jsonl")
    ledger.append(_resource("a", False, "2026-11-07T23:00:00Z"))
    assert [e.entry_id for e in verify_teardown(_policy(), ledger)] == ["a"]
    ledger.append(_resource("b", True, "2026-11-07T23:10:00Z"))
    assert verify_teardown(_policy(), ledger) == []
    data = json.loads(make_ledger_entries(MeasurementMode.LIVE)[3].model_dump_json())
    data.update(entry_id="c", provider="unguarded_cloud")
    ledger.append(LedgerEntry.model_validate_json(json.dumps(data)))
    assert [e.entry_id for e in verify_teardown(_policy(), ledger)] == ["c"]


def test_verify_teardown_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "l.jsonl"
    JsonlLedger(path).append(_resource("a", True, "2026-11-07T23:00:00Z"))
    assert cli(["verify-teardown", "--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    other = tmp_path / "m.jsonl"
    data = json.loads(_resource("q", False, "2026-11-07T23:30:00Z").model_dump_json())
    data["resource_id"] = "pod-other"
    JsonlLedger(other).append(LedgerEntry.model_validate_json(json.dumps(data)))
    assert cli(["verify-teardown", "--ledger", str(path), str(other)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [u["resource_id"] for u in out["unverified"]] == ["pod-other"]
    assert cli(["verify-teardown", "--ledger", str(tmp_path / "missing.jsonl")]) == 2
