from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fixtures.adapters import FixtureMeta, FixtureRepositoryValidator, FixtureRunner
from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.api import create_app
from proofhunter.cli import main, replay_receipt
from proofhunter.config import load_settings, mode_config_fingerprint
from proofhunter.pipeline import VerificationPipeline, WallClock, compose
from proofhunter.ports import AdapterError, ApiDependencies, LeaseLostError
from proofhunter.store import create_store
from proofhunter.worker import VerificationWorker


def setup_run(tmp_path: Path, case: str = "buggy") -> tuple[VerificationPipeline, Any, str]:
    settings = load_settings(
        {"PROOFHUNTER_PROFILE": "offline", "PROOFHUNTER_DB_PATH": str(tmp_path / "runs.db")}
    )
    store = create_store(settings)
    pipeline = compose(settings, store)
    number = {"buggy": 1, "fixed": 2, "half_even": 3, "existing": 4}[case]
    request = c.RunRequest(
        pr_url=f"https://github.com/proofhunter-demo/checkout-demo/pull/{number}", seed=17
    )
    target, enrollment = FixtureRepositoryValidator().resolve(request.pr_url)
    status = store.create_run(
        request,
        idempotency_key="test",
        requester_scope="test",
        target=target,
        enrollment=enrollment,
    )
    return pipeline, store, status.run_id


def normalized(value: Any) -> Any:
    ignored = {
        "run_id",
        "attempt_id",
        "created_at",
        "completed_at",
        "resolved_at",
        "started_at",
        "deadline_at",
        "duration_ms",
        "wall_clock_ms",
        "latency_ms",
        "receipt_sha256",
        "command",
    }
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items() if key not in ignored}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


@pytest.mark.parametrize(
    ("case", "verdict"),
    [
        ("buggy", "confirmed_regression"),
        ("fixed", "no_defect_found"),
        ("half_even", "confirmed_regression"),
        ("existing", "confirmed_existing_defect"),
    ],
)
async def test_demo(tmp_path: Path, case: str, verdict: str) -> None:
    pipeline, store, run_id = setup_run(tmp_path, case)
    worker = VerificationWorker(pipeline)
    assert await worker.run_once()
    assert not await worker.run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict == verdict
    assert oracle.verify_receipt(receipt.model_dump_json()) == receipt
    assert receipt.budget.used.sandbox_invocations <= 15
    assert receipt.budget.used.pairs <= 6
    assert receipt.budget.used.confirmations <= 1
    assert receipt.budget.used.meta_requests <= 2
    assert receipt.budget.used.snap_requests <= 3
    assert receipt.budget.used.candidates_generated <= 64
    if case == "buggy":
        probe = receipt.probes[receipt.counterexample_probe_no - 1]
        assert probe.candidate.prices_cents == [15, 15, 15]
        assert (
            probe.oracle.expected_cents,
            probe.base.value,
            probe.head.value,
            probe.confirmation.value,
        ) == (41, 41, 42, 42)
        await replay_receipt(pipeline, receipt.model_dump_json().encode(), None)
    if case == "fixed":
        assert receipt.coverage.complete
    events = store.load_run(run_id).events
    assert [
        event.to_state.value for event in events if event.kind is c.RunEventKind.STATE_CHANGED
    ] == ["fetching", "planning", "testing", "finalizing"]


@pytest.mark.parametrize("case", ["buggy", "fixed"])
async def test_determinism_and_golden(tmp_path: Path, case: str) -> None:
    results = []
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        pipeline, store, run_id = setup_run(directory, case)
        await VerificationWorker(pipeline).run_once()
        results.append(normalized(store.get_receipt(run_id).model_dump(mode="json")))
    assert results[0] == results[1]
    golden = normalized(json.loads(Path(f"fixtures/demo/receipts/{case}.seed17.json").read_text()))
    for probe in golden["probes"]:
        for side in ("base", "head", "confirmation"):
            if probe[side] is not None:
                probe[side]["stdout_sha256"] = None
                probe[side]["stderr_sha256"] = None
    assert golden == results[0]


@pytest.mark.parametrize("fault", [c.ObservationOutcome.TIMEOUT, c.ObservationOutcome.INVALID, 43])
async def test_runner_faults(tmp_path: Path, fault: c.ObservationOutcome | int) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    side = c.Side.CONFIRM if fault == 43 else c.Side.HEAD
    pipeline.runner = FixtureRunner(faults={(side, None): fault})
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict is c.Verdict.INCONCLUSIVE
    if fault == 43:
        assert receipt.reason_code is c.ReasonCode.NONDETERMINISTIC
    else:
        expected = (
            c.ExclusionReason.INVALID_OUTPUT
            if fault is c.ObservationOutcome.INVALID
            else c.ExclusionReason.TIMEOUT
        )
        assert all(p.exclusion_reason is expected for p in receipt.probes)


