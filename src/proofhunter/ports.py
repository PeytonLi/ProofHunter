"""Frozen typed ports (PRD section 6, "Internal callable interfaces").

External I/O is asynchronous (``SourcePort``, ``MetaPort``, ``RoutePort``,
``RunnerPort``, ``CheckPort``, ``ControlApiClient``). Local math, oracles and
SQLite persistence are synchronous. Adapters bind real or fixture
implementations at startup; fixture implementations live in
``fixtures/adapters.py``.

Failure signaling: adapters raise ``AdapterError`` (never provider bodies in the
message). ``RoutePort`` never raises for provider problems; it returns a
``fallback``/``policy`` decision. ``RunnerPort.observe`` never raises for target
misbehavior; it returns an observation with an explicit unusable ``outcome``.

``REQUIRED_IMPLEMENTATIONS`` names the module attributes every owner must
export. ``tools/gate.py integrated`` fails if any is missing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from proofhunter.contracts import (
    AttemptRecord,
    Belief,
    BinaryOutcome,
    Candidate,
    CandidateBank,
    CheckIntent,
    Coverage,
    DecisionState,
    Enrollment,
    ExecutionRequest,
    GithubWebhookEvent,
    HypothesisPlan,
    InitialBelief,
    IsolationAttestation,
    OracleResult,
    PinnedRevision,
    PlanningContext,
    ProbeRecord,
    ProbeSelection,
    ProviderName,
    PrTarget,
    PublishResult,
    QuotaDecision,
    QuotaReservation,
    ReasonCode,
    ReceiptV1,
    Replay,
    ReplayRequest,
    RouteDecision,
    RunEvent,
    RunKind,
    RunLease,
    RunRequest,
    RunStatus,
    SandboxObservation,
    SelectionPolicy,
    SourceBundle,
    Strategy,
    VerdictResult,
)

if TYPE_CHECKING:
    from proofhunter.config import Settings


# ---------------------------------------------------------------------------
# Typed adapter failures
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Typed adapter failure with retryability. ``message`` must be sanitized:
    no provider bodies, headers, tokens or source text."""

    def __init__(self, provider: ProviderName | str, code: ReasonCode, message: str, *,
                 retryable: bool, request_id: str | None = None) -> None:
        super().__init__(message[:300])
        self.provider = provider
        self.code = code
        self.message = message[:300]
        self.retryable = retryable
        self.request_id = request_id

    def __repr__(self) -> str:
        return (f"AdapterError(provider={self.provider!s}, code={self.code.value}, "
                f"retryable={self.retryable})")


class LeaseLostError(Exception):
    """Raised by ``RunStore`` writes when the lease owner/epoch no longer matches."""


class IdempotencyConflictError(Exception):
    """Same idempotency key, different canonical request body (HTTP 409)."""


class ConfigError(Exception):
    """Invalid or unsafe configuration detected at startup."""


# ---------------------------------------------------------------------------
# Small runtime helpers
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


@runtime_checkable
class IdFactory(Protocol):
    def new_run_id(self) -> str:
        """Lowercase UUIDv4 string."""
        ...

    def new_attempt_id(self) -> str:
        """Lowercase UUIDv4 string, fresh for every sandbox invocation."""
        ...


# ---------------------------------------------------------------------------
# External I/O ports (async)
# ---------------------------------------------------------------------------


@runtime_checkable
class SourcePort(Protocol):
    """GitHub source access (owner: github)."""

    async def pin(self, target: PrTarget) -> PinnedRevision:
        """Resolve the PR and pin exact same-repo base/head SHAs.

        Raises ``AdapterError`` with ``fork_unsupported``, ``revision_unavailable``,
        ``repo_not_enrolled`` or ``provider_unavailable``.
        """
        ...

    async def fetch(self, revision: PinnedRevision, enrollment: Enrollment) -> SourceBundle:
        """Fetch only ``enrollment.source_path`` at both SHAs with blob/tree checks.

        Raises ``AdapterError`` with ``source_invalid`` (symlink, submodule, LFS,
        oversize, non-UTF-8, drift) or ``revision_unavailable``.
        """
        ...


@runtime_checkable
class MetaPort(Protocol):
    """Hypothesis proposals (owner: meta). No tools are ever exposed to the model."""

    async def propose(self, context: PlanningContext) -> HypothesisPlan:
        """Return validated proposals with provenance.

        Raises ``AdapterError`` (``provider_unavailable``, ``provider_timeout``,
        ``provider_refused``, ``provider_invalid_response``) so the caller can apply
        the labeled built-in plan (``contracts.builtin_hypothesis_payload``).
        """
        ...


@runtime_checkable
class RoutePort(Protocol):
    """Snap 1.1 strategy routing (owner: snap)."""

    async def choose(self, state: DecisionState, eligible: tuple[Strategy, ...]) -> RouteDecision:
        """Choose the next preparation strategy among ``eligible`` (non-empty).

        Never raises for provider failures: returns ``mode=fallback`` using
        ``STRATEGY_FALLBACK_ORDER``, ``mode=policy`` with ``call_budget`` after the
        call budget, or ``single_eligible`` without a provider call.
        """
        ...


