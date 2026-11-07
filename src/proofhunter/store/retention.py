"""Bounded local retention, executed only within the store's writer transaction."""

# SQL identifiers/fragments are fixed below; all caller values are bound parameters.
# ruff: noqa: S608

import sqlite3
from datetime import UTC, datetime, timedelta

OPERATIONS_TTL = timedelta(days=7)
RECEIPTS_TTL = timedelta(days=30)
DEFAULT_BATCH_LIMIT = 500
MAX_BATCH_LIMIT = 5000

PROTECTED_TABLES = ("receipts", "observations", "run_events", "probes", "outbox_attempts")
OPERATION_TABLES = (
    "run_events",
    "attempts",
    "observations",
    "probes",
    "inbound_messages",
    "idempotency_keys",
    "webhook_deliveries",
    "quota_reservations",
    "outbox_attempts",
)
SETTLED_OUTBOX = (
    "('published','failed_permanent','stale_head','skipped_disabled','skipped_offline')"
)
MIGRATION_4 = (
    "CREATE TABLE retention_gate (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))",
    "INSERT INTO retention_gate VALUES (1,0)",
    "ALTER TABLE runs ADD COLUMN enrollment TEXT",
    "DROP TRIGGER immutable_terminal_run",
    "CREATE TRIGGER immutable_terminal_run BEFORE UPDATE OF "
    "run_id,status,state,kind,target,seed,scope,parent,replay,checkpoint,enrollment ON runs "
    "WHEN OLD.state IN ('completed','failed') "
    "BEGIN SELECT RAISE(ABORT,'immutable terminal run'); END",
    *(
        f"CREATE TRIGGER retention_delete_{table} BEFORE DELETE ON {table} "
        "WHEN COALESCE((SELECT enabled FROM retention_gate WHERE singleton=1),0) != 1 "
        "BEGIN SELECT RAISE(ABORT,'retention gate required'); END"
        for table in PROTECTED_TABLES
    ),
)

_ACTIVE = """
WITH RECURSIVE protected(run_id) AS (
    SELECT run_id FROM runs
    WHERE state NOT IN ('completed','failed') OR (owner IS NOT NULL AND expires>:now)
       OR EXISTS (SELECT 1 FROM check_outbox o WHERE o.run_id=runs.run_id
                  AND o.owner IS NOT NULL AND o.expires>:now)
    UNION
    SELECT parent.run_id FROM runs parent
    JOIN runs child ON child.parent=json_extract(parent.status,'$.receipt_sha256')
    JOIN protected ON protected.run_id=child.run_id
)
"""
_TERMINAL = """
r.state IN ('completed','failed')
AND (r.owner IS NULL OR r.expires IS NULL OR r.expires<=:now)
AND r.run_id NOT IN (SELECT run_id FROM protected)
"""
_ORDER = "julianday(json_extract(r.status,'$.updated_at')),r.run_id,t.rowid"
_CHILD = """
EXISTS (SELECT 1 FROM runs child
        WHERE child.parent=json_extract(r.status,'$.receipt_sha256'))
"""


