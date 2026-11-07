from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from fixtures.adapters import FixtureRepositoryValidator, SteppingClock
from proofhunter import contracts as c
from proofhunter.config import load_settings
from proofhunter.ports import LeaseLostError
from proofhunter.store import _MIGRATION, SQLiteStore
from proofhunter.store.outbox import (
    MAX_OUTBOX_CALLS,
    MIGRATION_3,
)
from proofhunter.store.outbox import (
    OutboxOperation as Op,
)
from proofhunter.store.outbox import (
    OutboxOutcome as Outcome,
)
from tests.store.test_store import URL, clock, create, receipt_for, settings


def queued_store(tmp_path: Path, *, live: bool = True, enabled: bool = True) -> SQLiteStore:
    config = settings(tmp_path / "db")
    config = config.model_copy(
        update={
            "profile": c.Profile.LIVE if live else c.Profile.OFFLINE,
            "github": config.github.model_copy(update={"enable_checks": enabled}),
        }
    )
    store = SQLiteStore(config, clock=clock())
    status = create(store, seed=17)
    lease = store.claim("worker", 60)
    assert lease is not None
    receipt = receipt_for(status, live=True)
    revision = receipt.pinned_revision
    assert revision is not None
    intent = c.CheckIntent(
        run_id=status.run_id,
        owner=revision.owner,
        repo=revision.repo,
        repo_id=revision.repo_id,
        installation_id=FixtureRepositoryValidator().resolve(URL)[0].installation_id,
        head_sha=revision.head_sha,
        external_id=status.run_id,
        name="ProofHunter",
        status="completed",
        conclusion=c.CheckConclusion.FAILURE,
        title="Advisory",
        summary="Sanitized",
        receipt_sha256=str(receipt.receipt_sha256),
        execution_mode="live",
    )
    store.finish(lease, receipt, intent)
    return store


def row_state(store: SQLiteStore) -> tuple[str, int | None]:
    with store._connection() as db:
        row = db.execute("SELECT publish_state,check_run_id FROM check_outbox").fetchone()
        return row[0], row[1]


@pytest.mark.parametrize(("live", "enabled"), [(False, False), (False, True), (True, False)])
def test_publisher_default_off(tmp_path: Path, live: bool, enabled: bool) -> None:
    assert not load_settings({"PROOFHUNTER_PROFILE": "offline"}).github.enable_checks
    store = queued_store(tmp_path, live=live, enabled=enabled)
    assert store.claim_outbox("publisher") is None
    assert row_state(store) == ("pending", None)


