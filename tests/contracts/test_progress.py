"""Refreeze wire examples: durable pre-I/O progress and fail-closed liveness."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from fixtures import adapters, catalog
from proofhunter import config, ports
from proofhunter import contracts as c

SAMPLES = Path(__file__).resolve().parents[2] / "schemas" / "samples"


def _pin() -> c.PinnedRevision:
    return c.PinnedRevision.model_validate_json((SAMPLES / "PinnedRevision.json").read_bytes())


def _claimed(limits: c.BudgetLimits | None = None) -> tuple[
        adapters.InMemoryRunStore, adapters.SteppingClock, c.RunLease]:
    clock = adapters.SteppingClock(step=timedelta(0))
    store = adapters.InMemoryRunStore(clock=clock, budget=limits)
    url = catalog.PR_CASES["buggy"].url
    target, enrollment = adapters.FixtureRepositoryValidator().resolve(url)
    store.create_run(c.RunRequest(pr_url=url, seed=17), idempotency_key="k1",
                     requester_scope="fixture:a", target=target, enrollment=enrollment)
    lease = store.claim("worker-1", 30)
    assert lease is not None
    return store, clock, lease


def _progress(meta: int = 0, snap: int = 0) -> c.RunCheckpoint:
    return c.RunCheckpoint(pinned_revision=_pin(),
                           provider_allocations=c.ProviderAllocations(meta=meta, snap=snap))


def _heartbeat(*, ttl: int = c.HEARTBEAT_TTL_SECONDS,
               profile: c.Profile = c.Profile.OFFLINE,
               fingerprint: str = "a" * 64, worker_id: str = "worker-1",
               at: datetime = adapters.FIXTURE_EPOCH) -> c.WorkerHeartbeat:
    return c.WorkerHeartbeat(worker_id=worker_id, profile=profile,
                             config_fingerprint=fingerprint, observed_at=at,
                             expires_at=at + timedelta(seconds=ttl))


def test_additive_internal_defaults_and_protocol_examples() -> None:
    store, _, lease = _claimed()
    record = store.load_run(lease.run_id)
    assert isinstance(store, ports.RunStore)
    assert record.checkpoint == c.RunCheckpoint()
    assert record.recoverable
    assert not replace(record, checkpoint=None).recoverable
    assert isinstance(adapters.InMemoryHeartbeatStore(), ports.HeartbeatStore)
    deps = ports.ApiDependencies(config.load_settings({}), store,
                                 adapters.FixtureRepositoryValidator(), None,
                                 adapters.SteppingClock())
    assert deps.heartbeat_store is None
    assert deps.config_fingerprint is None
    assert config.Profile is c.Profile
    heartbeat_store = adapters.InMemoryHeartbeatStore()
    fingerprint = config.mode_config_fingerprint(deps.settings, [catalog.demo_enrollment()])
    wired = replace(deps, heartbeat_store=heartbeat_store, config_fingerprint=fingerprint)
    assert wired.heartbeat_store is not None
    assert wired.config_fingerprint is not None
    assert wired.heartbeat_store.read_heartbeat(wired.settings.profile,
                                               wired.config_fingerprint) is None
    wired.heartbeat_store.write_heartbeat(_heartbeat(fingerprint=fingerprint))
    assert wired.heartbeat_store.read_heartbeat(wired.settings.profile,
                                               wired.config_fingerprint) is not None


def test_reserve_pin_and_counts_before_io_and_reclaim_without_repin() -> None:
    store, clock, lease = _claimed()
    saved = store.reserve_progress(lease, _progress())
    assert saved.pinned_revision == _pin()
    store.reserve_progress(lease, _progress(meta=1))
    store.reserve_progress(lease, _progress(meta=2, snap=3))
    record = store.load_run(lease.run_id)
    assert record.checkpoint == _progress(meta=2, snap=3)
    assert record.status.budget.used.meta_requests == 2
    assert record.status.budget.used.snap_requests == 3
    assert record.status.budget.used.sandbox_invocations == 0
    clock._now = lease.expires_at
    new = store.claim("worker-2", 30)
    assert new is not None
    assert new.epoch == lease.epoch + 1
    assert store.load_run(new.run_id).checkpoint == record.checkpoint
    with pytest.raises(ports.LeaseLostError):
        store.reserve_progress(lease, _progress())


@pytest.mark.parametrize(("field", "value"), [
    ("base_sha", "a" * 40), ("head_sha", "b" * 40),
    ("resolved_at", adapters.FIXTURE_EPOCH + timedelta(seconds=1)),
    ("repo_id", 123), ("installation_id", 456), ("number", 99),
])
def test_pin_is_immutable_and_failed_write_is_atomic(field: str, value: object) -> None:
    store, _, lease = _claimed()
    store.reserve_progress(lease, _progress(meta=1))
    before = store.load_run(lease.run_id)
    changed = _pin().model_copy(update={field: value})
    with pytest.raises(ValueError, match="immutable"):
        store.reserve_progress(lease, c.RunCheckpoint(
            pinned_revision=changed, provider_allocations=c.ProviderAllocations(meta=2)))
    assert store.load_run(lease.run_id) == before


def test_pin_cannot_be_cleared_or_bound_to_another_target() -> None:
    store, _, lease = _claimed()
    with pytest.raises(ValueError, match="target"):
        store.reserve_progress(lease, c.RunCheckpoint(
            pinned_revision=_pin().model_copy(update={"repo_id": 999})))
    store.reserve_progress(lease, _progress())
    with pytest.raises(ValueError, match="immutable"):
        store.reserve_progress(lease, c.RunCheckpoint())


@pytest.mark.parametrize(("meta", "snap"), [(0, 1), (1, 0), (1, 1)])
def test_allocations_never_decrease_or_repeat(meta: int, snap: int) -> None:
    store, _, lease = _claimed()
    store.reserve_progress(lease, _progress(meta=1, snap=1))
    before = store.load_run(lease.run_id)
    with pytest.raises(ValueError):
        store.reserve_progress(lease, _progress(meta, snap))
    assert store.load_run(lease.run_id) == before


@pytest.mark.parametrize(("meta", "snap"), [(2, 0), (0, 2)])
def test_lower_run_caps_are_enforced_atomically(meta: int, snap: int) -> None:
    store, _, lease = _claimed(c.BudgetLimits(max_meta_requests=1, max_snap_requests=1))
    before = store.load_run(lease.run_id)
    with pytest.raises(ValueError, match="caps"):
        store.reserve_progress(lease, _progress(meta, snap))
    assert store.load_run(lease.run_id) == before


@pytest.mark.parametrize("counts", [
    {"meta": True}, {"snap": 1.0}, {"meta": "1"}, {"snap": -1},
    {"meta": 3}, {"snap": 4}, {"meta": 0, "unknown": 0},
])
def test_strict_provider_counters(counts: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        c.ProviderAllocations.model_validate(counts)


def test_checkpoint_requires_pin_before_provider_spending() -> None:
    with pytest.raises(ValidationError, match="pinned revision"):
        c.RunCheckpoint(provider_allocations=c.ProviderAllocations(meta=1))


@pytest.mark.parametrize("fence", ["owner", "epoch", "expired", "terminal", "legacy"])
def test_reservation_fencing(fence: str) -> None:
    store, clock, lease = _claimed()
    if fence == "owner":
        lease = lease.model_copy(update={"owner": "wrong-worker"})
    elif fence == "epoch":
        lease = lease.model_copy(update={"epoch": lease.epoch + 1})
    elif fence == "expired":
        clock._now = lease.expires_at
    elif fence == "terminal":
        record = store.load_run(lease.run_id)
        store.records[lease.run_id] = replace(
            record, status=record.status.model_copy(update={"status": c.RunState.FAILED}))
    else:
        store.records[lease.run_id] = replace(store.load_run(lease.run_id), checkpoint=None)
    before = store.load_run(lease.run_id)
    with pytest.raises(ValueError if fence == "legacy" else ports.LeaseLostError):
        store.reserve_progress(lease, _progress(meta=1))
    assert store.load_run(lease.run_id) == before


def test_concurrent_duplicate_reservation_authorizes_only_one_call() -> None:
    store, _, lease = _claimed()

    def reserve(_: int) -> bool:
        try:
            store.reserve_progress(lease, _progress(meta=1))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert store.load_run(lease.run_id).checkpoint == _progress(meta=1)


def test_heartbeat_absent_mismatched_expired_and_future_fail_closed() -> None:
    clock = adapters.SteppingClock(step=timedelta(0))
    store = adapters.InMemoryHeartbeatStore(clock)
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    store.write_heartbeat(_heartbeat())
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) == _heartbeat()
    assert store.read_heartbeat(c.Profile.LIVE, "a" * 64) is None
    assert store.read_heartbeat(c.Profile.OFFLINE, "b" * 64) is None
    clock._now += timedelta(seconds=c.HEARTBEAT_TTL_SECONDS)
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    clock._now = adapters.FIXTURE_EPOCH - timedelta(microseconds=1)
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    with pytest.raises(ValueError, match="regress"):
        store.write_heartbeat(_heartbeat())


def test_heartbeat_uses_server_time_not_producer_wall_clock() -> None:
    clock = adapters.SteppingClock(step=timedelta(0))
    store = adapters.InMemoryHeartbeatStore(clock)
    store.write_heartbeat(_heartbeat(at=datetime(2099, 1, 1, tzinfo=UTC)))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) == _heartbeat()
    clock._now += timedelta(seconds=10)
    store.write_heartbeat(_heartbeat(at=datetime(2000, 1, 1, tzinfo=UTC)))
    heartbeat = store.read_heartbeat(c.Profile.OFFLINE, "a" * 64)
    assert heartbeat is not None
    assert heartbeat.observed_at == clock._now
    assert heartbeat.expires_at == clock._now + timedelta(seconds=c.HEARTBEAT_TTL_SECONDS)
    store.write_heartbeat(_heartbeat(ttl=60))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) == heartbeat


def test_heartbeat_config_replacement_and_multiple_workers() -> None:
    clock = adapters.SteppingClock()
    store = adapters.InMemoryHeartbeatStore(clock)
    store.write_heartbeat(_heartbeat())
    store.write_heartbeat(_heartbeat(fingerprint="b" * 64))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    assert store.read_heartbeat(c.Profile.OFFLINE, "b" * 64) is not None
    store.write_heartbeat(_heartbeat(worker_id="worker-2"))
    assert store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is not None


@pytest.mark.parametrize("ttl", [-1, 0, 61])
def test_heartbeat_ttl_bounds(ttl: int) -> None:
    with pytest.raises(ValidationError, match="TTL"):
        _heartbeat(ttl=ttl)


def test_store_revalidates_copied_payloads_before_writing() -> None:
    heartbeat_store = adapters.InMemoryHeartbeatStore()
    forged = _heartbeat().model_copy(update={
        "expires_at": adapters.FIXTURE_EPOCH + timedelta(days=365)})
    with pytest.raises(ValidationError):
        heartbeat_store.write_heartbeat(forged)
    assert heartbeat_store.read_heartbeat(c.Profile.OFFLINE, "a" * 64) is None
    store, _, lease = _claimed()
    forged_progress = _progress().model_copy(update={
        "provider_allocations": c.ProviderAllocations().model_copy(update={"meta": True})})
    with pytest.raises(ValidationError):
        store.reserve_progress(lease, forged_progress)
    assert store.load_run(lease.run_id).checkpoint == c.RunCheckpoint()


def test_heartbeat_strict_fields() -> None:
    sample = _heartbeat().model_dump()
    for changes in ({"worker_id": "worker with spaces"}, {"profile": "invalid"},
                    {"config_fingerprint": "A" * 64},
                    {"observed_at": datetime(2026, 1, 1)}, {"extra": 1}):
        with pytest.raises(ValidationError):
            c.WorkerHeartbeat.model_validate({**sample, **changes})


def test_public_config_fingerprint_excludes_secrets_and_local_paths() -> None:
    settings = config.load_settings({})
    enrollment = catalog.demo_enrollment()
    fingerprint = config.mode_config_fingerprint(settings, [enrollment])
    altered = settings.model_copy(update={
        "api_token": SecretStr("fixture-service-token"),
        "operator_token": SecretStr("fixture-operator-token"),
        "db_path": Path("/private/db"), "enrollments_path": Path("/private/config"),
        "github": settings.github.model_copy(update={
            "app_id": 999, "private_key_path": SecretStr("/private/key"),
            "webhook_secret": SecretStr("fixture-webhook-secret")}),
        "meta": settings.meta.model_copy(update={"api_key": SecretStr("fixture-meta")}),
        "snap": settings.snap.model_copy(update={"api_key": SecretStr("fixture-snap")}),
        "daytona": settings.daytona.model_copy(update={"api_key": SecretStr("fixture-sdk")}),
        "agent": settings.agent.model_copy(update={
            "seed_phrase": SecretStr("fixture-seed")}),
    })
    assert config.mode_config_fingerprint(altered, [enrollment]) == fingerprint
    assert config.mode_config_fingerprint(settings, [enrollment.model_copy(
        update={"installation_id": 999})]) == fingerprint
    public = c.canonical_json(config.public_mode_config(altered, [enrollment]))
    assert b"private" not in public
    assert b"fixture-" not in public
    assert b"********" not in public
    assert len(fingerprint) == 64
    assert config.mode_config_fingerprint(settings, []) != fingerprint
    assert config.mode_config_fingerprint(settings.model_copy(
        update={"profile": c.Profile.LIVE}), [enrollment]) != fingerprint
    assert config.mode_config_fingerprint(settings.model_copy(update={
        "budget": c.BudgetLimits(max_meta_requests=1)}), [enrollment]) != fingerprint
    assert config.mode_config_fingerprint(settings.model_copy(update={
        "meta": settings.meta.model_copy(update={"model": "other-model"})}),
        [enrollment]) != fingerprint


def test_schema_wire_examples_bind_public_config_and_checkpoint() -> None:
    heartbeat = c.WorkerHeartbeat.model_validate_json(
        (SAMPLES / "WorkerHeartbeat.json").read_bytes())
    assert heartbeat.config_fingerprint == config.mode_config_fingerprint(
        config.load_settings({}), [catalog.demo_enrollment()])
    checkpoint = c.RunCheckpoint.model_validate_json((SAMPLES / "RunCheckpoint.json").read_bytes())
    assert checkpoint.pinned_revision == _pin()
    assert checkpoint.provider_allocations == c.ProviderAllocations(meta=1, snap=1)
