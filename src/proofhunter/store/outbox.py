"""Durable publisher lease and sanitized attempt records."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from proofhunter.contracts import CheckIntent


class OutboxOperation(StrEnum):
    CREATE = "create"
    RECONCILE = "reconcile"


class OutboxOutcome(StrEnum):
    STARTED = "started"
    RETRYABLE = "retryable"
    AMBIGUOUS = "ambiguous"
    PUBLISHED = "published"
    RECONCILED_ABSENT = "reconciled_absent"
    HELD = "held"


@dataclass(frozen=True)
class OutboxLease:
    intent: CheckIntent
    owner: str
    epoch: int
    expires_at: datetime
    requires_reconciliation: bool


@dataclass(frozen=True)
class OutboxAttempt:
    attempt_no: int
    at: datetime
    operation: OutboxOperation
    outcome: OutboxOutcome
    http_status: int | None


MAX_OUTBOX_CALLS = 5
MAX_OUTBOX_AMBIGUITIES = 2

MIGRATION_3 = (
    "ALTER TABLE check_outbox ADD COLUMN check_run_id INTEGER",
    "ALTER TABLE check_outbox ADD COLUMN owner TEXT",
    "ALTER TABLE check_outbox ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE check_outbox ADD COLUMN expires REAL",
    "ALTER TABLE check_outbox ADD COLUMN requires_reconciliation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE check_outbox ADD COLUMN operation TEXT",
    "ALTER TABLE check_outbox ADD COLUMN calls INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE check_outbox ADD COLUMN ambiguities INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE outbox_attempts (run_id TEXT NOT NULL REFERENCES check_outbox(run_id), "
    "attempt_no INTEGER NOT NULL, at TEXT NOT NULL, operation TEXT NOT NULL, "
    "outcome TEXT NOT NULL, http_status INTEGER, PRIMARY KEY(run_id,attempt_no))",
    *(
        f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable record'); END"
        for table in ("receipts", "observations", "run_events", "probes", "outbox_attempts")
    ),
    "CREATE TRIGGER immutable_terminal_run BEFORE UPDATE OF "
    "run_id,status,state,kind,target,seed,scope,parent,replay,checkpoint ON runs "
    "WHEN OLD.state IN ('completed','failed') "
    "BEGIN SELECT RAISE(ABORT,'immutable terminal run'); END",
)
