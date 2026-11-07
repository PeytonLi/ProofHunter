"""JSONL cost ledger of frozen ``LedgerEntry`` records (validated on write and read)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from research.schemas.models import (
    CostStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerPhase,
    MeasurementMode,
)

DEFAULT_LEDGER: Final = Path("research/artifacts/generator/ledger.jsonl")


class JsonlLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER) -> None:
        self.path = path

    def append(self, entry: LedgerEntry) -> None:
        LedgerEntry.model_validate(entry.model_dump())
        ids = {e.entry_id for e in self.entries()}
        if entry.entry_id in ids:
            raise ValueError(f"duplicate ledger entry {entry.entry_id}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def entries(self) -> Sequence[LedgerEntry]:
        if not self.path.exists():
            return []
        return [LedgerEntry.model_validate_json(line)
                for line in self.path.read_text().splitlines() if line.strip()]


def utcnow() -> datetime:
    return datetime.now(UTC)


def gpu_entry(*, entry_id: str, provider: str, resource_id: str | None, run_id: str | None,
              hours: float, unit_price: float | None, estimated_job_usd: float,
              pricing_reference: str, phase: str, wall_ms: float | None,
              teardown_verified: bool | None, cost_status: CostStatus = CostStatus.ESTIMATED,
              category: str = "training_gpu", **extra: Any) -> LedgerEntry:
    cost = None if cost_status is CostStatus.UNKNOWN or unit_price is None else round(
        hours * unit_price, 6)
    status = CostStatus.UNKNOWN if cost is None else cost_status
    now = utcnow()
    return LedgerEntry.model_validate({
        "schema_version": "research-ledger-v1", "entry_id": entry_id, "recorded_at": now,
        "lane": "generator", "provider": provider, "category": LedgerCategory(category),
        "phase": LedgerPhase(phase),
        "resource_id": resource_id, "run_id": run_id, "case_id": None,
        "quantity": round(hours, 6), "unit": "gpu_hour", "unit_price_usd": unit_price,
        "cost_usd": cost, "cost_status": status, "pricing_reference": pricing_reference,
        "pricing_date": now.date().isoformat(), "wall_ms": wall_ms, "queue_ms": None,
        "measurement_mode": MeasurementMode.LIVE, "paid": True,
        "estimated_job_usd": estimated_job_usd, "coordinator_checkin_ref": None,
        "teardown_verified": teardown_verified, **extra})
