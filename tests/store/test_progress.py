from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures.adapters import FixtureRepositoryValidator, SequentialIds
from proofhunter import contracts as c
from proofhunter.api import create_app
from proofhunter.config import mode_config_fingerprint
from proofhunter.ports import ApiDependencies, LeaseLostError
from proofhunter.store import _MIGRATION, SQLiteStore
from tests.store.test_store import clock, create, progress_for, receipt_for, settings


def heartbeat(
    store: SQLiteStore, worker: str = "worker", ttl: int = 30, fingerprint: str = "a" * 64
) -> c.WorkerHeartbeat:
    now = store.clock.now()
    return c.WorkerHeartbeat(
        worker_id=worker,
        profile=c.Profile.OFFLINE,
        config_fingerprint=fingerprint,
        observed_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )


@pytest.mark.parametrize("ttl", [-1, 0, 61])
def test_heartbeat_ttl(tmp_path: Path, ttl: int) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    forged = heartbeat(store).model_copy(
        update={"expires_at": store.clock.now() + timedelta(seconds=ttl)}
    )
    with pytest.raises(ValueError):
        store.write_heartbeat(forged)
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None


def test_heartbeat_durable_time_and_replacement(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    submitted = heartbeat(store)
    store.write_heartbeat(
        submitted.model_copy(
            update={
                "observed_at": submitted.observed_at + timedelta(days=1),
                "expires_at": submitted.expires_at + timedelta(days=1),
            }
        )
    )
    reopened = SQLiteStore(store.settings, clock=timer)
    assert reopened.read_heartbeat(c.Profile.OFFLINE, "a" * 64) == submitted
    reopened.write_heartbeat(heartbeat(store, ttl=60))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) == submitted
    with pytest.raises(ValueError):
        store.write_heartbeat(heartbeat(store, fingerprint="b" * 64))
    timer._now -= timedelta(microseconds=1)
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    with pytest.raises(ValueError, match="regress"):
        store.write_heartbeat(submitted)
    timer._now = submitted.expires_at
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    store.write_heartbeat(heartbeat(store, fingerprint="b" * 64))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    store.write_heartbeat(heartbeat(store, worker="worker-z", fingerprint="b" * 64))
    found = reopened.read_heartbeat(c.Profile.OFFLINE, "b" * 64)
    assert found is not None
    assert found.worker_id == "worker-z"
    timer._now += timedelta(seconds=1)
    store.write_heartbeat(heartbeat(store, worker="worker-a", fingerprint="b" * 64))
    found = reopened.read_heartbeat(c.Profile.OFFLINE, "b" * 64)
    assert found is not None
    assert found.worker_id == "worker-a"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "store",
        "fingerprint",
        "absent",
        "expired",
        "future",
        "profile",
        "mismatch",
        "schema",
        "config",
    ],
)
def test_readiness(tmp_path: Path, failure: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    validator = FixtureRepositoryValidator()
    fingerprint = mode_config_fingerprint(
        store.settings,
        [validator.resolve("https://github.com/proofhunter-demo/checkout-demo/pull/1")[1]],
    )
    beat = heartbeat(store, fingerprint=fingerprint)
    if failure == "profile":
        beat = beat.model_copy(update={"profile": c.Profile.LIVE})
    if failure == "mismatch":
        beat = beat.model_copy(update={"config_fingerprint": "b" * 64})
    if failure != "absent":
        store.write_heartbeat(beat)
    if failure == "expired":
        timer._now = beat.expires_at
    if failure == "future":
        timer._now -= timedelta(seconds=1)
    if failure == "schema":
        monkeypatch.setattr(store, "ready", lambda: False)
    config = store.settings
    if failure == "config":
        config = config.model_copy(update={"api_token": None})
    client = TestClient(
        create_app(
            ApiDependencies(
                settings=config,
                store=store,
                validator=validator,
                webhook=None,
                clock=timer,
                heartbeat_store=None if failure == "store" else store,
                config_fingerprint=None if failure == "fingerprint" else fingerprint,
            )
        )
    )
    response = client.get("/readyz")
    assert response.status_code == (200 if failure is None else 503)
    assert "test-service" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "clear",
        "base_sha",
        "head_sha",
        "resolved_at",
        "owner",
        "repo",
        "number",
        "repo_id",
        "installation_id",
        "meta",
        "snap",
        "caps",
        "target",
        "forged",
    ],
)
def test_progress_rejections_atomic(tmp_path: Path, change: str) -> None:
    config = settings(tmp_path / "db")
    if change == "caps":
        config = config.model_copy(update={"budget": c.BudgetLimits(max_meta_requests=1)})
    store = SQLiteStore(config, clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    progress = progress_for(status, meta=1, snap=1)
    if change != "target":
        store.reserve_progress(lease, progress)
    before = store.load_run(status.run_id)
    pin = progress.pinned_revision
    assert pin is not None
    values: dict[str, object] = {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "resolved_at": pin.resolved_at + timedelta(seconds=1),
        "owner": "other",
        "repo": "other",
        "number": 99,
        "repo_id": 999,
        "installation_id": 999,
    }
    if change in values:
        progress = progress.model_copy(
            update={"pinned_revision": pin.model_copy(update={change: values[change]})}
        )
    elif change == "target":
        progress = progress.model_copy(
            update={"pinned_revision": pin.model_copy(update={"installation_id": 999})}
        )
    elif change == "clear":
        progress = c.RunCheckpoint()
    elif change in ("meta", "snap", "caps"):
        progress = progress_for(
            status,
            meta=2 if change == "caps" else int(change != "meta"),
            snap=int(change != "snap"),
        )
    elif change == "forged":
        progress = progress.model_copy(
            update={
                "provider_allocations": c.ProviderAllocations().model_copy(update={"meta": True})
            }
        )
    with pytest.raises(ValueError):
        store.reserve_progress(lease, progress)
    assert store.load_run(status.run_id) == before


def test_progress_rollback_reopen_reclaim(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "db"), clock=timer)
    status = create(store)
    lease = store.claim("worker", 30)
    assert lease is not None
    saved = progress_for(status, meta=1, snap=2)
    assert store.reserve_progress(lease, saved) == saved
    before = store.load_run(status.run_id)
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_progress BEFORE UPDATE OF status ON runs "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.reserve_progress(lease, progress_for(status, meta=2, snap=3))
    assert store.load_run(status.run_id) == before
    with store._transaction() as db:
        db.execute("DROP TRIGGER reject_progress")
    reopened = SQLiteStore(store.settings, clock=timer)
    timer._now = lease.expires_at
    replacement = reopened.claim("replacement", 30)
    assert replacement is not None
    assert replacement.epoch == lease.epoch + 1
    record = reopened.load_run(status.run_id)
    assert record.checkpoint == saved
    assert record.recoverable
    assert not record.probes
    assert record.status.budget.used.meta_requests == 1
    assert record.status.budget.used.snap_requests == 2
    with pytest.raises(LeaseLostError):
        store.reserve_progress(lease, progress_for(status, meta=2, snap=3))
    reopened.reserve_progress(replacement, progress_for(status, meta=2, snap=3))