async def test_isolation_denied(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.runner = FixtureRunner(isolation=c.IsolationStatus.UNVERIFIED)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.reason_code is c.ReasonCode.ISOLATION_UNVERIFIED
    assert receipt.budget.used.sandbox_invocations == 0


async def test_small_budget(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    record = store.load_run(run_id)
    limits = c.BudgetLimits(
        max_sandbox_invocations=1, max_meta_requests=0, max_snap_requests=0, max_candidates=1
    )
    record = replace(
        record,
        status=record.status.model_copy(
            update={"budget": record.status.budget.model_copy(update={"limits": limits})}
        ),
    )
    lease = store.claim("test", 240)
    receipt = await pipeline.run(record, lease)
    assert receipt.budget.used.sandbox_invocations == 0
    assert receipt.budget.used.meta_requests == 0
    assert receipt.budget.used.snap_requests == 0
    assert receipt.budget.used.candidates_generated == 1


async def test_unexpected_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, store, run_id = setup_run(tmp_path)

    async def broken(*args: object) -> None:
        raise RuntimeError("private-provider-detail")

    monkeypatch.setattr(pipeline.source, "pin", broken)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict is None
    assert receipt.reason_code is c.ReasonCode.INTERNAL_ERROR
    assert "private-provider-detail" not in receipt.model_dump_json()


async def test_expired_lease_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    old = store.claim("old", 1)
    pin = await pipeline.source.pin(store.load_run(run_id).target)
    store.reserve_progress(
        old,
        c.RunCheckpoint(
            pinned_revision=pin, provider_allocations=c.ProviderAllocations(meta=2, snap=3)
        ),
    )
    for seq, (before, after) in enumerate(
        [
            (c.RunState.QUEUED, c.RunState.FETCHING),
            (c.RunState.FETCHING, c.RunState.PLANNING),
        ],
        1,
    ):
        store.checkpoint(
            old,
            c.RunEvent(
                run_id=run_id,
                seq=seq,
                at=WallClock().now(),
                kind=c.RunEventKind.STATE_CHANGED,
                from_state=before,
                to_state=after,
                reason_code=None,
                detail=None,
            ),
        )
    calls = []

    async def forbidden(*args: object) -> None:
        calls.append("forbidden")
        raise AssertionError("recovery must not call external ports")

    monkeypatch.setattr(pipeline.source, "pin", forbidden)
    monkeypatch.setattr(pipeline.source, "fetch", forbidden)
    monkeypatch.setattr(pipeline.meta, "propose", forbidden)
    monkeypatch.setattr(pipeline.route, "choose", forbidden)
    monkeypatch.setattr(pipeline.runner, "attest", forbidden)
    monkeypatch.setattr(pipeline.runner, "observe", forbidden)
    now = WallClock().now() + timedelta(seconds=2)

    class LaterClock:
        def now(self) -> datetime:
            return now

    store.clock = LaterClock()
    with pytest.raises(LeaseLostError):
        store.renew(old, 240)
    assert await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict is c.Verdict.INCONCLUSIVE
    assert receipt.reason_code is c.ReasonCode.REVISION_UNAVAILABLE
    assert calls == []
    assert receipt.budget.used.sandbox_invocations == 0
    assert receipt.budget.used.meta_requests == 2
    assert receipt.budget.used.snap_requests == 3
    assert receipt.pinned_revision == c.public_revision(pin)


async def test_store_replay_bypasses_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    await VerificationWorker(pipeline).run_once()
    parent = store.get_receipt(run_id)
    status = store.create_replay(
        run_id, c.ReplayRequest(probe_no=parent.counterexample_probe_no), idempotency_key="replay"
    )

    async def forbidden(*args: object) -> None:
        raise AssertionError("provider must not execute during replay")

    monkeypatch.setattr(pipeline.meta, "propose", forbidden)
    monkeypatch.setattr(pipeline.route, "choose", forbidden)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(status.run_id)
    assert receipt.verdict is c.Verdict.CONFIRMED_REGRESSION
    assert receipt.run_kind is c.RunKind.REPLAY
    assert receipt.parent_receipt_sha256 == parent.receipt_sha256


async def test_meta_fallback(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.meta = FixtureMeta(fail_with=c.ReasonCode.PROVIDER_UNAVAILABLE)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.providers[c.ProviderName.META].mode is c.ProviderMode.FALLBACK


async def test_unavailable_oracle(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.enrollment = pipeline.enrollment.model_copy(update={"enabled": False})
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.reason_code is c.ReasonCode.ORACLE_UNAVAILABLE
    assert receipt.budget.used.sandbox_invocations == 0


async def test_lease_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)

    def lost(*args: object) -> None:
        raise LeaseLostError()

    monkeypatch.setattr(store, "record_attempt", lost)
    assert await VerificationWorker(pipeline).run_once()
    assert store.get_receipt(run_id) is None


async def test_offline_finish_never_has_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, store, _ = setup_run(tmp_path)
    finish = store.finish
    intents = []

    def capture(lease: c.RunLease, receipt: c.ReceiptV1, check: c.CheckIntent | None) -> None:
        intents.append(check)
        finish(lease, receipt, check)

    monkeypatch.setattr(store, "finish", capture)
    await VerificationWorker(pipeline).run_once()
    assert intents == [None]


async def test_replay_refuses_missing_artifact_and_nonreproduction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    await VerificationWorker(pipeline).run_once()
    raw = store.get_receipt(run_id).model_dump_json().encode()
    pipeline.runner = FixtureRunner(faults={(c.Side.HEAD, None): 99})
    with pytest.raises(ValueError, match="did not reproduce"):
        await replay_receipt(pipeline, raw, None)

    async def missing(*args: object) -> None:
        raise AdapterError(
            c.ProviderName.GITHUB,
            c.ReasonCode.REVISION_UNAVAILABLE,
            "missing artifact",
            retryable=False,
        )

    monkeypatch.setattr(pipeline.source, "fetch", missing)
    with pytest.raises(AdapterError):
        await replay_receipt(pipeline, raw, None)


async def test_source_error_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)

    async def unavailable(*args: object) -> None:
        raise AdapterError(
            c.ProviderName.GITHUB, c.ReasonCode.REVISION_UNAVAILABLE, "sanitized", retryable=False
        )

    monkeypatch.setattr(pipeline.source, "pin", unavailable)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict is c.Verdict.INCONCLUSIVE
    assert receipt.probes == []


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROOFHUNTER_DB_PATH", raising=False)
    receipt = tmp_path / "receipt.json"
    assert (
        main(["demo", "--offline", "--case", "buggy", "--seed", "17", "--receipt", str(receipt)])
        == 0
    )
    assert (
        main(["replay", "--offline", "--receipt", str(receipt), "--probe", "counterexample"]) == 0
    )
    data = json.loads(receipt.read_text())
    data["seed"] = 18
    receipt.write_text(json.dumps(data))
    assert main(["replay", "--offline", "--receipt", str(receipt)]) == 1
    assert main(["replay", "--profile", "live", "--receipt", str(receipt)]) == 1
    assert main(["replay", "--offline", "--receipt", str(tmp_path / "missing")]) == 1


async def test_api_worker(tmp_path: Path) -> None:
    from pydantic import SecretStr

    pipeline, store, _ = setup_run(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "api_token": SecretStr("test-service"),
            "operator_token": SecretStr("test-operator"),
        }
    )
    app = create_app(
        ApiDependencies(settings, store, FixtureRepositoryValidator(), None, WallClock())
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-service", "Idempotency-Key": "test"}
        response = client.post(
            "/v1/runs",
            headers=headers,
            json={"pr_url": "https://github.com/proofhunter-demo/checkout-demo/pull/2", "seed": 17},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        worker = VerificationWorker(pipeline)
        await worker.run_once()
        await worker.run_once()
        response = client.get(f"/v1/runs/{run_id}/receipt", headers=headers)
        assert response.status_code == 200
        assert response.json()["verdict"] == "no_defect_found"


async def test_readyz_heartbeat(tmp_path: Path) -> None:
    from pydantic import SecretStr

    pipeline, store, _ = setup_run(tmp_path)
    settings = pipeline.settings.model_copy(
        update={
            "api_token": SecretStr("test-service"),
            "operator_token": SecretStr("test-operator"),
        }
    )
    fingerprint = mode_config_fingerprint(settings, [pipeline.enrollment])
    app = create_app(
        ApiDependencies(
            settings,
            store,
            FixtureRepositoryValidator(),
            None,
            WallClock(),
            heartbeat_store=store,
            config_fingerprint=fingerprint,
        )
    )
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        worker = VerificationWorker(pipeline, heartbeat_store=store, config_fingerprint=fingerprint)
        await worker.run_once()
        assert client.get("/readyz").status_code == 200
        worker.config_fingerprint = "f" * 64
        worker.heartbeat()
        assert client.get("/readyz").status_code == 503
        worker.config_fingerprint = fingerprint
        worker.heartbeat()
        now = WallClock().now() + timedelta(seconds=31)

        class LaterClock:
            def now(self) -> datetime:
                return now

        store.clock = LaterClock()
        assert client.get("/readyz").status_code == 503


async def test_pin_only_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    lease = store.claim("old", 1)
    pin = await pipeline.source.pin(store.load_run(run_id).target)
    store.reserve_progress(lease, c.RunCheckpoint(pinned_revision=pin))
    now = WallClock().now() + timedelta(seconds=2)

    class LaterClock:
        def now(self) -> datetime:
            return now

    store.clock = LaterClock()

    async def moved(*args: object) -> None:
        raise AssertionError("must not resolve moving head")

    monkeypatch.setattr(pipeline.source, "pin", moved)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.verdict is c.Verdict.CONFIRMED_REGRESSION
    assert receipt.pinned_revision == c.public_revision(pin)


async def test_legacy_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    load = store.load_run
    monkeypatch.setattr(store, "load_run", lambda key: replace(load(key), checkpoint=None))

    async def forbidden(*args: object) -> None:
        raise AssertionError("legacy must not perform I/O")

    for port, methods in (
        (pipeline.source, ("pin", "fetch")),
        (pipeline.meta, ("propose",)),
        (pipeline.route, ("choose",)),
        (pipeline.runner, ("attest", "observe")),
    ):
        for method in methods:
            monkeypatch.setattr(port, method, forbidden)
    await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.reason_code is c.ReasonCode.REVISION_UNAVAILABLE
    assert receipt.budget.used.meta_requests == 2
    assert receipt.budget.used.snap_requests == 3


async def test_reservation_order_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.meta_request_allowance = 2
    pipeline.route_requests_per_call = 2
    propose, choose = pipeline.meta.propose, pipeline.route.choose

    async def meta(context: Any) -> c.HypothesisPlan:
        checkpoint = store.load_run(run_id).checkpoint
        assert checkpoint.pinned_revision is not None
        assert checkpoint.provider_allocations.meta == 2
        return await propose(context)

    async def route(state: c.DecisionState, eligible: Any) -> c.RouteDecision:
        checkpoint = store.load_run(run_id).checkpoint
        assert checkpoint.pinned_revision is not None
        assert checkpoint.provider_allocations.snap >= state.snap_requests_remaining
        assert state.snap_requests_remaining >= 2
        return await choose(state, eligible)

    monkeypatch.setattr(pipeline.meta, "propose", meta)
    monkeypatch.setattr(pipeline.route, "choose", route)
    await VerificationWorker(pipeline).run_once()
    assert store.get_receipt(run_id).verdict is c.Verdict.CONFIRMED_REGRESSION


async def test_reservation_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    calls = []

    def broken(*args: object) -> None:
        raise RuntimeError("lost acknowledgment")

    async def forbidden(*args: object) -> None:
        calls.append(True)

    monkeypatch.setattr(store, "reserve_progress", broken)
    monkeypatch.setattr(pipeline.source, "fetch", forbidden)
    monkeypatch.setattr(pipeline.meta, "propose", forbidden)
    monkeypatch.setattr(pipeline.route, "choose", forbidden)
    await VerificationWorker(pipeline).run_once()
    assert store.get_receipt(run_id).reason_code is c.ReasonCode.INTERNAL_ERROR
    assert calls == []


async def test_active_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline, store, _ = setup_run(tmp_path)
    monkeypatch.setattr(c, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    fingerprint = mode_config_fingerprint(pipeline.settings, [pipeline.enrollment])
    original = pipeline.runner.attest
    observed = []

    async def slow() -> c.IsolationAttestation:
        before = store.read_heartbeat(c.Profile.OFFLINE, fingerprint)
        await asyncio.sleep(0.05)
        after = store.read_heartbeat(c.Profile.OFFLINE, fingerprint)
        observed.append(after.observed_at > before.observed_at)
        return await original()

    monkeypatch.setattr(pipeline.runner, "attest", slow)
    await VerificationWorker(
        pipeline, heartbeat_store=store, config_fingerprint=fingerprint
    ).run_once()
    assert observed == [True]
