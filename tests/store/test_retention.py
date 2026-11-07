from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from fixtures.adapters import SteppingClock
from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.store import _MIGRATION, SQLiteStore
from proofhunter.store.outbox import MIGRATION_3, OutboxOperation
from proofhunter.store.retention import OPERATION_TABLES, PROTECTED_TABLES
from tests.store.test_outbox import queued_store
from tests.store.test_progress import heartbeat
from tests.store.test_store import (
    OPERATOR,
    SERVICE,
    attempt_for,
    client_for,
    clock,
    create,
    event_for,
    probe_for,
    receipt_for,
    settings,
)


def completed(store: SQLiteStore, key: str = "parent") -> c.ReceiptV1:
    status = create(store, key=key, seed=17)
    lease = store.claim("worker", 60)
    assert lease is not None
    store.checkpoint(lease, event_for(status))
    store.record_attempt(lease, attempt_for(status))
    receipt = receipt_for(status)
    store.commit_probe(lease, probe_for(receipt))
    store.finish(lease, receipt, None)
    with store._connection() as db:
        db.execute("INSERT INTO webhook_deliveries VALUES (?,?,?)", (key, "{}", status.run_id))
    return receipt


def database(store: SQLiteStore) -> list[str]:
    with store._connection() as db:
        return list(db.iterdump())