def test_v1_upgrade(tmp_path: Path) -> None:
    config = settings(tmp_path / "db")
    donor = SQLiteStore(settings(tmp_path / "donor"), clock=clock(), ids=SequentialIds())
    status = create(donor)
    with donor._connection() as db:
        row = tuple(
            db.execute(
                "SELECT run_id,kind,target,seed,scope,status,state,parent,replay,owner,epoch,"
                "expires FROM runs"
            ).fetchone()
        )
    with sqlite3.connect(config.db_path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for statement in _MIGRATION:
            db.execute(statement)
        db.execute("INSERT INTO schema_version VALUES (1)")
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
    store = SQLiteStore(config, clock=clock())
    assert store.ready()
    record = store.load_run(status.run_id)
    assert record.checkpoint is None
    assert not record.recoverable
    lease = store.claim("worker", 30)
    assert lease is not None
    with pytest.raises(ValueError, match="legacy"):
        store.reserve_progress(lease, progress_for(status))
    assert store.load_run(status.run_id) == record
    assert SQLiteStore(config).ready()


def test_replay_pin_validation(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    store.finish(lease, receipt_for(status), None)
    replay = store.create_replay(status.run_id, c.ReplayRequest(probe_no=1), idempotency_key="r")
    assert store.load_run(replay.run_id).checkpoint == c.RunCheckpoint()
    lease = store.claim("worker", 60)
    assert lease is not None
    progress = progress_for(replay)
    pin = progress.pinned_revision
    assert pin is not None
    before = store.load_run(replay.run_id)
    for field in ("base_sha", "head_sha"):
        with pytest.raises(ValueError, match="replay"):
            store.reserve_progress(
                lease,
                progress.model_copy(
                    update={"pinned_revision": pin.model_copy(update={field: "a" * 40})}
                ),
            )
        assert store.load_run(replay.run_id) == before
    store.reserve_progress(lease, progress)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "other"),
        ("repo", "other"),
        ("number", 99),
        ("repo_id", 999),
        ("installation_id", 999),
    ],
)
def test_initial_pin_target_fields(tmp_path: Path, field: str, value: object) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 30)
    assert lease is not None
    progress = progress_for(status)
    pin = progress.pinned_revision
    assert pin is not None
    before = store.load_run(status.run_id)
    with pytest.raises(ValueError, match="target"):
        store.reserve_progress(
            lease,
            progress.model_copy(update={"pinned_revision": pin.model_copy(update={field: value})}),
        )
    assert store.load_run(status.run_id) == before


def test_snap_cap_and_empty_duplicate(tmp_path: Path) -> None:
    config = settings(tmp_path / "db").model_copy(
        update={"budget": c.BudgetLimits(max_snap_requests=1)}
    )
    store = SQLiteStore(config, clock=clock())
    status = create(store)
    lease = store.claim("worker", 30)
    assert lease is not None
    before = store.load_run(status.run_id)
    for progress in (c.RunCheckpoint(), progress_for(status, snap=2)):
        with pytest.raises(ValueError):
            store.reserve_progress(lease, progress)
        assert store.load_run(status.run_id) == before


def test_concurrent_duplicate_reservation(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 30)
    assert lease is not None

    def reserve(_: int) -> bool:
        connection = SQLiteStore(store.settings, clock=store.clock)
        try:
            connection.reserve_progress(lease, progress_for(status, meta=1))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert store.load_run(status.run_id).checkpoint == progress_for(status, meta=1)