def purge(
    db: sqlite3.Connection,
    now: datetime,
    *,
    operations_ttl: timedelta,
    receipts_ttl: timedelta,
    batch_limit: int,
    dry_run: bool,
) -> dict[str, int]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("retention requires an aware UTC time")
    if operations_ttl <= timedelta(0) or receipts_ttl < operations_ttl:
        raise ValueError("invalid retention TTLs")
    if type(batch_limit) is not int or not 1 <= batch_limit <= MAX_BATCH_LIMIT:
        raise ValueError("invalid retention batch limit")
    if type(dry_run) is not bool:
        raise ValueError("invalid dry run")
    if db.execute("SELECT enabled FROM retention_gate WHERE singleton=1").fetchone()[0] != 0:
        raise ValueError("retention gate already active")
    params = {
        "now": now.timestamp(),
        "operations": (now - operations_ttl).astimezone(UTC).isoformat(),
        "receipts": (now - receipts_ttl).astimezone(UTC).isoformat(),
        "limit": batch_limit,
    }
    if dry_run:
        db.execute("SAVEPOINT retention_preview")
    db.execute("UPDATE retention_gate SET enabled=1 WHERE singleton=1")
    counts: dict[str, int] = {}

    def delete(table: str, selection: str) -> None:
        rows = db.execute(selection, params).fetchall()
        db.executemany(f"DELETE FROM {table} WHERE rowid=?", [(row[0],) for row in rows])
        counts[table] = len(rows)

    for table in OPERATION_TABLES:
        extra = ""
        if table == "outbox_attempts":
            extra = (
                "AND EXISTS (SELECT 1 FROM check_outbox o WHERE o.run_id=r.run_id "
                f"AND o.publish_state IN {SETTLED_OUTBOX} "
                "AND (o.owner IS NULL OR o.expires<=:now))"
            )
        delete(
            table,
            _ACTIVE + f"SELECT t.rowid FROM {table} t JOIN runs r ON r.run_id=t.run_id "
            f"WHERE {_TERMINAL} AND julianday(json_extract(r.status,'$.updated_at'))"
            f"<=julianday(:operations) {extra} ORDER BY {_ORDER} LIMIT :limit",
        )

    delete(
        "worker_heartbeats",
        "SELECT rowid FROM worker_heartbeats "
        "WHERE julianday(json_extract(body,'$.expires_at'))<=julianday(:operations) "
        "ORDER BY julianday(json_extract(body,'$.expires_at')),worker_id LIMIT :limit",
    )

    expired = (
        "r.state IN ('completed','failed') "
        "AND julianday(json_extract(r.status,'$.updated_at'))<=julianday(:receipts)"
    )
    counts["retained_replay_parents"] = db.execute(
        f"SELECT COUNT(*) FROM runs r WHERE {expired} AND {_CHILD}", params
    ).fetchone()[0]
    counts["retained_outbox"] = db.execute(
        f"SELECT COUNT(*) FROM check_outbox o JOIN runs r ON r.run_id=o.run_id WHERE {expired} "
        f"AND (o.publish_state NOT IN {SETTLED_OUTBOX} "
        "OR (o.owner IS NOT NULL AND o.expires>:now))",
        params,
    ).fetchone()[0]

    eligible = (
        _TERMINAL + " AND julianday(json_extract(r.status,'$.updated_at'))<=julianday(:receipts)"
    )
    delete(
        "check_outbox",
        _ACTIVE + "SELECT t.rowid FROM check_outbox t JOIN runs r ON r.run_id=t.run_id "
        f"WHERE {eligible} AND NOT {_CHILD} AND t.publish_state IN {SETTLED_OUTBOX} "
        "AND (t.owner IS NULL OR t.expires<=:now) "
        "AND NOT EXISTS (SELECT 1 FROM outbox_attempts a WHERE a.run_id=r.run_id) "
        f"ORDER BY {_ORDER} LIMIT :limit",
    )
    delete(
        "receipts",
        _ACTIVE + "SELECT t.rowid FROM receipts t JOIN runs r ON r.run_id=t.run_id "
        f"WHERE {eligible} AND NOT {_CHILD} "
        "AND NOT EXISTS (SELECT 1 FROM check_outbox o WHERE o.run_id=r.run_id) "
        f"ORDER BY {_ORDER} LIMIT :limit",
    )
    dependencies = (*OPERATION_TABLES, "receipts", "check_outbox")
    unreferenced = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {table} d WHERE d.run_id=r.run_id)" for table in dependencies
    )
    delete(
        "runs",
        _ACTIVE + f"SELECT r.rowid FROM runs r WHERE {eligible} AND NOT {_CHILD} "
        f"AND {unreferenced} "
        "ORDER BY julianday(json_extract(r.status,'$.updated_at')),r.run_id LIMIT :limit",
    )
    db.execute("UPDATE retention_gate SET enabled=0 WHERE singleton=1")
    if dry_run:
        db.execute("ROLLBACK TO retention_preview")
        db.execute("RELEASE retention_preview")
    return counts
