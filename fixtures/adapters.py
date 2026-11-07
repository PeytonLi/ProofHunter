"""Fixture implementations of every port in ``proofhunter.ports``.

All fixtures are offline: no network, no credentials, no execution of request
bytes. ``FixtureRunner`` runs only the reviewed first-party revisions under
``fixtures/demo/revisions``, selected by exact source SHA-256; unknown sources
are ``denied``. Every result is labeled ``offline_fixture``/``stub``.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import importlib.util
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType

from fixtures import catalog
from proofhunter.contracts import (
    ACTIVE_STATES,
    CONTRACT_ID,
    ENTRYPOINT,
    MAX_PROPOSALS,
    STRATEGY_FALLBACK_ORDER,
    TERMINAL_STATES,
    AttemptRecord,
    Budget,
    BudgetLimits,
    BudgetUsage,
    CheckIntent,
    ContextFile,
    CostRecord,
    CostStatus,
    DecisionState,
    Enrollment,
    ExecutionRequest,
    GithubWebhookEvent,
    HypothesisMode,
    HypothesisPlan,
    HypothesisPlanPayload,
    IsolationAttestation,
    IsolationStatus,
    ObservationOutcome,
    PinnedRevision,
    PlanningContext,
    ProbeRecord,
    Profile,
    ProviderMode,
    ProviderName,
    ProviderProvenance,
    PrTarget,
    PublishResult,
    PublishStatus,
    QuotaDecision,
    QuotaPolicy,
    QuotaReservation,
    ReasonCode,
    ReceiptV1,
    ReplayRequest,
    RouteDecision,
    RouteMode,
    RouteReasonCode,
    RunCheckpoint,
    RunEvent,
    RunEventKind,
    RunKind,
    RunLease,
    RunnerMode,
    RunRequest,
    RunState,
    RunStatus,
    SandboxObservation,
    Side,
    SourceBundle,
    SourceFile,
    Strategy,
    WebhookAction,
    WorkerHeartbeat,
    canonical_json,
    canonical_sha256,
    git_blob_sha1,
    sha256_hex,
)
from proofhunter.ports import (
    AdapterError,
    IdempotencyConflictError,
    LeaseLostError,
    RunRecord,
)

FIXTURE_EPOCH = datetime(2026, 11, 7, 19, 0, 0, tzinfo=UTC)
FIXTURE_WEBHOOK_SECRET = b"proofhunter-fixture-webhook-key"  # fixture value, not a credential


# ---------------------------------------------------------------- clock and IDs


class SteppingClock:
    """Deterministic clock: each ``now()`` advances by ``step``."""

    def __init__(self, start: datetime = FIXTURE_EPOCH,
                 step: timedelta = timedelta(milliseconds=1)) -> None:
        self._now = start
        self._step = step
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            current = self._now
            self._now = current + self._step
            return current


class SequentialIds:
    """Deterministic lowercase UUIDv4 strings from a namespace and a counter."""

    def __init__(self, namespace: int = 0x5E7) -> None:
        self._namespace = namespace
        self._counter = 0
        self._lock = threading.Lock()

    def _next(self, kind: int) -> str:
        with self._lock:
            self._counter += 1
            value = (self._namespace << 80) | (kind << 64) | self._counter
        return str(uuid.UUID(int=value, version=4))

    def new_run_id(self) -> str:
        return self._next(1)

    def new_attempt_id(self) -> str:
        return self._next(2)


# ---------------------------------------------------------------- source


def _source_file(side: str, revision: catalog.Revision) -> SourceFile:
    data = revision.source_bytes
    return SourceFile(side=side, commit_sha=revision.commit_sha, path="checkout.py",  # type: ignore[arg-type]
                      blob_sha=git_blob_sha1(data), content=data.decode("utf-8"),
                      content_sha256=sha256_hex(data), size_bytes=len(data))


def planning_context(run_id: str, base: SourceFile, head: SourceFile) -> PlanningContext:
    diff = "".join(difflib.unified_diff(
        base.content.splitlines(keepends=True), head.content.splitlines(keepends=True),
        fromfile="a/checkout.py", tofile="b/checkout.py"))
    return PlanningContext(
        run_id=run_id, contract_id=CONTRACT_ID, entrypoint=ENTRYPOINT,
        contract_text=catalog.contract_text(),
        files=[ContextFile(path="checkout.py", start_line=1, text=head.content)],
        diff=diff, supported_modes=list(HypothesisMode), max_hypotheses=MAX_PROPOSALS)


class FixtureSource:
    """``SourcePort`` over ``catalog.PR_CASES`` in the demo repo."""

    def __init__(self, clock: SteppingClock | None = None, *,
                 head_overrides: Mapping[int, str] | None = None) -> None:
        self._clock = clock or SteppingClock()
        self._head_overrides = dict(head_overrides or {})

    async def pin(self, target: PrTarget) -> PinnedRevision:
        case = catalog.case_by_number(target.number)
        if (target.repo_id != catalog.DEMO_REPO_ID
                or target.full_name.lower() != catalog.DEMO_REPO_FULL_NAME):
            raise AdapterError(ProviderName.GITHUB, ReasonCode.REPO_NOT_ENROLLED,
                               "repository is not enrolled", retryable=False)
        if case is None:
            raise AdapterError(ProviderName.GITHUB, ReasonCode.REVISION_UNAVAILABLE,
                               "pull request not found", retryable=False)
        head = self._head_overrides.get(target.number, case.head)
        return PinnedRevision(
            owner=target.owner, repo=target.repo, number=target.number, repo_id=target.repo_id,
            installation_id=target.installation_id, base_sha=case.base_revision.commit_sha,
            head_sha=catalog.revisions()[head].commit_sha, resolved_at=self._clock.now())

    async def fetch(self, revision: PinnedRevision, enrollment: Enrollment) -> SourceBundle:
        if enrollment.repo_id != revision.repo_id:
            raise AdapterError(ProviderName.GITHUB, ReasonCode.REPO_NOT_ENROLLED,
                               "enrollment does not match revision", retryable=False)
        by_sha = {r.commit_sha: r for r in catalog.revisions().values()}
        try:
            base = _source_file("base", by_sha[revision.base_sha])
            head = _source_file("head", by_sha[revision.head_sha])
        except KeyError as error:
            raise AdapterError(ProviderName.GITHUB, ReasonCode.REVISION_UNAVAILABLE,
                               "revision unavailable", retryable=False) from error
        run_id = str(uuid.UUID(int=0, version=4))
        return SourceBundle(revision=revision, base=base, head=head,
                            context=planning_context(run_id, base, head))


# ---------------------------------------------------------------- meta


def load_plan_payload(name: str = "meta_plan_payload.json") -> HypothesisPlanPayload:
    return HypothesisPlanPayload.model_validate_json(
        (catalog.PROVIDERS_DIR / name).read_bytes())


class FixtureMeta:
    """``MetaPort`` returning the recorded fixture plan, or raising on demand."""

    def __init__(self, payload: HypothesisPlanPayload | None = None, *,
                 fail_with: ReasonCode | None = None) -> None:
        self._payload = payload
        self._fail_with = fail_with
        self.calls: list[PlanningContext] = []

    async def propose(self, context: PlanningContext) -> HypothesisPlan:
        self.calls.append(context)
        if self._fail_with is not None:
            raise AdapterError(ProviderName.META, self._fail_with, "fixture meta failure",
                               retryable=self._fail_with is ReasonCode.PROVIDER_TIMEOUT)
        payload = self._payload or load_plan_payload()
        for proposal in payload.hypotheses:
            for ref in proposal.source_refs:
                if not context.contains_ref(ref):
                    raise AdapterError(ProviderName.META, ReasonCode.PROVIDER_INVALID_RESPONSE,
                                       "source ref outside supplied context", retryable=False)
        return HypothesisPlan(hypotheses=payload.hypotheses, provenance=ProviderProvenance(
            provider=ProviderName.META, mode=ProviderMode.OFFLINE_FIXTURE, model_id=None,
            request_id=None, latency_ms=0, attempts=0, reason_code=None))


# ---------------------------------------------------------------- snap


class FixtureRoute:
    """``RoutePort`` with scripted ``stub`` choices for the first ``provider_steps``
    steps, then the deterministic ``policy`` fallback (``call_budget``)."""

    def __init__(self, script: Sequence[Strategy] = (Strategy.CONSTRAINT, Strategy.BOUNDARY),
                 *, provider_steps: int = 2) -> None:
        self._script = list(script)
        self._provider_steps = provider_steps
        self.states: list[DecisionState] = []

    async def choose(self, state: DecisionState, eligible: tuple[Strategy, ...]) -> RouteDecision:
        if not eligible:
            raise ValueError("eligible must be non-empty")
        self.states.append(state)
        offered = list(eligible)
        if len(offered) == 1:
            return RouteDecision(choice=offered[0], eligible=offered, mode=RouteMode.POLICY,
                                 model_id=None, probabilities=None, confidence=None,
                                 provider_request_id=None, latency_ms=None,
                                 reason_code=RouteReasonCode.SINGLE_ELIGIBLE)
        index = state.step - 1
        if index < min(self._provider_steps, len(self._script)) and (
                self._script[index] in offered):
            choice = self._script[index]
            probs = {s: (0.6 if s is choice else round(0.4 / (len(offered) - 1), 12))
                     for s in offered}
            return RouteDecision(choice=choice, eligible=offered, mode=RouteMode.STUB,
                                 model_id=None, probabilities=probs, confidence=0.6,
                                 provider_request_id=None, latency_ms=0,
                                 reason_code=RouteReasonCode.STUB_CHOICE)
        fallback = next(s for s in STRATEGY_FALLBACK_ORDER if s in offered)
        return RouteDecision(choice=fallback, eligible=offered, mode=RouteMode.POLICY,
                             model_id=None, probabilities=None, confidence=None,
                             provider_request_id=None, latency_ms=None,
                             reason_code=RouteReasonCode.CALL_BUDGET)


# ---------------------------------------------------------------- runner


@cache
def _load_revision_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_ph_fixture_{path.parent.name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Fault = ObservationOutcome | int
FaultKey = tuple[Side, str | None]


class FixtureRunner:
    """``RunnerPort`` executing only reviewed fixture revisions, in-process.

    ``faults`` maps ``(side, input_sha256 or None)`` to an unusable outcome or an
    integer override value (e.g. a flaky confirmation).
    """

    def __init__(self, enrollment: Enrollment | None = None, *,
                 clock: SteppingClock | None = None,
                 faults: Mapping[FaultKey, Fault] | None = None,
                 isolation: IsolationStatus = IsolationStatus.OFFLINE_FIXTURE) -> None:
        self._enrollment = enrollment or catalog.demo_enrollment()
        self._clock = clock or SteppingClock()
        self._faults = dict(faults or {})
        self._isolation = isolation
        self.requests: list[ExecutionRequest] = []

    async def attest(self) -> IsolationAttestation:
        verified = self._isolation in (IsolationStatus.OFFLINE_FIXTURE, IsolationStatus.VERIFIED)
        return IsolationAttestation(
            runner_mode=RunnerMode.OFFLINE_FIXTURE, status=self._isolation,
            snapshot_id=self._enrollment.snapshot_id, image_digest=self._enrollment.image_digest,
            harness_sha256=self._enrollment.harness_sha256, network_block_all=True,
            egress_denied=None, metadata_denied=None, checked_at=self._clock.now(),
            config_fingerprint=canonical_sha256({"runner": "offline_fixture",
                                                 "snapshot": self._enrollment.snapshot_id}),
            reason_code=None if verified else ReasonCode.ISOLATION_UNVERIFIED)

    def _fault(self, request: ExecutionRequest) -> Fault | None:
        for key in ((request.side, request.input_sha256), (request.side, None)):
            if key in self._faults:
                return self._faults[key]
        return None

    async def observe(self, request: ExecutionRequest) -> SandboxObservation:
        self.requests.append(request)
        started = self._clock.now()
        outcome = ObservationOutcome.OK
        value: int | None = None
        exit_code: int | None = 0
        revision = catalog.revision_by_source_sha256(request.source_sha256)
        if (revision is None or request.snapshot_id != self._enrollment.snapshot_id
                or request.image_digest != self._enrollment.image_digest
                or request.harness_sha256 != self._enrollment.harness_sha256):
            outcome, exit_code = ObservationOutcome.DENIED, None
        else:
            fault = self._fault(request)
            if isinstance(fault, ObservationOutcome):
                outcome = fault
                exit_code = 1 if fault is ObservationOutcome.NONZERO else None
            else:
                module = _load_revision_module(revision.path)
                computed = module.total_cents(list(request.input.prices_cents),
                                              request.input.discount_percent)
                value = fault if isinstance(fault, int) else int(computed)
        stdout = canonical_json({"value": value}) if outcome is ObservationOutcome.OK else b""
        return SandboxObservation(
            attempt_id=request.attempt_id, side=request.side, commit_sha=request.commit_sha,
            source_sha256=request.source_sha256, input_sha256=request.input_sha256,
            runner_mode=RunnerMode.OFFLINE_FIXTURE, snapshot_id=request.snapshot_id,
            image_digest=request.image_digest, harness_sha256=request.harness_sha256,
            sandbox_id=None, started_at=started, duration_ms=1, outcome=outcome,
            value=value if outcome is ObservationOutcome.OK else None, exit_code=exit_code,
            stdout_sha256=sha256_hex(stdout), stderr_sha256=sha256_hex(b""), truncated=False)


# ---------------------------------------------------------------- checks


class FixtureChecks:
    """``CheckPort`` that never publishes; records intents for assertions."""

    def __init__(self, clock: SteppingClock | None = None) -> None:
        self._clock = clock or SteppingClock()
        self.intents: list[CheckIntent] = []

    async def publish(self, intent: CheckIntent) -> PublishResult:
        self.intents.append(intent)
        return PublishResult(run_id=intent.run_id, status=PublishStatus.SKIPPED_OFFLINE,
                             check_run_id=None, attempts=0, reason_code=ReasonCode.FEATURE_DISABLED,
                             published_at=None)


# ---------------------------------------------------------------- repository and webhooks

_FIXTURE_PR_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/pull/([1-9][0-9]{0,9})$")


class FixtureRepositoryValidator:
    """``RepositoryValidator`` for the offline demo repo (not a hardened parser)."""

    def __init__(self, enrollments: Sequence[Enrollment] | None = None) -> None:
        self._enrollments = tuple(enrollments or (catalog.demo_enrollment(),))

    def resolve(self, pr_url: str) -> tuple[PrTarget, Enrollment]:
        match = _FIXTURE_PR_URL.match(pr_url)
        if match is None or int(match.group(3)) > 2**31 - 1:
            raise AdapterError(ProviderName.GITHUB, ReasonCode.INVALID_PR_URL,
                               "expected https://github.com/OWNER/REPO/pull/N", retryable=False)
        owner, repo, number = match.group(1), match.group(2), int(match.group(3))
        enrollment = next((e for e in self._enrollments
                           if e.repo_full_name.lower() == f"{owner}/{repo}".lower()), None)
        if enrollment is None:
            raise AdapterError(ProviderName.GITHUB, ReasonCode.REPO_NOT_ENROLLED,
                               "repository is not enrolled", retryable=False)
        target = PrTarget(owner=enrollment.owner, repo=enrollment.repo, number=number,
                          repo_id=enrollment.repo_id, installation_id=enrollment.installation_id)
        return target, enrollment


class FixtureWebhookVerifier:
    def __init__(self, secret: bytes = FIXTURE_WEBHOOK_SECRET) -> None:
        self._secret = secret

    def sign(self, raw_body: bytes) -> str:
        return "sha256=" + hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()

    def verify(self, raw_body: bytes, signature_header: str | None) -> bool:
        if not signature_header:
            return False
        return hmac.compare_digest(self.sign(raw_body), signature_header)

    def parse(self, event: str, delivery_id: str, raw_body: bytes) -> GithubWebhookEvent | None:
        if event != "pull_request":
            return None
        body = json.loads(raw_body)
        if body.get("action") not in {a.value for a in WebhookAction}:
            return None
        pr, repository = body["pull_request"], body["repository"]
        return GithubWebhookEvent(
            delivery_id=delivery_id, event="pull_request", action=WebhookAction(body["action"]),
            installation_id=body["installation"]["id"], repo_id=repository["id"],
            owner=repository["owner"]["login"], repo=repository["name"], number=pr["number"],
            head_sha=pr["head"]["sha"], base_sha=pr["base"]["sha"],
            body_sha256=sha256_hex(raw_body))


# ---------------------------------------------------------------- store


def initial_budget(now: datetime, limits: BudgetLimits | None = None) -> Budget:
    limits = limits or BudgetLimits()
    return Budget(limits=limits, used=BudgetUsage(pairs=0, confirmations=0, sandbox_invocations=0,
                                                  meta_requests=0, snap_requests=0,
                                                  candidates_generated=0),
                  deadline_at=now + timedelta(seconds=limits.deadline_seconds),
                  wall_clock_ms=None, cost=CostRecord(status=CostStatus.UNKNOWN,
                                                      amount_usd_micros=None))


class InMemoryRunStore:
    """Fixture ``RunStore`` + ``ControlStore`` with idempotency, quotas and lease fencing.

    Single-process and in-memory; the real SQLite store is owned by ``store``.
    """

    def __init__(self, clock: SteppingClock | None = None, ids: SequentialIds | None = None,
                 quota: QuotaPolicy | None = None, budget: BudgetLimits | None = None) -> None:
        self._clock = clock or SteppingClock()
        self._ids = ids or SequentialIds()
        self._quota = quota or QuotaPolicy()
        self._budget = budget or BudgetLimits()
        self._lock = threading.RLock()
        self.records: dict[str, RunRecord] = {}
        self.leases: dict[str, RunLease] = {}
        self.epochs: dict[str, int] = {}
        self.receipts: dict[str, ReceiptV1] = {}
        self.outbox: dict[str, CheckIntent] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self.reservations: list[QuotaReservation] = []
        self.deliveries: set[str] = set()
        self.requester: dict[str, str] = {}

    # -- control side
    def reserve_quota(self, requester_scope: str, run_kind: RunKind) -> QuotaDecision:
        with self._lock:
            now = self._clock.now()
            recent = [r for r in self.reservations if now - r.reserved_at < timedelta(hours=1)]
            active = [rid for rid, rec in self.records.items()
                      if rec.status.status in ACTIVE_STATES]
            mine = [rid for rid in active if self.requester.get(rid) == requester_scope]
            if (len(recent) >= self._quota.accepted_runs_per_hour
                    or len(active) >= self._quota.max_active_runs
                    or len(mine) >= self._quota.max_active_per_requester):
                return QuotaDecision(allowed=False, reason_code=ReasonCode.RATE_LIMITED,
                                     retry_after_seconds=60)
            return QuotaDecision(allowed=True, reason_code=None, retry_after_seconds=None)

    def _new_run(self, requester_scope: str, kind: RunKind, target: PrTarget, seed: int,
                 parent: str | None) -> RunStatus:
        decision = self.reserve_quota(requester_scope, kind)
        if not decision.allowed:
            raise AdapterError("store", ReasonCode.RATE_LIMITED, "quota exceeded",
                               retryable=True)
        now = self._clock.now()
        run_id = self._ids.new_run_id()
        status = RunStatus(run_id=run_id, status=RunState.QUEUED, created_at=now, updated_at=now,
                           is_stale=None, attempts_used=0, budget=initial_budget(now, self._budget),
                           verdict=None, reason_code=None, receipt_sha256=None)
        self.records[run_id] = RunRecord(run_id=run_id, run_kind=kind, target=target, seed=seed,
                                         status=status, parent_receipt_sha256=parent,
                                         checkpoint=RunCheckpoint())
        self.requester[run_id] = requester_scope
        self.reservations.append(QuotaReservation(
            run_id=run_id, requester_scope=requester_scope, reserved_at=now,
            window_started_at=now, run_kind=kind))
        return status

    def create_run(self, request: RunRequest, *, idempotency_key: str, requester_scope: str,
                   target: PrTarget, enrollment: Enrollment) -> RunStatus:
        del enrollment
        with self._lock:
            body = request.canonical_sha256()
            existing = self.idempotency.get((requester_scope, idempotency_key))
            if existing is not None:
                if existing[0] != body:
                    raise IdempotencyConflictError(idempotency_key)
                return self.records[existing[1]].status
            status = self._new_run(requester_scope, RunKind.VERIFICATION, target,
                                   17 if request.seed is None else request.seed, None)
            self.idempotency[(requester_scope, idempotency_key)] = (body, status.run_id)
            return status

    def create_replay(self, parent_run_id: str, request: ReplayRequest, *,
                      idempotency_key: str) -> RunStatus:
        with self._lock:
            receipt = self.receipts.get(parent_run_id)
            parent = self.records.get(parent_run_id)
            if receipt is None or parent is None or receipt.replay is None or (
                    receipt.replay.probe_no != request.probe_no):
                raise AdapterError("store", ReasonCode.REPLAY_UNAVAILABLE,
                                   "no eligible pinned input", retryable=False)
            key = ("operator", idempotency_key)
            body = canonical_sha256({"parent": parent_run_id, **request.model_dump(mode="json")})
            if key in self.idempotency:
                if self.idempotency[key][0] != body:
                    raise IdempotencyConflictError(idempotency_key)
                return self.records[self.idempotency[key][1]].status
            status = self._new_run("operator", RunKind.REPLAY, parent.target, receipt.seed,
                                   receipt.receipt_sha256)
            self.records[status.run_id] = RunRecord(
                run_id=status.run_id, run_kind=RunKind.REPLAY, target=parent.target,
                seed=receipt.seed, status=status, parent_receipt_sha256=receipt.receipt_sha256,
                replay=receipt.replay, checkpoint=RunCheckpoint())
            self.idempotency[key] = (body, status.run_id)
            return status

    def get_status(self, run_id: str) -> RunStatus | None:
        record = self.records.get(run_id)
        return None if record is None else record.status

    def get_receipt(self, run_id: str) -> ReceiptV1 | None:
        return self.receipts.get(run_id)

    def quota_reservations(self, run_id: str) -> Sequence[QuotaReservation]:
        return [r for r in self.reservations if r.run_id == run_id]

    def record_webhook_delivery(self, event: GithubWebhookEvent) -> bool:
        with self._lock:
            if event.delivery_id in self.deliveries:
                return False
            self.deliveries.add(event.delivery_id)
            return True

    # -- worker side
    def _check(self, lease: RunLease) -> RunRecord:
        held = self.leases.get(lease.run_id)
        if held is None or held.owner != lease.owner or held.epoch != lease.epoch or (
                held.expires_at <= self._clock.now()) or (
                self.records[lease.run_id].status.status in TERMINAL_STATES):
            raise LeaseLostError(lease.run_id)
        return self.records[lease.run_id]

    def claim(self, worker_id: str, lease_seconds: int) -> RunLease | None:
        with self._lock:
            now = self._clock.now()
            for run_id, record in self.records.items():
                held = self.leases.get(run_id)
                if record.status.status in TERMINAL_STATES or (held and held.expires_at > now):
                    continue
                epoch = self.epochs.get(run_id, 0) + 1
                self.epochs[run_id] = epoch
                lease = RunLease(run_id=run_id, owner=worker_id, epoch=epoch,
                                 expires_at=now + timedelta(seconds=lease_seconds))
                self.leases[run_id] = lease
                return lease
            return None

    def renew(self, lease: RunLease, lease_seconds: int) -> RunLease:
        with self._lock:
            self._check(lease)
            renewed = lease.model_copy(update={
                "expires_at": self._clock.now() + timedelta(seconds=lease_seconds)})
            self.leases[lease.run_id] = renewed
            return renewed

    def _replace(self, record: RunRecord, **changes: object) -> None:
        from dataclasses import replace

        self.records[record.run_id] = replace(record, **changes)  # type: ignore[arg-type]

    def checkpoint(self, lease: RunLease, event: RunEvent) -> None:
        with self._lock:
            record = self._check(lease)
            if event.run_id != lease.run_id or event.seq != len(record.events) + 1:
                raise ValueError("event must be the next sequence number for this run")
            status = record.status
            if event.kind is RunEventKind.STATE_CHANGED:
                if event.from_state is not status.status:
                    raise ValueError("event from_state does not match current state")
                status = status.model_copy(update={"status": event.to_state,
                                                   "updated_at": event.at})
            self._replace(record, events=(*record.events, event), status=status)

    def reserve_progress(self, lease: RunLease, checkpoint: RunCheckpoint) -> RunCheckpoint:
        with self._lock:
            record = self._check(lease)
            progress = RunCheckpoint.model_validate(checkpoint.model_dump())
            previous = record.checkpoint
            if previous is None:
                raise ValueError("legacy run has no recoverable progress ledger")
            pin = progress.pinned_revision
            if previous.pinned_revision is not None and pin != previous.pinned_revision:
                raise ValueError("pinned revision is immutable")
            if pin is not None and any(getattr(pin, name) != getattr(record.target, name)
                                       for name in PrTarget.model_fields):
                raise ValueError("pinned revision must match trusted target")
            if record.replay is not None and pin is not None and (
                    pin.base_sha != record.replay.base_sha
                    or pin.head_sha != record.replay.head_sha):
                raise ValueError("replay pin must match original revisions")
            before, after = previous.provider_allocations, progress.provider_allocations
            if after.meta < before.meta or after.snap < before.snap:
                raise ValueError("provider allocations cannot decrease")
            limits = record.status.budget.limits
            if after.meta > limits.max_meta_requests or after.snap > limits.max_snap_requests:
                raise ValueError("provider allocations exceed run caps")
            if progress == previous:
                raise ValueError("progress must advance; duplicate reservation authorizes no I/O")
            used = record.status.budget.used.model_dump()
            used.update(meta_requests=after.meta, snap_requests=after.snap)
            budget = Budget.model_validate({**record.status.budget.model_dump(), "used": used})
            status = record.status.model_copy(update={"budget": budget})
            self._replace(record, checkpoint=progress, status=status)
            return progress

    def record_attempt(self, lease: RunLease, attempt: AttemptRecord) -> None:
        with self._lock:
            record = self._check(lease)
            others = [a for a in record.attempts if a.attempt_id != attempt.attempt_id]
            status = record.status.model_copy(update={
                "attempts_used": sum(1 for a in [*others, attempt] if a.charged)})
            self._replace(record, attempts=(*others, attempt), status=status)

    def commit_probe(self, lease: RunLease, probe: ProbeRecord) -> None:
        with self._lock:
            record = self._check(lease)
            if any(p.candidate.input_sha256 == probe.candidate.input_sha256
                   for p in record.probes):
                raise ValueError("duplicate logical observation for this input")
            if probe.probe_no != len(record.probes) + 1:
                raise ValueError("probe_no must be the next probe number")
            self._replace(record, probes=(*record.probes, probe))

    def finish(self, lease: RunLease, receipt: ReceiptV1, check: CheckIntent | None) -> None:
        with self._lock:
            record = self._check(lease)
            if lease.run_id in self.receipts:
                raise ValueError("one terminal receipt per run")
            if receipt.receipt_sha256 is None or receipt.run_id != lease.run_id:
                raise ValueError("finish requires the sealed receipt for this run")
            state = RunState.FAILED if receipt.verdict is None else RunState.COMPLETED
            status = record.status.model_copy(update={
                "status": state, "updated_at": receipt.completed_at, "verdict": receipt.verdict,
                "reason_code": receipt.reason_code, "receipt_sha256": receipt.receipt_sha256,
                "budget": receipt.budget, "is_stale": False})
            RunStatus.model_validate(status.model_dump())
            self.receipts[lease.run_id] = receipt
            if check is not None:
                self.outbox[lease.run_id] = check
            self._replace(record, status=status)
            self.leases.pop(lease.run_id, None)

    def load_run(self, run_id: str) -> RunRecord:
        return self.records[run_id]


class InMemoryHeartbeatStore:
    """HeartbeatStore wire example only; production must persist across restarts."""

    def __init__(self, clock: SteppingClock | None = None) -> None:
        self._clock = clock or SteppingClock()
        self._lock = threading.RLock()
        self._heartbeats: dict[str, WorkerHeartbeat] = {}

    def write_heartbeat(self, heartbeat: WorkerHeartbeat) -> None:
        heartbeat = WorkerHeartbeat.model_validate(heartbeat.model_dump())
        ttl = heartbeat.expires_at - heartbeat.observed_at
        with self._lock:
            now = self._clock.now()
            previous = self._heartbeats.get(heartbeat.worker_id)
            if previous is not None:
                if now < previous.observed_at:
                    raise ValueError("trusted heartbeat time cannot regress")
                if now == previous.observed_at:
                    if (heartbeat.profile, heartbeat.config_fingerprint) != (
                            previous.profile, previous.config_fingerprint):
                        raise ValueError("changed heartbeat requires advancing trusted time")
                    return
            stored = WorkerHeartbeat(
                worker_id=heartbeat.worker_id, profile=heartbeat.profile,
                config_fingerprint=heartbeat.config_fingerprint,
                observed_at=now, expires_at=now + ttl)
            self._heartbeats[stored.worker_id] = stored

    def read_heartbeat(self, profile: Profile,
                       config_fingerprint: str) -> WorkerHeartbeat | None:
        with self._lock:
            now = self._clock.now()
            matches = [h for h in self._heartbeats.values()
                       if h.is_fresh(profile, config_fingerprint, now)]
            return max(matches, key=lambda h: (h.observed_at, h.worker_id), default=None)


# ---------------------------------------------------------------- control API client


class FixtureControlApi:
    """``ControlApiClient`` bound directly to a ``ControlStore`` (no HTTP)."""

    def __init__(self, store: InMemoryRunStore | None = None,
                 validator: FixtureRepositoryValidator | None = None,
                 requester_scope: str = "acp:fixture") -> None:
        self.store = store or InMemoryRunStore()
        self._validator = validator or FixtureRepositoryValidator()
        self._scope = requester_scope

    async def create_run(self, request: RunRequest, idempotency_key: str) -> RunStatus:
        target, enrollment = self._validator.resolve(request.pr_url)
        try:
            return self.store.create_run(request, idempotency_key=idempotency_key,
                                         requester_scope=self._scope, target=target,
                                         enrollment=enrollment)
        except IdempotencyConflictError as error:
            raise AdapterError("store", ReasonCode.CONFLICTING_IDEMPOTENCY_KEY,
                               "idempotency key reused with a different body",
                               retryable=False) from error

    async def get_status(self, run_id: str) -> RunStatus:
        status = self.store.get_status(run_id)
        if status is None:
            raise AdapterError("store", ReasonCode.NOT_FOUND, "unknown run", retryable=False)
        return status

    async def get_receipt(self, run_id: str) -> ReceiptV1:
        receipt = self.store.get_receipt(run_id)
        if receipt is None:
            code = ReasonCode.NOT_FOUND if self.store.get_status(run_id) is None else (
                ReasonCode.RECEIPT_PENDING)
            raise AdapterError("store", code, "receipt unavailable",
                               retryable=code is ReasonCode.RECEIPT_PENDING)
        return receipt


FixtureFactory = Callable[[], object]

__all__ = [
    "FIXTURE_EPOCH",
    "FIXTURE_WEBHOOK_SECRET",
    "FixtureChecks",
    "FixtureControlApi",
    "FixtureMeta",
    "FixtureRepositoryValidator",
    "FixtureRoute",
    "FixtureRunner",
    "FixtureSource",
    "FixtureWebhookVerifier",
    "InMemoryRunStore",
    "SequentialIds",
    "SteppingClock",
    "initial_budget",
    "load_plan_payload",
    "planning_context",
]
