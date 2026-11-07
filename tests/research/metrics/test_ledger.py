from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from research.metrics.ledger import (
    JsonlLedger,
    LedgerError,
    check_launch,
    make_entry,
    open_ledger,
    overrun,
    read_ledger,
    totals,
)
from research.metrics.synthetic import make_ledger_entries
from research.schemas import interfaces as I
from research.schemas.models import CostStatus, LedgerCategory, LedgerPhase, MeasurementMode


def _paid(**extra: Any) -> dict[str, Any]:
    return {
        "pricing_reference": "https://example.invalid/pricing",
        "pricing_date": "2026-11-07",
        "estimated_job_usd": 1.0,
        **extra,
    }


def test_open_ledger_implements_interface_and_round_trips(tmp_path: Path) -> None:
    ledger = open_ledger(tmp_path / "ledger.jsonl")
    assert isinstance(ledger, I.Ledger)
    for entry in make_ledger_entries(MeasurementMode.LIVE):
        ledger.append(entry)
    assert [e.entry_id for e in ledger.entries()] == [
        e.entry_id for e in make_ledger_entries(MeasurementMode.LIVE)
    ]
    assert read_ledger(tmp_path / "ledger.jsonl") == list(ledger.entries())


def test_duplicate_and_invalid_entries_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = JsonlLedger(path)
    entry = make_ledger_entries(MeasurementMode.OFFLINE)[0]
    ledger.append(entry)
    with pytest.raises(LedgerError, match="duplicate"):
        ledger.append(entry)
    data = json.loads(entry.model_dump_json())
    data.update(entry_id="x-1", cost_usd=None)  # measured status with null cost
    path.write_text(path.read_text() + json.dumps(data) + "\n")
    with pytest.raises(LedgerError, match=":2: invalid"):
        read_ledger(path)


def test_make_entry_enforces_paid_rules() -> None:
    common: dict[str, Any] = {
        "lane": "measurement",
        "provider": "openai",
        "category": LedgerCategory.MODEL_INFERENCE,
        "phase": LedgerPhase.EVALUATION,
        "measurement_mode": MeasurementMode.LIVE,
        "quantity": 10.0,
        "unit": "token",
    }
    with pytest.raises(ValidationError, match="pricing reference"):
        make_entry(paid=True, cost_usd=0.1, cost_status=CostStatus.MEASURED, **common)
    with pytest.raises(ValidationError, match="checkin"):
        make_entry(
            paid=True,
            cost_usd=0.1,
            cost_status=CostStatus.MEASURED,
            **common,
            **_paid(estimated_job_usd=60.0),
        )
    with pytest.raises(ValidationError, match="teardown"):
        make_entry(
            paid=True,
            cost_usd=0.1,
            cost_status=CostStatus.MEASURED,
            **common,
            **_paid(resource_id="pod-1"),
        )
    with pytest.raises(LedgerError, match="unknown ledger fields"):
        make_entry(paid=False, cost_usd=None, cost_status=CostStatus.UNKNOWN, bogus=1, **common)
    ok = make_entry(paid=True, cost_usd=None, cost_status=CostStatus.UNKNOWN, **common, **_paid())
    assert ok.recorded_at.utcoffset() is not None
    assert ok.cost_usd is None


def test_launch_guard_and_overrun() -> None:
    check_launch(50.0, None)
    with pytest.raises(LedgerError, match="check-in"):
        check_launch(50.01, None)
    check_launch(80.0, "coord-123")
    entries = make_ledger_entries(MeasurementMode.LIVE)
    assert not overrun(entries, "pod-live", 0.6)  # 0.66 <= 0.6 * 1.25
    assert overrun(entries, "pod-live", 0.5)


def test_totals_keep_modes_and_unknowns_separate() -> None:
    entries = [e for m in MeasurementMode for e in make_ledger_entries(m)]
    rows = totals(entries)
    assert {r["measurement_mode"] for r in rows} == {"offline", "recorded", "live"}
    storage = [r for r in rows if r["category"] == "storage_idle"]
    assert len(storage) == 3
    assert all(r["unknown_cost_entries"] == 1 and r["measured_usd"] == 0.0 for r in storage)
    gpu = next(
        r for r in rows if r["category"] == "training_gpu" and r["measurement_mode"] == "live"
    )
    assert gpu["estimated_usd"] == pytest.approx(0.66)
    assert gpu["measured_usd"] == 0.0
    assert not any(
        r["category"] == "training_gpu" and r["measurement_mode"] == "offline" for r in rows
    )
