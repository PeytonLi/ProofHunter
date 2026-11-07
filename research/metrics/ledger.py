"""Append-only JSONL cost/timing ledger of frozen ``LedgerEntry`` rows (``I.Ledger``).

Entries are validated on write and on read. Unknown costs stay ``None``; they are never
summed as zero. Totals keep measurement modes, categories and phases separate.
"""

from __future__ import annotations

import os
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research.schemas import interfaces as I
from research.schemas.models import (
    SINGLE_JOB_CHECKIN_USD,
    CostStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerPhase,
    MeasurementMode,
)

OVERRUN_TOLERANCE = 0.25


class LedgerError(ValueError):
    pass


class JsonlLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> Sequence[LedgerEntry]:
        return read_ledger(self.path) if self.path.exists() else []

    def append(self, entry: LedgerEntry) -> None:
        checked = LedgerEntry.model_validate_json(entry.model_dump_json())
        if any(e.entry_id == checked.entry_id for e in self.entries()):
            raise LedgerError(f"duplicate ledger entry_id {checked.entry_id!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(checked.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def open_ledger(path: Path) -> I.Ledger:
    return JsonlLedger(path)


def read_ledger(path: Path) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = LedgerEntry.model_validate_json(line)
        except ValueError as error:
            raise LedgerError(f"{path}:{number}: invalid ledger entry: {error}") from error
        if entry.entry_id in seen:
            raise LedgerError(f"{path}:{number}: duplicate entry_id {entry.entry_id!r}")
        seen.add(entry.entry_id)
        entries.append(entry)
    return entries


def make_entry(
    *,
    lane: str,
    provider: str,
    category: LedgerCategory,
    phase: LedgerPhase,
    measurement_mode: MeasurementMode,
    paid: bool,
    quantity: float,
    unit: str,
    cost_usd: float | None,
    cost_status: CostStatus,
    **optional: Any,
) -> LedgerEntry:
    """Build and validate an entry, filling ids, UTC timestamp and absent optional fields."""
    data: dict[str, Any] = {
        "schema_version": "research-ledger-v1",
        "entry_id": f"ledger-{uuid.uuid4().hex}",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "lane": lane,
        "provider": provider,
        "category": category.value,
        "phase": phase.value,
        "resource_id": None,
        "run_id": None,
        "case_id": None,
        "quantity": float(quantity),
        "unit": unit,
        "unit_price_usd": None,
        "cost_usd": cost_usd,
        "cost_status": cost_status.value,
        "pricing_reference": None,
        "pricing_date": None,
        "wall_ms": None,
        "queue_ms": None,
        "measurement_mode": measurement_mode.value,
        "paid": paid,
        "estimated_job_usd": None,
        "coordinator_checkin_ref": None,
        "teardown_verified": None,
    }
    unknown = set(optional) - set(data)
    if unknown:
        raise LedgerError(f"unknown ledger fields {sorted(unknown)}")
    data.update(optional)
    return LedgerEntry.model_validate(_jsonable(data), strict=False)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v.value if hasattr(v, "value") else v for k, v in data.items()}


def check_launch(estimated_job_usd: float, coordinator_checkin_ref: str | None) -> None:
    """Pre-launch guard: jobs above the frozen check-in threshold need a check-in reference."""
    if estimated_job_usd < 0:
        raise LedgerError("estimate must be non-negative")
    if estimated_job_usd > SINGLE_JOB_CHECKIN_USD and coordinator_checkin_ref is None:
        raise LedgerError(
            f"job estimated at {estimated_job_usd:.2f} USD exceeds the "
            f"{SINGLE_JOB_CHECKIN_USD:.0f} USD check-in threshold without a check-in reference"
        )


def job_spend(entries: Iterable[LedgerEntry], resource_id: str) -> tuple[float, int]:
    """(known USD, entries with unknown cost) for one job/resource."""
    items = [e for e in entries if e.resource_id == resource_id]
    return (
        sum(e.cost_usd or 0.0 for e in items),
        sum(e.cost_status is CostStatus.UNKNOWN for e in items),
    )


def overrun(
    entries: Iterable[LedgerEntry],
    resource_id: str,
    estimate_usd: float,
    tolerance: float = OVERRUN_TOLERANCE,
) -> bool:
    """Spend-policy hard stop: known spend exceeds the job estimate by more than ``tolerance``."""
    known, _ = job_spend(entries, resource_id)
    return known > estimate_usd * (1 + tolerance) + 1e-12


def totals(entries: Iterable[LedgerEntry]) -> list[dict[str, Any]]:
    """Totals keyed by (measurement_mode, category, phase); modes are never pooled."""
    grouped: dict[tuple[str, str, str], list[LedgerEntry]] = defaultdict(list)
    for e in entries:
        grouped[(e.measurement_mode.value, e.category.value, e.phase.value)].append(e)
    out = []
    for (mode, category, phase), items in sorted(grouped.items()):
        out.append(
            {
                "measurement_mode": mode,
                "category": category,
                "phase": phase,
                "entries": len(items),
                "paid_entries": sum(e.paid for e in items),
                "measured_usd": sum(
                    e.cost_usd or 0.0 for e in items if e.cost_status is CostStatus.MEASURED
                ),
                "estimated_usd": sum(
                    e.cost_usd or 0.0 for e in items if e.cost_status is CostStatus.ESTIMATED
                ),
                "unknown_cost_entries": sum(e.cost_status is CostStatus.UNKNOWN for e in items),
                "wall_ms": sum(e.wall_ms or 0.0 for e in items),
                "queue_ms": sum(e.queue_ms or 0.0 for e in items),
                "providers": sorted({e.provider for e in items}),
            }
        )
    return out