def test_ten_day_replay_preserves_original_bindings(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    parent = completed(store)
    store.write_heartbeat(heartbeat(store))
    before = store.get_receipt(parent.run_id)
    with store._connection() as db:
        original_run = tuple(db.execute("SELECT * FROM runs").fetchone())
    timer._now += timedelta(days=10)
    counts = store.purge_expired(timer.now())
    for table in OPERATION_TABLES:
        expected = {"outbox_attempts": 0, "observations": 3}.get(table, 1)
        assert counts[table] == expected
    assert counts["worker_heartbeats"] == 1
    assert counts["runs"] == counts["receipts"] == 0
    assert store.get_receipt(parent.run_id) == before
    with store._connection() as db:
        assert tuple(db.execute("SELECT * FROM runs").fetchone()) == original_run
    client = client_for(store)
    response = client.post(
        f"/v1/runs/{parent.run_id}/replays", json={"probe_no": 1}, headers=OPERATOR
    )
    assert response.status_code == 202
    replay = store.load_run(response.json()["run_id"])
    assert replay.replay == oracle.validate_replay(parent, 1)
    assert replay.parent_receipt_sha256 == parent.receipt_sha256
    with store._connection() as db:
        enrollments = [r[0] for r in db.execute("SELECT enrollment FROM runs ORDER BY rowid")]
        assert enrollments[0] is not None
        assert enrollments[0] == enrollments[1]


def test_thirty_day_expiry_returns_404_and_409(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    parent = completed(store)
    timer._now += timedelta(days=31)
    counts = store.purge_expired(timer.now())
    assert counts["receipts"] == counts["runs"] == 1
    client = client_for(store)
    assert client.get(f"/v1/runs/{parent.run_id}/receipt", headers=SERVICE).status_code == 404
    response = client.post(
        f"/v1/runs/{parent.run_id}/replays", json={"probe_no": 1}, headers=OPERATOR
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "replay_unavailable"


@pytest.mark.parametrize("terminal_child", [False, True])
def test_replay_parent_reference_protection(tmp_path: Path, terminal_child: bool) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    parent = completed(store)
    timer._now += timedelta(days=31)
    child = store.create_replay(parent.run_id, c.ReplayRequest(probe_no=1), idempotency_key="child")
    if terminal_child:
        lease = store.claim("worker", 60)
        assert lease is not None
        binding = parent.replay
        assert binding is not None
        receipt = parent.model_copy(
            update={
                "run_id": child.run_id,
                "run_kind": c.RunKind.REPLAY,
                "parent_receipt_sha256": parent.receipt_sha256,
                "created_at": child.created_at,
                "completed_at": child.created_at + timedelta(seconds=1),
                "replay": binding.model_copy(
                    update={
                        "command": c.build_replay_command(
                            binding.execution_mode, child.run_id, None
                        )
                    }
                ),
                "receipt_sha256": None,
            }
        )
        store.finish(lease, oracle.seal(receipt), None)
    counts = store.purge_expired(timer.now())
    assert counts["retained_replay_parents"] == 1
    assert store.get_receipt(parent.run_id) == parent
    assert store.get_status(child.run_id) is not None
    with store._connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=?", (parent.run_id,)
        ).fetchone()[0] == int(not terminal_child)
    if terminal_child:
        timer._now += timedelta(days=31)
        store.purge_expired(timer.now())
        store.purge_expired(timer.now())
        assert store.get_status(parent.run_id) is None
        assert store.get_status(child.run_id) is None


@pytest.mark.parametrize("state", list(c.ACTIVE_STATES))
def test_nonterminal_runs_and_references_untouched(tmp_path: Path, state: c.RunState) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    status = create(store, seed=17)
    lease = store.claim("worker", 60)
    assert lease is not None
    store.record_attempt(lease, attempt_for(status))
    store.commit_probe(lease, probe_for(receipt_for(status)))
    with store._connection() as db:
        db.execute("UPDATE runs SET state=?", (state.value,))
    timer._now += timedelta(days=40)
    before = database(store)
    store.purge_expired(timer.now())
    assert database(store) == before


@pytest.mark.parametrize("state", ["pending", "claimed", "held", "published"])
def test_outbox_retention_and_active_lease(tmp_path: Path, state: str) -> None:
    store = queued_store(tmp_path)
    timer = store.clock
    assert isinstance(timer, SteppingClock)
    timer._now += timedelta(days=31)
    with store._connection() as db:
        db.execute("UPDATE check_outbox SET publish_state=?", (state,))
    counts = store.purge_expired(timer.now())
    retained = int(state != "published")
    assert counts["retained_outbox"] == retained
    assert counts["receipts"] == counts["runs"] == 1 - retained


def test_settled_attempts_and_active_outbox_lease(tmp_path: Path) -> None:
    store = queued_store(tmp_path)
    lease = store.claim_outbox("publisher")
    assert lease is not None
    store.begin_outbox_attempt(lease, OutboxOperation.CREATE)
    store.mark_outbox_published(lease, 123)
    timer = store.clock
    assert isinstance(timer, SteppingClock)
    timer._now += timedelta(days=10)
    assert store.purge_expired(timer.now())["outbox_attempts"] == 2
    with store._connection() as db:
        assert db.execute("SELECT check_run_id FROM check_outbox").fetchone()[0] == 123
        db.execute(
            "UPDATE check_outbox SET owner='publisher',expires=?",
            ((timer.now() + timedelta(days=40)).timestamp(),),
        )
    timer._now += timedelta(days=31)
    before = database(store)
    counts = store.purge_expired(timer.now())
    assert counts["retained_outbox"] == 1
    assert database(store) == before


def test_terminal_run_with_live_lease_is_protected(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    completed(store)
    timer._now += timedelta(days=31)
    with store._connection() as db:
        db.execute(
            "UPDATE runs SET owner='worker',expires=?",
            ((timer.now() + timedelta(seconds=60)).timestamp(),),
        )
    before = database(store)
    store.purge_expired(timer.now())
    assert database(store) == before


def test_dry_run_bounded_oldest_first_and_convergence(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    first = completed(store, "first")
    timer._now += timedelta(days=1)
    completed(store, "second")
    with store._connection() as db:
        db.executemany(
            "INSERT INTO run_events VALUES (?,?,?)", [(first.run_id, n, "{}") for n in range(2, 5)]
        )
    timer._now += timedelta(days=31)
    before = database(store)
    preview = store.purge_expired(timer.now(), batch_limit=1, dry_run=True)
    assert database(store) == before
    assert store.purge_expired(timer.now(), batch_limit=1) == preview
    with store._connection() as db:
        assert (
            db.execute(
                "SELECT MIN(seq) FROM run_events WHERE run_id=?", (first.run_id,)
            ).fetchone()[0]
            == 2
        )
    for _ in range(10):
        counts = store.purge_expired(timer.now(), batch_limit=1)
        assert all(
            0 <= count <= 1 for key, count in counts.items() if not key.startswith("retained_")
        )
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_failure_rolls_back_deletes_and_gate(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    completed(store)
    timer._now += timedelta(days=31)
    with store._connection() as db:
        db.execute(
            "CREATE TRIGGER fail_purge BEFORE DELETE ON attempts "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    before = database(store)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.purge_expired(timer.now())
    assert database(store) == before
    with store._connection() as db:
        assert db.execute("SELECT enabled FROM retention_gate").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_purge")
    assert store.purge_expired(timer.now())["runs"] == 1


@pytest.mark.parametrize(
    "tokens", [(None, None), ("same", "same"), ("service", None), (None, "operator")]
)
def test_retention_auth_fails_closed(tmp_path: Path, tokens: tuple[str | None, str | None]) -> None:
    config = settings(tmp_path / "db").model_copy(
        update={
            "api_token": None if tokens[0] is None else SecretStr(tokens[0]),
            "operator_token": None if tokens[1] is None else SecretStr(tokens[1]),
        }
    )
    client = client_for(SQLiteStore(config, clock=clock()))
    assert (
        client.post(
            "/v1/retention/purge", json={}, headers={"Authorization": "Bearer same"}
        ).status_code
        == 401
    )


def test_retention_operator_endpoint_defaults_to_preview(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    completed(store)
    timer._now += timedelta(days=31)
    client = client_for(store)
    before = database(store)
    assert client.post("/v1/retention/purge", json={}, headers=SERVICE).status_code == 401
    preview = client.post("/v1/retention/purge", json={}, headers=OPERATOR)
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    assert all(type(count) is int for count in preview.json()["counts"].values())
    assert database(store) == before
    result = client.post("/v1/retention/purge", json={"dry_run": False}, headers=OPERATOR)
    assert result.status_code == 200
    assert result.json()["counts"] == preview.json()["counts"]
    for body in (
        {"batch_limit": 0},
        {"batch_limit": 5001},
        {"dry_run": "false"},
        {"now": "future"},
    ):
        assert client.post("/v1/retention/purge", json=body, headers=OPERATOR).status_code == 422


@pytest.mark.parametrize("age", [6, 7, 29, 30])
def test_terminal_time_boundaries(tmp_path: Path, age: int) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    parent = completed(store)
    counts = store.purge_expired(parent.completed_at + timedelta(days=age))
    assert counts["run_events"] == int(age >= 7)
    assert counts["receipts"] == int(age >= 30)


@pytest.mark.parametrize("limit", [0, -1, 5001, True])
def test_retention_rejects_invalid_batch(tmp_path: Path, limit: int) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    before = database(store)
    with pytest.raises(ValueError, match="batch"):
        store.purge_expired(store.clock.now(), batch_limit=limit)
    assert database(store) == before


def test_empty_retention_and_custom_ttls(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    assert all(count == 0 for count in store.purge_expired(store.clock.now()).values())
    parent = completed(store)
    with pytest.raises(ValueError, match="TTL"):
        store.purge_expired(store.clock.now(), operations_ttl=timedelta(days=31))
    with pytest.raises(ValueError, match="aware"):
        store.purge_expired(store.clock.now().replace(tzinfo=None))
    counts = store.purge_expired(
        parent.completed_at + timedelta(days=3),
        operations_ttl=timedelta(days=1),
        receipts_ttl=timedelta(days=2),
    )
    assert counts["runs"] == counts["receipts"] == 1


def test_populated_v3_upgrade(tmp_path: Path) -> None:
    donor = queued_store(tmp_path)
    lease = donor.claim_outbox("publisher")
    assert lease is not None
    donor.begin_outbox_attempt(lease, OutboxOperation.CREATE)
    donor.mark_outbox_published(lease, 42)
    config = donor.settings.model_copy(update={"db_path": tmp_path / "v3"})
    with donor._connection() as source, sqlite3.connect(config.db_path) as db:
        db.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for sql in _MIGRATION:
            db.execute(sql)
        db.execute("ALTER TABLE runs ADD COLUMN checkpoint TEXT")
        db.execute("CREATE TABLE worker_heartbeats(worker_id TEXT PRIMARY KEY,body TEXT NOT NULL)")
        for sql in MIGRATION_3:
            db.execute(sql)
        db.executemany("INSERT INTO schema_version VALUES (?)", [(1,), (2,), (3,)])
        before = {}
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if table == "schema_version":
                continue
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            rows = [tuple(r) for r in source.execute(f"SELECT {','.join(columns)} FROM {table}")]
            db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", rows)
            before[table] = columns, rows
    migrated = SQLiteStore(config, clock=donor.clock)
    assert migrated.ready()
    SQLiteStore(config)
    with migrated._connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 4
        assert db.execute("SELECT enabled FROM retention_gate").fetchone()[0] == 0
        for table, (columns, rows) in before.items():
            assert [
                tuple(r) for r in db.execute(f"SELECT {','.join(columns)} FROM {table}")
            ] == rows
        for table in PROTECTED_TABLES:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
                    (f"retention_delete_{table}",),
                ).fetchone()[0]
                == 1
            )