@runtime_checkable
class RunnerPort(Protocol):
    """Isolated execution (owner: runner). Oracle values never enter the sandbox."""

    async def attest(self) -> IsolationAttestation:
        """Preflight isolation check from the enrolled snapshot (benign canary)."""
        ...

    async def observe(self, request: ExecutionRequest) -> SandboxObservation:
        """Run one fresh instance and return one strictly parsed observation.

        Target misbehavior becomes ``timeout/nonzero/invalid``; isolation failures
        become ``denied``; provider failures become ``infra_error``.
        """
        ...


@runtime_checkable
class CheckPort(Protocol):
    """Advisory Check Run publishing (owner: github). Disabled by default."""

    async def publish(self, intent: CheckIntent) -> PublishResult:
        """Publish or reconcile one advisory check; never a blind retry POST."""
        ...


@runtime_checkable
class ControlApiClient(Protocol):
    """Private control API client used by the ACP process (owner: acp)."""

    async def create_run(self, request: RunRequest, idempotency_key: str) -> RunStatus:
        """``POST /v1/runs``; raises ``AdapterError`` carrying the error envelope code."""
        ...

    async def get_status(self, run_id: str) -> RunStatus:
        """``GET /v1/runs/{run_id}``."""
        ...

    async def get_receipt(self, run_id: str) -> ReceiptV1:
        """``GET /v1/runs/{run_id}/receipt``; ``receipt_pending`` while active."""
        ...


# ---------------------------------------------------------------------------
# Repository validation and webhooks (sync, owner: github)
# ---------------------------------------------------------------------------


@runtime_checkable
class RepositoryValidator(Protocol):
    def resolve(self, pr_url: str) -> tuple[PrTarget, Enrollment]:
        """Strictly parse a canonical PR URL and bind it to a trusted enrollment.

        Raises ``AdapterError`` with ``invalid_pr_url`` or ``repo_not_enrolled``.
        """
        ...


@runtime_checkable
class WebhookVerifier(Protocol):
    def verify(self, raw_body: bytes, signature_header: str | None) -> bool:
        """Constant-time ``X-Hub-Signature-256`` check over raw bytes, before parsing."""
        ...

    def parse(self, event: str, delivery_id: str, raw_body: bytes) -> GithubWebhookEvent | None:
        """Validated relevant event, or ``None`` for irrelevant events."""
        ...


# ---------------------------------------------------------------------------
# Persistence (sync, owner: store)
# ---------------------------------------------------------------------------


@runtime_checkable
class RunStore(Protocol):
    """Worker-facing persistence with lease fencing (owner/epoch CAS)."""

    def claim(self, worker_id: str, lease_seconds: int) -> RunLease | None:
        ...

    def renew(self, lease: RunLease, lease_seconds: int) -> RunLease:
        """Extend a held lease; raises ``LeaseLostError`` if fenced."""
        ...

    def checkpoint(self, lease: RunLease, event: RunEvent) -> None:
        ...

    def record_attempt(self, lease: RunLease, attempt: AttemptRecord) -> None:
        ...

    def commit_probe(self, lease: RunLease, probe: ProbeRecord) -> None:
        ...

    def finish(self, lease: RunLease, receipt: ReceiptV1, check: CheckIntent | None) -> None:
        """Store the sealed receipt and the optional outbox row in one transaction."""
        ...

    def load_run(self, run_id: str) -> RunRecord:
        """Trusted run inputs and committed progress for resume/recovery."""
        ...


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Trusted run inputs the worker needs (internal, never public)."""

    run_id: str
    run_kind: RunKind
    target: PrTarget
    seed: int
    status: RunStatus
    parent_receipt_sha256: str | None = None
    replay: Replay | None = None
    probes: tuple[ProbeRecord, ...] = ()
    attempts: tuple[AttemptRecord, ...] = ()
    events: tuple[RunEvent, ...] = field(default_factory=tuple)


@runtime_checkable
class ControlStore(Protocol):
    """API-facing persistence: idempotent intake, quotas, reads (owner: store)."""

    def create_run(self, request: RunRequest, *, idempotency_key: str, requester_scope: str,
                   target: PrTarget, enrollment: Enrollment) -> RunStatus:
        """Idempotent create: same key + same canonical request returns the same run;
        a changed body raises ``IdempotencyConflictError``; quota denial raises
        ``AdapterError(rate_limited)``."""
        ...

    def create_replay(self, parent_run_id: str, request: ReplayRequest, *,
                      idempotency_key: str) -> RunStatus:
        ...

    def get_status(self, run_id: str) -> RunStatus | None:
        ...

    def get_receipt(self, run_id: str) -> ReceiptV1 | None:
        ...

    def reserve_quota(self, requester_scope: str, run_kind: RunKind) -> QuotaDecision:
        ...

    def quota_reservations(self, run_id: str) -> Sequence[QuotaReservation]:
        ...

    def record_webhook_delivery(self, event: GithubWebhookEvent) -> bool:
        """Persist delivery dedupe with enqueue; ``False`` for duplicates."""
        ...


@runtime_checkable
class VerificationWorker(Protocol):
    """Job worker (owner: integration)."""

    async def run_once(self) -> bool:
        """Claim and fully process at most one run; ``True`` if one was processed."""
        ...


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Injected into ``proofhunter.api.create_app`` (owner: store/api)."""

    settings: Settings
    store: ControlStore
    validator: RepositoryValidator
    webhook: WebhookVerifier | None
    clock: Clock


