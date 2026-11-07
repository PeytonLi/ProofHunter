from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from fixtures.adapters import (
    FixtureRepositoryValidator,
    FixtureWebhookVerifier,
    InMemoryRunStore,
    SequentialIds,
    SteppingClock,
)
from proofhunter import contracts as c
from proofhunter.api import create_app
from proofhunter.config import Settings, load_settings
from proofhunter.contracts import (
    AttemptRecord,
    AttemptStatus,
    CheckConclusion,
    CheckIntent,
    ErrorEnvelope,
    ProbeRecord,
    QuotaPolicy,
    ReasonCode,
    ReceiptV1,
    ReplayRequest,
    RunEvent,
    RunEventKind,
    RunKind,
    RunRequest,
    RunState,
    RunStatus,
    build_replay_command,
    compute_receipt_sha256,
)
from proofhunter.ports import (
    AdapterError,
    ApiDependencies,
    IdempotencyConflictError,
    LeaseLostError,
)
from proofhunter.store import SQLiteStore, create_store

URL = "https://github.com/proofhunter-demo/checkout-demo/pull/1"


def settings(path: Path) -> Settings:
    return load_settings({"PROOFHUNTER_PROFILE": "offline"}).model_copy(
        update={
            "db_path": path,
            "api_token": SecretStr("test-service"),
            "operator_token": SecretStr("test-operator"),
        }
    )


def clock() -> SteppingClock:
    return SteppingClock(step=timedelta(0))


def create(
    store: SQLiteStore | InMemoryRunStore,
    key: str = "message",
    scope: str = "acp:test",
    seed: int | None = None,
) -> RunStatus:
    target, enrollment = FixtureRepositoryValidator().resolve(URL)
    return store.create_run(
        RunRequest(pr_url=URL, seed=seed),
        idempotency_key=key,
        requester_scope=scope,
        target=target,
        enrollment=enrollment,
    )


def receipt_for(status: RunStatus, *, live: bool = False) -> ReceiptV1:
    receipt = ReceiptV1.model_validate_json(
        Path("fixtures/demo/receipts/buggy.seed17.json").read_bytes()
    )
    data = receipt.model_dump(mode="json")
    data.update(
        run_id=status.run_id,
        receipt_sha256=None,
        created_at=status.created_at,
        completed_at=status.created_at + timedelta(seconds=1),
    )
    if live:
        data["execution_mode"] = "live"
    unsealed = ReceiptV1.model_validate(data)
    if unsealed.replay is not None:
        unsealed = unsealed.model_copy(
            update={
                "replay": unsealed.replay.model_copy(
                    update={
                        "execution_mode": unsealed.execution_mode,
                        "command": build_replay_command(
                            unsealed.execution_mode, status.run_id, None
                        ),
                    }
                )
            }
        )
    return ReceiptV1.model_validate(
        unsealed.model_copy(
            update={"receipt_sha256": compute_receipt_sha256(unsealed)}
        ).model_dump()
    )


def progress_for(status: RunStatus, meta: int = 0, snap: int = 0) -> c.RunCheckpoint:
    revision = receipt_for(status).pinned_revision
    assert revision is not None
    target, _ = FixtureRepositoryValidator().resolve(URL)
    pin = c.PinnedRevision(
        **target.model_dump(),
        base_sha=revision.base_sha,
        head_sha=revision.head_sha,
        resolved_at=status.created_at,
    )
    return c.RunCheckpoint(
        pinned_revision=pin, provider_allocations=c.ProviderAllocations(meta=meta, snap=snap)
    )


def probe_for(receipt: ReceiptV1) -> ProbeRecord:
    data = receipt.probes[0].model_dump(mode="json")
    for side in ("base", "head", "confirmation"):
        if data[side] is not None:
            data[side]["sandbox_id"] = "operator-sandbox"
    data["route"]["provider_request_id"] = None
    return ProbeRecord.model_validate(data)


def event_for(status: RunStatus) -> RunEvent:
    return RunEvent(
        run_id=status.run_id,
        seq=1,
        at=status.created_at,
        kind=RunEventKind.STATE_CHANGED,
        from_state=RunState.QUEUED,
        to_state=RunState.FETCHING,
        reason_code=None,
        detail=None,
    )


def attempt_for(status: RunStatus, epoch: int = 1) -> AttemptRecord:
    probe = probe_for(receipt_for(status))
    return AttemptRecord(
        run_id=status.run_id,
        attempt_id=probe.base.attempt_id,
        side=probe.base.side,
        input_sha256=probe.candidate.input_sha256,
        probe_no=1,
        status=AttemptStatus.COMPLETED,
        charged=True,
        lease_epoch=epoch,
        created_at=status.created_at,
        observation=probe.base,
        reason_code=None,
    )