def test_publish_and_reopen(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    lease = store.claim_outbox("publisher")
    assert lease is not None
    before = store.load_run(lease.intent.run_id), store.get_receipt(lease.intent.run_id)
    assert not lease.requires_reconciliation
    store.begin_outbox_attempt(lease, Op.CREATE)
    store.mark_outbox_published(lease, 123, http_status=201)
    reopened = SQLiteStore(store.settings, clock=store.clock)
    assert row_state(reopened) == ("published", 123)
    assert reopened.claim_outbox("other") is None
    assert (store.load_run(lease.intent.run_id), store.get_receipt(lease.intent.run_id)) == before
    attempts = reopened.outbox_attempts(lease.intent.run_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert [a.outcome for a in attempts] == [Outcome.STARTED, Outcome.PUBLISHED]
    assert [a.http_status for a in attempts] == [None, 201]
    assert all(a.at.utcoffset() == timedelta(0) for a in attempts)
    assert not reopened.outbox_attempts("missing")
    with pytest.raises(LeaseLostError):
        store.mark_outbox_published(lease, 123)


@pytest.mark.parametrize("found", [True, False])
def test_ambiguous_create_requires_bound_reconciliation(tmp_path: Path, found: bool) -> None:
    store = queued_store(tmp_path)
    lease = store.claim_outbox("publisher")
    assert lease is not None
    store.begin_outbox_attempt(lease, Op.CREATE)
    store.mark_outbox_ambiguous(lease, http_status=503)
    store = SQLiteStore(store.settings, clock=store.clock)
    lease = store.claim_outbox("replacement")
    assert lease is not None
    assert lease.requires_reconciliation
    with pytest.raises(ValueError, match="reconciliation required"):
        store.begin_outbox_attempt(lease, Op.CREATE)
    store.begin_outbox_attempt(lease, Op.RECONCILE)
    with pytest.raises(ValueError, match="reserved create"):
        store.mark_outbox_published(lease, 123)
    for sha, external_id in [
        ("a" * 40, lease.intent.external_id),
        (lease.intent.head_sha, "other"),
    ]:
        with pytest.raises(ValueError, match="match commit"):
            store.reconcile_outbox(lease, head_sha=sha, external_id=external_id, check_run_id=None)
    store.reconcile_outbox(
        lease,
        head_sha=lease.intent.head_sha,
        external_id=lease.intent.external_id,
        check_run_id=456 if found else None,
    )
    if found:
        assert row_state(store) == ("published", 456)
    else:
        lease = store.claim_outbox("retry")
        assert lease is not None
        assert not lease.requires_reconciliation
        store.begin_outbox_attempt(lease, Op.CREATE)
        store.mark_outbox_published(lease, 789, http_status=201)
        assert row_state(store) == ("published", 789)


def test_persistent_ambiguity_holds(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    for operation in (Op.CREATE, Op.RECONCILE):
        lease = store.claim_outbox("publisher")
        assert lease is not None
        store.begin_outbox_attempt(lease, operation)
        store.mark_outbox_ambiguous(lease)
    assert row_state(store) == ("held", None)
    assert store.claim_outbox("publisher") is None


def test_retry_bound_and_sanitization(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    for _ in range(MAX_OUTBOX_CALLS):
        lease = store.claim_outbox("publisher")
        assert lease is not None
        store.begin_outbox_attempt(lease, Op.CREATE)
        with pytest.raises(ValueError):
            store.record_outbox_attempt(lease, Outcome.RETRYABLE, http_status=503)
        with pytest.raises(ValueError):
            store.record_outbox_attempt(lease, Outcome("secret provider body"))
        with pytest.raises(ValueError):
            store.mark_outbox_ambiguous(lease, http_status=999)
        store.record_outbox_attempt(lease, Outcome.RETRYABLE, http_status=429)
    assert row_state(store) == ("held", None)
    assert store.claim_outbox("publisher") is None
    assert lease is not None
    assert len(store.outbox_attempts(lease.intent.run_id)) == MAX_OUTBOX_CALLS * 2


def test_lease_fencing_and_lost_acknowledgement(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    old = store.claim_outbox("publisher", 1)
    assert old is not None
    with pytest.raises(LeaseLostError):
        store.begin_outbox_attempt(replace(old, owner="wrong"), Op.CREATE)
    store.begin_outbox_attempt(old, Op.CREATE)
    with pytest.raises(ValueError):
        store.begin_outbox_attempt(old, Op.CREATE)
    timer = store.clock
    assert isinstance(timer, SteppingClock)
    timer._now += timedelta(seconds=1)
    with pytest.raises(LeaseLostError):
        store.mark_outbox_published(old, 1)
    new = store.claim_outbox("replacement")
    assert new is not None
    assert new.epoch > old.epoch
    assert new.requires_reconciliation
    with pytest.raises(LeaseLostError):
        store.mark_outbox_ambiguous(old)
    with pytest.raises(ValueError):
        store.begin_outbox_attempt(new, Op.CREATE)
    store.begin_outbox_attempt(new, Op.RECONCILE)
    store.reconcile_outbox(
        new, head_sha=new.intent.head_sha, external_id=new.intent.external_id, check_run_id=42
    )
    assert row_state(store) == ("published", 42)


def test_concurrent_claim(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(store.claim_outbox, ["one", "two"]))
    assert sum(lease is not None for lease in leases) == 1


def test_populated_v2_upgrade(tmp_path: Path) -> None:
    donor = queued_store(tmp_path)
    with donor._connection() as db:
        run_id = db.execute("SELECT run_id FROM runs").fetchone()[0]
        db.execute("INSERT INTO inbound_messages VALUES (?,?,?)", ("acp:test", "extra", run_id))
        db.execute("INSERT INTO webhook_deliveries VALUES (?,?,?)", ("delivery", "{}", run_id))
        db.execute("INSERT INTO run_events VALUES (?,?,?)", (run_id, 1, "{}"))
        db.execute("INSERT INTO attempts VALUES (?,?,?)", (run_id, "attempt", "{}"))
        db.execute("INSERT INTO probes VALUES (?,?,?,?)", (run_id, 1, "a" * 64, "{}"))
        db.execute(
            "INSERT INTO observations VALUES (?,?,?,?)", (run_id, "a" * 64, "head", "attempt")
        )
        db.execute("INSERT INTO worker_heartbeats VALUES (?,?)", ("worker", "{}"))
    create(donor, key="queued", scope="other")
    config = donor.settings.model_copy(update={"db_path": tmp_path / "v2"})
    with donor._connection() as source, sqlite3.connect(config.db_path) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for sql in _MIGRATION:
            db.execute(sql)
        db.execute("ALTER TABLE runs ADD COLUMN checkpoint TEXT")
        db.execute("CREATE TABLE worker_heartbeats(worker_id TEXT PRIMARY KEY,body TEXT NOT NULL)")
        db.executemany("INSERT INTO schema_version VALUES (?)", [(1,), (2,)])
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {}
        for table in tables:
            if table == "schema_version":
                continue
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            rows = [tuple(r) for r in source.execute(f"SELECT {','.join(columns)} FROM {table}")]
            db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", rows)
            before[table] = (columns, rows)
    migrated = SQLiteStore(config, clock=donor.clock)
    assert migrated.ready()
    SQLiteStore(config)
    with migrated._connection() as db:
        assert [
            r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")
        ] == [1, 2, 3, 4]
        for table, (columns, rows) in before.items():
            assert [
                tuple(r) for r in db.execute(f"SELECT {','.join(columns)} FROM {table}")
            ] == rows
        assert db.execute("SELECT COUNT(*) FROM outbox_attempts").fetchone()[0] == 0
        db.execute("INSERT INTO schema_version VALUES (5)")
    with pytest.raises(ValueError, match="unsupported"):
        SQLiteStore(config)


@pytest.mark.parametrize(
    "table", ["receipts", "observations", "run_events", "probes", "outbox_attempts"]
)
def test_append_only_triggers(tmp_path: Path, table: str) -> None:
    store = queued_store(tmp_path)
    with store._connection() as db:
        run_id = db.execute("SELECT run_id FROM runs").fetchone()[0]
        if table == "observations":
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?)", (run_id, "a" * 64, "head", "attempt")
            )
        if table in ("run_events", "probes"):
            if table == "run_events":
                db.execute("INSERT INTO run_events VALUES (?,?,?)", (run_id, 1, "{}"))
            else:
                db.execute("INSERT INTO probes VALUES (?,?,?,?)", (run_id, 1, "a" * 64, "{}"))
    if table == "outbox_attempts":
        lease = store.claim_outbox("publisher")
        assert lease is not None
        store.begin_outbox_attempt(lease, Op.CREATE)
    with store._connection() as db:
        column = db.execute(f"PRAGMA table_info({table})").fetchone()[1]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"UPDATE {table} SET {column}={column}")
        if table == "receipts":
            db.execute("DELETE FROM check_outbox")
        with pytest.raises(sqlite3.IntegrityError, match="retention gate"):
            db.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_terminal_run_update_trigger(tmp_path: Path, state: str) -> None:
    store = queued_store(tmp_path)
    with store._connection() as db:
        if state == "failed":
            db.execute("DROP TRIGGER immutable_terminal_run")
            db.execute("UPDATE runs SET state='failed'")
            db.execute(MIGRATION_3[-1])
        for column in ("status", "state", "parent", "replay", "checkpoint"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable terminal"):
                db.execute(f"UPDATE runs SET {column}={column}")
        db.execute("UPDATE runs SET owner=NULL,expires=NULL")


def test_outbox_atomicity_and_disabled_write_fencing(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    lease = store.claim_outbox("publisher")
    assert lease is not None
    with store._connection() as db:
        db.execute(
            "CREATE TRIGGER reject_attempt BEFORE INSERT ON outbox_attempts "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.begin_outbox_attempt(lease, Op.CREATE)
    with store._connection() as db:
        assert tuple(
            db.execute(
                "SELECT calls,operation,requires_reconciliation FROM check_outbox"
            ).fetchone()
        ) == (0, None, 0)
        db.execute("DROP TRIGGER reject_attempt")
    with pytest.raises(LeaseLostError):
        store.begin_outbox_attempt(replace(lease, epoch=lease.epoch + 1), Op.CREATE)
    store.begin_outbox_attempt(lease, Op.CREATE)
    disabled = SQLiteStore(
        store.settings.model_copy(update={"profile": c.Profile.OFFLINE}), clock=store.clock
    )
    with pytest.raises(LeaseLostError):
        disabled.mark_outbox_published(lease, 1)
    assert len(store.outbox_attempts(lease.intent.run_id)) == 1