# ---------------------------------------------------------------------------
# Pure synchronous functions (owners: bayes, oracle)
# ---------------------------------------------------------------------------


class Initializer(Protocol):
    def __call__(self, plan: HypothesisPlan) -> InitialBelief: ...


class Generator(Protocol):
    def __call__(self, plan: HypothesisPlan, seed: int) -> CandidateBank: ...


class Selector(Protocol):
    def __call__(self, belief: Belief, candidates: Sequence[Candidate],
                 policy: SelectionPolicy) -> ProbeSelection: ...


class Updater(Protocol):
    def __call__(self, belief: Belief, selection: ProbeSelection,
                 observed: BinaryOutcome) -> Belief: ...


class Evaluator(Protocol):
    def __call__(self, enrollment: Enrollment, candidate: Candidate) -> OracleResult: ...


class Classifier(Protocol):
    def __call__(self, probes: Sequence[ProbeRecord], coverage: Coverage,
                 stop_reason: ReasonCode) -> VerdictResult: ...


class CoverageFn(Protocol):
    def __call__(self, probes: Sequence[ProbeRecord], coverage_guard_used: bool) -> Coverage: ...


class Sealer(Protocol):
    def __call__(self, receipt: ReceiptV1) -> ReceiptV1: ...


class ReceiptVerifier(Protocol):
    def __call__(self, receipt_json: bytes | str) -> ReceiptV1:
        """Parse untrusted receipt JSON, verify the canonical hash; raise ``AdapterError``
        with ``receipt_tampered`` on mismatch."""
        ...


class ReplayValidator(Protocol):
    def __call__(self, receipt: ReceiptV1, probe_no: int | None) -> Replay:
        """Return the bound replay for ``probe_no`` (``None`` = counterexample)
        after checking provenance; raise ``AdapterError(replay_unavailable)``."""
        ...


# ---------------------------------------------------------------------------
# Factories (owner modules) and required implementations
# ---------------------------------------------------------------------------

SourceFactory = Callable[["Settings", Sequence[Enrollment]], SourcePort]
CheckFactory = Callable[["Settings"], CheckPort]
MetaFactory = Callable[["Settings"], MetaPort]
RouteFactory = Callable[["Settings"], RoutePort]
RunnerFactory = Callable[["Settings"], RunnerPort]


#: ``module -> attributes`` that must exist (and be callable) after G1 join.
REQUIRED_IMPLEMENTATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "proofhunter.github": ("create_source_port", "create_check_port",
                           "create_repository_validator", "create_webhook_verifier"),
    "proofhunter.meta": ("create_meta_port",),
    "proofhunter.snap": ("create_route_port",),
    "proofhunter.bayes": ("initialize", "generate", "select", "update"),
    "proofhunter.runner": ("create_runner_port",),
    "proofhunter.oracle": ("evaluate", "classify", "compute_coverage", "seal",
                           "verify_receipt", "validate_replay"),
    "proofhunter.acp": ("create_control_client", "parse_command"),
    "proofhunter.acp.__main__": (),
    "proofhunter.store": ("create_store",),
    "proofhunter.api": ("create_app",),
    "proofhunter.pipeline": (),
    "proofhunter.worker": (),
    "proofhunter.cli": ("main",),
    "proofhunter.__main__": (),
    "bench.run": ("main",),
}

#: Path ownership after G0 (setup paths are frozen).
MODULE_OWNERS: Final[Mapping[str, tuple[str, ...]]] = {
    "github": ("src/proofhunter/github/", "tests/github/"),
    "meta": ("src/proofhunter/meta/", "tests/meta/"),
    "snap": ("src/proofhunter/snap/", "tests/snap/"),
    "bayes": ("src/proofhunter/bayes/", "tests/bayes/"),
    "runner": ("src/proofhunter/runner/", "tests/runner/", "ops/sandbox/"),
    "oracle": ("src/proofhunter/oracle/", "tests/oracle/"),
    "acp": ("src/proofhunter/acp/", "tests/acp/"),
    "store": ("src/proofhunter/store/", "src/proofhunter/api.py", "tests/store/"),
    "bench": ("bench/", "tests/bench/"),
    "integration": ("src/proofhunter/pipeline.py", "src/proofhunter/worker.py",
                    "src/proofhunter/cli.py", "src/proofhunter/__main__.py",
                    "tests/integration/", "tools/gate.py", "README.md", "docs/integration.md",
                    "ops/deploy/"),
}

__all__ = [name for name in dir() if not name.startswith("_") and name not in {
    "annotations", "TYPE_CHECKING"}]