@pytest.fixture(params=["sqlite", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SQLiteStore | InMemoryRunStore:
    if request.param == "memory":
        return InMemoryRunStore(clock=clock(), ids=SequentialIds())
    return SQLiteStore(settings(tmp_path / "store.db"), clock=clock(), ids=SequentialIds())


def test_reference_idempotency_quota_and_resume(store: SQLiteStore | InMemoryRunStore) -> None:
    status = create(store)
    assert create(store) == status
    with pytest.raises(IdempotencyConflictError):
        create(store, seed=18)
    with pytest.raises(AdapterError) as error:
        create(store, key="another")
    assert error.value.code is ReasonCode.RATE_LIMITED
    assert len(store.quota_reservations(status.run_id)) == 1
    lease = store.claim("worker", 60)
    assert lease is not None
    progress = progress_for(status, meta=1)
    assert store.reserve_progress(lease, progress) == progress
    assert store.load_run(status.run_id).status.budget.used.meta_requests == 1
    store.checkpoint(lease, event_for(status))
    store.record_attempt(lease, attempt_for(status))
    store.commit_probe(lease, probe_for(receipt_for(status)))
    record = store.load_run(status.run_id)
    assert record.status.status is RunState.FETCHING
    assert record.status.attempts_used == 1
    assert len(record.probes) == len(record.attempts) == len(record.events) == 1
    sealed = receipt_for(status)
    store.finish(lease, sealed, None)
    assert store.get_receipt(status.run_id) == sealed
    assert store.get_status(status.run_id).status is RunState.COMPLETED  # type: ignore[union-attr]
    assert store.claim("worker", 60) is None
    replay = store.create_replay(status.run_id, ReplayRequest(probe_no=1), idempotency_key="replay")
    assert store.load_run(replay.run_id).parent_receipt_sha256 == sealed.receipt_sha256
    assert store.load_run(replay.run_id).run_kind is RunKind.REPLAY
    assert len(store.quota_reservations(replay.run_id)) == 1
    assert (
        store.create_replay(status.run_id, ReplayRequest(probe_no=1), idempotency_key="replay")
        == replay
    )


@pytest.mark.parametrize(
    "write", ["renew", "checkpoint", "record_attempt", "commit_probe", "finish", "reserve_progress"]
)
@pytest.mark.parametrize("fence", ["expired", "epoch", "owner", "terminal"])
def test_every_write_fenced(tmp_path: Path, write: str, fence: str) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=timer, ids=SequentialIds())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    if fence in ("expired", "epoch"):
        timer._now += timedelta(seconds=61)
    if fence == "epoch":
        replacement = store.claim("worker", 60)
        assert replacement is not None
        assert replacement.epoch == lease.epoch + 1
    if fence == "owner":
        lease = lease.model_copy(update={"owner": "other"})
    if fence == "terminal":
        store.finish(lease, receipt_for(status), None)
    before = store.load_run(status.run_id)

    def stale_write() -> None:
        if write == "renew":
            store.renew(lease, 60)
        elif write == "checkpoint":
            store.checkpoint(lease, event_for(status))
        elif write == "record_attempt":
            store.record_attempt(lease, attempt_for(status))
        elif write == "commit_probe":
            store.commit_probe(lease, probe_for(receipt_for(status)))
        elif write == "reserve_progress":
            store.reserve_progress(lease, progress_for(status))
        else:
            store.finish(lease, receipt_for(status), None)

    with pytest.raises(LeaseLostError):
        stale_write()
    assert store.load_run(status.run_id) == before


def test_migrations_reopen_and_empty(tmp_path: Path) -> None:
    config = settings(tmp_path / "nested" / "store.db")
    store = create_store(config)
    assert store.ready()
    assert store.claim("worker", 60) is None
    assert store.get_status("missing") is None
    assert store.get_receipt("missing") is None
    assert not store.quota_reservations("missing")
    with pytest.raises(KeyError):
        store.load_run("missing")
    with pytest.raises(AdapterError) as error:
        store.create_replay("missing", ReplayRequest(probe_no=1), idempotency_key="key")
    assert error.value.code is ReasonCode.REPLAY_UNAVAILABLE
    status = create(store)
    reopened = create_store(config)
    assert reopened.get_status(status.run_id) == status
    with reopened._connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchall()[0][0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_renewal_and_reclaim(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=timer)
    create(store)
    lease = store.claim("worker", 10)
    assert lease is not None
    assert store.claim("other", 10) is None
    timer._now += timedelta(seconds=9)
    renewed = store.renew(lease, 10)
    assert renewed.expires_at > lease.expires_at
    timer._now += timedelta(seconds=2)
    assert store.claim("other", 10) is None
    timer._now += timedelta(seconds=9)
    reclaimed = store.claim("other", 10)
    assert reclaimed is not None
    assert reclaimed.epoch == 2


def test_quotas_hourly_global_and_bucket(tmp_path: Path) -> None:
    timer = clock()
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=timer)
    first = create(store)
    create(store, key="two", scope="acp:two")
    assert not store.reserve_quota("acp:three", RunKind.VERIFICATION).allowed
    assert not store.reserve_quota("acp:test", RunKind.VERIFICATION).allowed
    assert store.quota_reservations(first.run_id)[0].requester_scope == "acp:test"
    store = SQLiteStore(
        settings(tmp_path / "hour.db").model_copy(
            update={"quota": QuotaPolicy(accepted_runs_per_hour=2)}
        ),
        clock=timer,
    )
    for index in range(2):
        status = create(store, key=str(index))
        lease = store.claim("worker", 60)
        assert lease is not None
        store.finish(lease, receipt_for(status), None)
    decision = store.reserve_quota("acp:test", RunKind.REPLAY)
    assert not decision.allowed
    assert decision.retry_after_seconds == 60
    timer._now += timedelta(hours=1, seconds=1)
    assert store.reserve_quota("acp:test", RunKind.REPLAY).allowed
    assert create(store, key="new")


def test_settings_budget_and_disabled_intake(tmp_path: Path) -> None:
    config = settings(tmp_path / "store.db")
    budget = config.budget.model_copy(update={"max_sandbox_invocations": 1, "max_pairs": 1})
    store = SQLiteStore(config.model_copy(update={"budget": budget}), clock=clock())
    status = create(store)
    assert status.budget.limits == budget
    lease = store.claim("worker", 60)
    assert lease is not None
    store.record_attempt(lease, attempt_for(status))
    second = attempt_for(status).model_copy(
        update={
            "attempt_id": SequentialIds().new_attempt_id(),
            "observation": None,
            "status": AttemptStatus.RESERVED,
        }
    )
    with pytest.raises(ValueError):
        store.record_attempt(lease, second)
    assert len(store.load_run(status.run_id).attempts) == 1
    disabled = SQLiteStore(
        settings(tmp_path / "disabled.db").model_copy(update={"enable_intake": False}),
        clock=clock(),
    )
    with pytest.raises(AdapterError):
        create(disabled)


def test_transaction_rollbacks(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_observation BEFORE INSERT ON observations "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.commit_probe(lease, probe_for(receipt_for(status)))
    assert not store.load_run(status.run_id).probes
    sealed = receipt_for(status, live=True)
    revision = sealed.pinned_revision
    assert revision is not None
    check = CheckIntent(
        run_id=status.run_id,
        owner=revision.owner,
        repo=revision.repo,
        repo_id=revision.repo_id,
        installation_id=FixtureRepositoryValidator().resolve(URL)[0].installation_id,
        head_sha=revision.head_sha,
        external_id=status.run_id,
        name="ProofHunter",
        status="completed",
        conclusion=CheckConclusion.FAILURE,
        title="Test",
        summary="Test",
        receipt_sha256=str(sealed.receipt_sha256),
        execution_mode="live",
    )
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_outbox BEFORE INSERT ON check_outbox "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.finish(lease, sealed, check)
    assert store.get_receipt(status.run_id) is None
    assert store.get_status(status.run_id) == status
    with store._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM check_outbox").fetchone()[0] == 0
        db.execute("DROP TRIGGER reject_outbox")
    store.finish(lease, sealed, check)
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM check_outbox").fetchone()[0] == 1
    assert store.get_receipt(status.run_id) == sealed


def test_duplicate_probe_and_invalid_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    event = event_for(status)
    with pytest.raises(ValueError):
        store.checkpoint(lease, event.model_copy(update={"seq": 2}))
    assert not store.load_run(status.run_id).events
    store.checkpoint(lease, event)
    probe = probe_for(receipt_for(status))
    store.commit_probe(lease, probe)
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_probe(lease, probe.model_copy(update={"probe_no": 2}))
    assert len(store.load_run(status.run_id).probes) == 1
    with pytest.raises(ValueError):
        store.finish(
            lease, receipt_for(status).model_copy(update={"receipt_sha256": "0" * 64}), None
        )
    assert store.get_receipt(status.run_id) is None


def test_finish_rejects_cross_run_provenance(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    unsealed = receipt_for(status).model_copy(update={"seed": 18, "receipt_sha256": None})
    incorrect = unsealed.model_copy(update={"receipt_sha256": compute_receipt_sha256(unsealed)})
    with pytest.raises(ValueError, match="provenance"):
        store.finish(lease, incorrect, None)
    assert store.get_receipt(status.run_id) is None


def test_concurrent_idempotent_intake(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: create(store), range(8)))
    assert len({status.run_id for status in results}) == 1
    assert len(store.quota_reservations(results[0].run_id)) == 1


def test_busy_retry_is_bounded(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            create(store)
        db.rollback()
    assert create(store)


def client_for(
    store: SQLiteStore,
    *,
    webhook: FixtureWebhookVerifier | None = None,
    config: Settings | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            ApiDependencies(
                settings=config or store.settings,
                store=store,
                validator=FixtureRepositoryValidator(),
                webhook=webhook,
                clock=store.clock,
            )
        )
    )


SERVICE = {"Authorization": "Bearer test-service", "Idempotency-Key": "test-message"}
OPERATOR = {"Authorization": "Bearer test-operator", "Idempotency-Key": "test-replay"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, OPERATOR])
def test_http_service_auth(tmp_path: Path, headers: dict[str, str]) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    client = client_for(store)
    response = client.post("/v1/runs", json={"pr_url": URL}, headers=headers)
    assert response.status_code == 401
    assert ErrorEnvelope.model_validate(response.json()).error.code is ReasonCode.UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"


def test_http_create_status_errors_quota(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    client = client_for(store)
    body = {"pr_url": URL}
    response = client.post("/v1/runs", json=body, headers=SERVICE)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert client.post("/v1/runs", json=body, headers=SERVICE).json() == response.json()
    assert client.post("/v1/runs", json={**body, "seed": 18}, headers=SERVICE).status_code == 409
    assert client.get(f"/v1/runs/{run_id}", headers=SERVICE).json() == response.json()
    pending = client.get(f"/v1/runs/{run_id}/receipt", headers=SERVICE)
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "receipt_pending"
    limited = client.post("/v1/runs", json=body, headers={**SERVICE, "Idempotency-Key": "second"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    for path in ("/v1/runs/missing", "/v1/runs/missing/receipt", "/missing"):
        missing = client.get(path, headers=SERVICE)
        assert missing.status_code == 404
        ErrorEnvelope.model_validate(missing.json())
    invalid = client.post("/v1/runs", json={"pr_url": "not github"}, headers=SERVICE)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_pr_url"
    unenrolled = client.post(
        "/v1/runs", json={"pr_url": URL.replace("checkout-demo", "unknown")}, headers=SERVICE
    )
    assert unenrolled.status_code == 403
    for content in ({"pr_url": URL, "seed": True}, {"pr_url": URL, "extra": 1}, {}):
        bad = client.post("/v1/runs", json=content, headers=SERVICE)
        assert bad.status_code == 422
        ErrorEnvelope.model_validate(bad.json())
    malformed = client.post("/v1/runs", content=b"{", headers=SERVICE)
    assert malformed.status_code == 422
    not_allowed = client.delete(f"/v1/runs/{run_id}", headers=SERVICE)
    assert not_allowed.status_code == 405
    ErrorEnvelope.model_validate(not_allowed.json())


@pytest.mark.parametrize("key", [None, "", "x" * 129, "bad\tkey"])
def test_http_idempotency_key_required(tmp_path: Path, key: str | None) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    headers = {"Authorization": "Bearer test-service"}
    if key is not None:
        headers["Idempotency-Key"] = key
    assert (
        client_for(store).post("/v1/runs", json={"pr_url": URL}, headers=headers).status_code == 422
    )


def test_http_sender_scope_and_global_cap(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    client = client_for(store)
    ids = []
    for sender in ("one", "two", "three"):
        response = client.post(
            "/v1/runs", json={"pr_url": URL}, headers={**SERVICE, "X-ProofHunter-Requester": sender}
        )
        if sender == "three":
            assert response.status_code == 429
        else:
            assert response.status_code == 202
            ids.append(response.json()["run_id"])
    assert len(set(ids)) == 2


def test_http_public_receipt_and_replay(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    status = create(store)
    lease = store.claim("worker", 60)
    assert lease is not None
    store.commit_probe(lease, probe_for(receipt_for(status)))
    store.finish(lease, receipt_for(status), None)
    client = client_for(store)
    response = client.get(f"/v1/runs/{status.run_id}/receipt", headers=SERVICE)
    assert response.status_code == 200
    sealed = ReceiptV1.model_validate(response.json())
    assert response.headers["etag"] == f'"{sealed.receipt_sha256}"'
    for field in (
        "installation_id",
        "sandbox_id",
        "provider_request_id",
        "operator-sandbox",
        "operator-request",
    ):
        assert field not in response.text
        assert field not in client.get(f"/v1/runs/{status.run_id}", headers=SERVICE).text
    assert (
        client.get(
            f"/v1/runs/{status.run_id}/receipt",
            headers={**SERVICE, "If-None-Match": response.headers["etag"]},
        ).status_code
        == 304
    )
    path = f"/v1/runs/{status.run_id}/replays"
    assert client.post(path, json={"probe_no": 1}, headers=SERVICE).status_code == 401
    assert client.post(path, json={"probe_no": 1}).status_code == 401
    replay = client.post(path, json={"probe_no": 1}, headers=OPERATOR)
    assert replay.status_code == 202
    assert client.post(path, json={"probe_no": 1}, headers=OPERATOR).json() == replay.json()
    assert client.post(path, json={"probe_no": 2}, headers=OPERATOR).status_code == 409
    assert (
        client.post("/v1/runs/unknown/replays", json={"probe_no": 1}, headers=OPERATOR).status_code
        == 409
    )


def test_body_limit_and_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    client = client_for(store)
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 503
    content = json.dumps({"pr_url": URL}).encode()
    assert (
        client.post(
            "/v1/runs",
            content=content + b" " * (8192 - len(content)),
            headers={**SERVICE, "Content-Type": "application/json"},
        ).status_code
        == 202
    )
    large = client.post("/v1/runs", content=iter([b"x" * 4096, b"y" * 4097]), headers=SERVICE)
    assert large.status_code == 413
    ErrorEnvelope.model_validate(large.json())
    monkeypatch.setattr(store, "ready", lambda: False)
    assert client.get("/readyz").status_code == 503
    unconfigured = client_for(store, config=store.settings.model_copy(update={"api_token": None}))
    assert unconfigured.get("/readyz").status_code == 503
    assert unconfigured.post("/v1/runs", json={"pr_url": URL}, headers=SERVICE).status_code == 401


def test_internal_error_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())

    def fail(run_id: str) -> RunStatus | None:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(store, "get_status", fail)
    response = client_for(store).get("/v1/runs/any", headers=SERVICE)
    assert response.status_code == 500
    assert "sensitive" not in response.text
    assert ErrorEnvelope.model_validate(response.json()).error.code is ReasonCode.INTERNAL_ERROR


def test_webhook_signature_parse_enqueue_dedupe_and_limits(tmp_path: Path) -> None:
    store = SQLiteStore(settings(tmp_path / "store.db"), clock=clock())
    verifier = FixtureWebhookVerifier()
    assert client_for(store, webhook=verifier).post("/webhooks/github").status_code == 404
    enabled = store.settings.model_copy(
        update={"github": store.settings.github.model_copy(update={"enable_webhook": True})}
    )
    assert client_for(store, config=enabled).post("/webhooks/github").status_code == 404
    client = client_for(store, webhook=verifier, config=enabled)
    target, _ = FixtureRepositoryValidator().resolve(URL)
    body = {
        "action": "opened",
        "installation": {"id": target.installation_id},
        "repository": {"id": target.repo_id, "name": target.repo, "owner": {"login": target.owner}},
        "pull_request": {
            "number": target.number,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
        },
    }
    raw = json.dumps(body).encode()

    def send(data: bytes, delivery: str = "delivery", event: str = "pull_request") -> Response:
        result = client.post(
            "/webhooks/github",
            content=data,
            headers={
                "X-Hub-Signature-256": verifier.sign(data),
                "X-GitHub-Delivery": delivery,
                "X-GitHub-Event": event,
            },
        )
        assert isinstance(result, Response)
        return result

    assert client.post("/webhooks/github", content=b"{").status_code == 401
    assert send(b"{").status_code == 400
    assert send(b"[]").status_code == 400
    assert send(b"{}").status_code == 200
    assert send(b"{}", event="ping").status_code == 200
    assert send(b"x" * (256 * 1024 + 1)).status_code == 413
    assert send(raw).status_code == 202
    assert send(raw).status_code == 200
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    denied = send(raw, "another-delivery")
    assert denied.status_code == 429
    with store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 1
