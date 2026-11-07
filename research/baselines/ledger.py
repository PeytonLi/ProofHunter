"""Ledger helpers for paid baseline calls (``research.schemas.models.LedgerEntry``)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from research.schemas.models import (
    CostStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerPhase,
    MeasurementMode,
)


class JsonlLedger:
    """Append-only JSONL ``I.Ledger``; validates on write and read. Prefer
    ``research.metrics.ledger.open_ledger`` once the measurement lane lands."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry: LedgerEntry) -> None:
        line = LedgerEntry.model_validate_json(entry.model_dump_json()).model_dump_json()
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def entries(self) -> Sequence[LedgerEntry]:
        if not self.path.exists():
            return []
        return [LedgerEntry.model_validate_json(line)
                for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class MemoryLedger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def append(self, entry: LedgerEntry) -> None:
        self._entries.append(LedgerEntry.model_validate_json(entry.model_dump_json()))

    def entries(self) -> Sequence[LedgerEntry]:
        return list(self._entries)


def model_call_entry(*, provider: str, unit: str, quantity: float, cost_usd: float | None,
                     cost_status: CostStatus, pricing_reference: str, pricing_date: str,
                     wall_ms: float | None, estimated_job_usd: float, phase: LedgerPhase,
                     mode: MeasurementMode, run_id: str | None,
                     case_id: str | None) -> LedgerEntry:
    return LedgerEntry(
        schema_version="research-ledger-v1", entry_id=f"baselines-{uuid.uuid4()}",
        recorded_at=datetime.now(UTC), lane="baselines", provider=provider,
        category=LedgerCategory.MODEL_INFERENCE, phase=phase, resource_id=None, run_id=run_id,
        case_id=case_id, quantity=quantity, unit=unit, unit_price_usd=None,
        cost_usd=cost_usd, cost_status=cost_status, pricing_reference=pricing_reference,
        pricing_date=pricing_date, wall_ms=wall_ms, queue_ms=None, measurement_mode=mode,
        paid=True, estimated_job_usd=estimated_job_usd, coordinator_checkin_ref=None,
        teardown_verified=None)


@dataclass
class Usage:
    """Per-episode provider usage of one arm (all attempts, including failures)."""

    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    wall_ms: float = 0.0
