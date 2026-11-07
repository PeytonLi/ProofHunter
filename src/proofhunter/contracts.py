"""Frozen shared data contracts for ProofHunter, ``schema_version = "1.0"``.

Every boundary DTO from PRD section 6 lives here, together with the canonical
serializer and the hashing helpers that bind receipts, inputs and enrollments.

Conventions (normative):

* Integers are strict (booleans, floats and numeric strings are rejected).
* Input models forbid extra keys. Every model is frozen (immutable).
* Nullable fields are required and must be sent as explicit ``null`` unless a
  default is documented.
* Timestamps are timezone-aware UTC and serialize as
  ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.
* Hashes are lowercase hex. Run and attempt IDs are lowercase UUIDv4 strings.
* Model and statistical measurements are the only floats. They must be finite
  and are rounded to 12 decimals on validation (``-0.0`` becomes ``0.0``).
* Model vectors are JSON objects keyed by canonical hypothesis ID.

Canonical encoding (``ph-canonical-json-v1``): ``json.dumps`` of the JSON-mode
dump with ``sort_keys=True``, ``separators=(",", ":")``, ``ensure_ascii=False``,
``allow_nan=False``, encoded as UTF-8. Keys sort by Unicode code point. Array
order is preserved. Floats use Python's shortest round-trip ``repr``. Integers
must fit in signed 64 bits. ``receipt_sha256`` is excluded from its own hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

# ---------------------------------------------------------------------------
# Versions and frozen constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: Final = "1.0"
CANONICAL_SERIALIZER_VERSION: Final = "ph-canonical-json-v1"
CONTRACT_ID: Final = "checkout-rounding@1"
SOURCE_PATH: Final = "checkout.py"
ENTRYPOINT: Final = "checkout.total_cents"
ORACLE_VERSION: Final = "checkout-rounding-oracle@1"
HARNESS_VERSION: Final = "proofhunter-harness@1"
PRIOR_POLICY: Final = "v1"
LIKELIHOOD_POLICY: Final = "rounding-likelihood-v1"
GENERATOR_VERSION: Final = "candidate-generator-v1"
SELECTION_POLICY: Final = "eig-select-v1"
VERDICT_POLICY: Final = "verdict-rules-v1"
ROUTE_FALLBACK_POLICY: Final = "route-fallback-v1"
CHECK_NAME: Final = "ProofHunter"

MAX_ITEMS: Final = 8
MAX_PRICE_CENTS: Final = 10_000
MAX_DISCOUNT_PERCENT: Final = 100
MAX_SEED: Final = 2**32 - 1
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1
MAX_SAFE_ID: Final = 2**53 - 1
MAX_CANDIDATES: Final = 64
MAX_CANDIDATES_PER_STRATEGY: Final = 20
MAX_COMMON_ANCHORS: Final = 4
MAX_PROPOSALS: Final = 6
MAX_HYPOTHESES: Final = 8
MAX_SOURCE_BYTES: Final = 64 * 1024
MAX_MODEL_CONTEXT_BYTES: Final = 32 * 1024
MAX_OUTPUT_BYTES: Final = 16 * 1024
MAX_JSON_BODY_BYTES: Final = 8 * 1024
MAX_WEBHOOK_BODY_BYTES: Final = 256 * 1024
MAX_PR_URL_LENGTH: Final = 512
MAX_PROBES: Final = 15
MAX_ATTEMPTS: Final = 15
MEASUREMENT_DECIMALS: Final = 12
PROBABILITY_SUM_TOLERANCE: Final = 1e-9
ROUTE_PROBABILITY_TOLERANCE: Final = 0.01

#: Raw prior weights (``prior_policy=v1``); renormalized over retained modes.
PRIOR_WEIGHTS_V1: Final[Mapping[str, float]] = {
    "H0": 0.60,
    "per_item_rounding": 0.15,
    "half_even_rounding": 0.15,
    "other": 0.10,
}
#: Likelihood constants (``rounding-likelihood-v1``): P(mismatch | h, x).
Q_H0: Final = 0.02
Q_PREDICTOR_DIFFERS: Final = 0.98
Q_PREDICTOR_AGREES: Final = 0.02
Q_OTHER: Final = 0.20


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunState(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    PLANNING = "planning"
    TESTING = "testing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATES: Final = frozenset(
    {RunState.QUEUED, RunState.FETCHING, RunState.PLANNING, RunState.TESTING, RunState.FINALIZING}
)
TERMINAL_STATES: Final = frozenset({RunState.COMPLETED, RunState.FAILED})
#: Allowed forward transitions. Any active state may also go to ``failed``, or to
#: ``completed`` (with ``inconclusive``) when a dependency or trust gate blocks a result.
STATE_TRANSITIONS: Final[Mapping[RunState, frozenset[RunState]]] = {
    RunState.QUEUED: frozenset({RunState.FETCHING, RunState.COMPLETED, RunState.FAILED}),
    RunState.FETCHING: frozenset({RunState.PLANNING, RunState.COMPLETED, RunState.FAILED}),
    RunState.PLANNING: frozenset({RunState.TESTING, RunState.COMPLETED, RunState.FAILED}),
    RunState.TESTING: frozenset({RunState.FINALIZING, RunState.COMPLETED, RunState.FAILED}),
    RunState.FINALIZING: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
}


class Verdict(StrEnum):
    CONFIRMED_REGRESSION = "confirmed_regression"
    CONFIRMED_EXISTING_DEFECT = "confirmed_existing_defect"
    NO_DEFECT_FOUND = "no_defect_found"
    INCONCLUSIVE = "inconclusive"


#: User-facing wording per verdict (PRD section 5).
VERDICT_WORDING: Final[Mapping[Verdict, str]] = {
    Verdict.CONFIRMED_REGRESSION: "Reproduced regression on this head. Review before merging.",
    Verdict.CONFIRMED_EXISTING_DEFECT: "Reproduced defect present in both revisions.",
    Verdict.NO_DEFECT_FOUND: "No defect found in the reported scope and test budget.",
    Verdict.INCONCLUSIVE: "Show reason, completed probes and the next missing requirement.",
}


class ReasonCode(StrEnum):
    """Machine reason codes for errors, stop reasons and verdict reasons."""

    # PRD section 6 normative list
    INVALID_PR_URL = "invalid_pr_url"
    REPO_NOT_ENROLLED = "repo_not_enrolled"
    FORK_UNSUPPORTED = "fork_unsupported"
    REVISION_UNAVAILABLE = "revision_unavailable"
    ORACLE_UNAVAILABLE = "oracle_unavailable"
    ISOLATION_UNVERIFIED = "isolation_unverified"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NONDETERMINISTIC = "nondeterministic"
    COVERAGE_INSUFFICIENT = "coverage_insufficient"
    STALE_EVENT = "stale_event"
    CONFLICTING_IDEMPOTENCY_KEY = "conflicting_idempotency_key"
    INTERNAL_ERROR = "internal_error"
    # Stop and verdict reasons
    CONFIRMED_EVIDENCE = "confirmed_evidence"
    COVERAGE_COMPLETE = "coverage_complete"
    INPUTS_EXHAUSTED = "inputs_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TRUST_FAILURE = "trust_failure"
    PREEXISTING_MISMATCH_CHANGED = "preexisting_mismatch_changed"
    UNSUPPORTED_CONTRACT = "unsupported_contract"
    SOURCE_INVALID = "source_invalid"
    # HTTP and transport
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RECEIPT_PENDING = "receipt_pending"
    REPLAY_UNAVAILABLE = "replay_unavailable"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INVALID_SIGNATURE = "invalid_signature"
    FEATURE_DISABLED = "feature_disabled"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_REFUSED = "provider_refused"
    PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
    RECEIPT_TAMPERED = "receipt_tampered"
    LEASE_LOST = "lease_lost"


class HypothesisMode(StrEnum):
    """Failure modes a provider may propose (H0 is always implicit)."""

    PER_ITEM_ROUNDING = "per_item_rounding"
    HALF_EVEN_ROUNDING = "half_even_rounding"
    OTHER = "other"


class HypothesisId(StrEnum):
    """Canonical hypothesis IDs, assigned only by the local app."""

    H0 = "H0"
    PER_ITEM_ROUNDING = "per_item_rounding"
    HALF_EVEN_ROUNDING = "half_even_rounding"
    OTHER = "other"


#: Canonical vector order used in docs and reference vectors.
HYPOTHESIS_ORDER: Final = (
    HypothesisId.H0,
    HypothesisId.PER_ITEM_ROUNDING,
    HypothesisId.HALF_EVEN_ROUNDING,
    HypothesisId.OTHER,
)
MANDATORY_HYPOTHESES: Final = frozenset({HypothesisId.H0, HypothesisId.OTHER})


class HypothesisSource(StrEnum):
    META = "meta"
    BUILTIN = "builtin"
    MANDATORY = "mandatory"


class PlanSource(StrEnum):
    META = "meta"
    BUILTIN_FALLBACK = "builtin_fallback"
    BUILTIN_RESTORED = "builtin_restored"
    OFFLINE_FIXTURE = "offline_fixture"


class Strategy(StrEnum):
    BOUNDARY = "boundary"
    CONSTRAINT = "constraint"
    SEEDED_FUZZ = "seeded_fuzz"


#: Deterministic route fallback order among eligible actions.
STRATEGY_FALLBACK_ORDER: Final = (Strategy.CONSTRAINT, Strategy.BOUNDARY, Strategy.SEEDED_FUZZ)


class CoverageTag(StrEnum):
    EMPTY = "empty"
    SINGLETON = "singleton"
    MULTI_ITEM = "multi_item"
    ZERO_DISCOUNT = "zero_discount"
    FULL_DISCOUNT = "full_discount"
    HALF_CENT = "half_cent"
    MAX_ITEMS = "max_items"
    MAX_PRICE = "max_price"


REQUIRED_COVERAGE_TAGS: Final = (
    CoverageTag.EMPTY,
    CoverageTag.FULL_DISCOUNT,
    CoverageTag.HALF_CENT,
    CoverageTag.MULTI_ITEM,
    CoverageTag.ZERO_DISCOUNT,
)


class RouteMode(StrEnum):
    LIVE = "live"
    STUB = "stub"
    FALLBACK = "fallback"
    POLICY = "policy"


class RouteReasonCode(StrEnum):
    PROVIDER_CHOICE = "provider_choice"
    STUB_CHOICE = "stub_choice"
    SINGLE_ELIGIBLE = "single_eligible"
    CALL_BUDGET = "call_budget"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    RATE_LIMITED = "rate_limited"
    PREVIEW_UNAVAILABLE = "preview_unavailable"
    INVALID_RESPONSE = "invalid_response"
    INVALID_CHOICE = "invalid_choice"
    INVALID_PROBABILITIES = "invalid_probabilities"


class ProviderMode(StrEnum):
    LIVE = "live"
    STUB = "stub"
    FALLBACK = "fallback"
    POLICY = "policy"
    OFFLINE_FIXTURE = "offline_fixture"
    DISABLED = "disabled"
    NOT_CALLED = "not_called"


class ProviderName(StrEnum):
    GITHUB = "github"
    META = "meta"
    SNAP = "snap"
    RUNNER = "runner"


class Side(StrEnum):
    BASE = "base"
    HEAD = "head"
    CONFIRM = "confirm"


class RunnerMode(StrEnum):
    DAYTONA = "daytona"
    OFFLINE_FIXTURE = "offline_fixture"


class ExecutionMode(StrEnum):
    LIVE = "live"
    OFFLINE_FIXTURE = "offline_fixture"


class RunKind(StrEnum):
    VERIFICATION = "verification"
    REPLAY = "replay"


class ObservationOutcome(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    NONZERO = "nonzero"
    INVALID = "invalid"
    DENIED = "denied"
    INFRA_ERROR = "infra_error"


class OracleStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class TrustTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class BinaryOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"


class EvidenceKind(StrEnum):
    """Classification of one probe pair relative to the T1 oracle.

    * ``match``: base = expected and head = expected (usable, Y=match).
    * ``head_mismatch``: base = expected, head != expected (usable, Y=mismatch).
    * ``shared_mismatch``: base = head != expected (pre-existing defect, no update).
    * ``base_mismatch``: base != expected and head != base (no update).
    * ``excluded``: unusable pair (see ``ExclusionReason``), no update.
    """

    MATCH = "match"
    HEAD_MISMATCH = "head_mismatch"
    SHARED_MISMATCH = "shared_mismatch"
    BASE_MISMATCH = "base_mismatch"
    EXCLUDED = "excluded"


class ExclusionReason(StrEnum):
    BASE_MISMATCH = "base_mismatch"
    TIMEOUT = "timeout"
    NONZERO_EXIT = "nonzero_exit"
    INVALID_OUTPUT = "invalid_output"
    DENIED = "denied"
    INFRA_ERROR = "infra_error"
    TRUST_FAILURE = "trust_failure"
    DUPLICATE = "duplicate"
    ORACLE_UNAVAILABLE = "oracle_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"


class SelectionReason(StrEnum):
    MAX_EIG = "max_eig"
    COVERAGE_GUARD = "coverage_guard"
    REPLAY = "replay"


class ConfirmationStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    REPRODUCED = "reproduced"
    DISAGREED = "disagreed"
    UNUSABLE = "unusable"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


class RunEventKind(StrEnum):
    STATE_CHANGED = "state_changed"
    LEASE_CLAIMED = "lease_claimed"
    LEASE_RENEWED = "lease_renewed"
    LEASE_LOST = "lease_lost"
    LEASE_EXPIRED = "lease_expired"
    RECOVERED = "recovered"
    AMBIGUOUS_ATTEMPT_CHARGED = "ambiguous_attempt_charged"
    PROVIDER_FALLBACK = "provider_fallback"
    COVERAGE_GUARD = "coverage_guard"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SANDBOX_CLEANUP_SCHEDULED = "sandbox_cleanup_scheduled"
    SANDBOX_CLEANUP_DONE = "sandbox_cleanup_done"
    PUBLISH_QUEUED = "publish_queued"
    PUBLISH_HELD = "publish_held"
    PUBLISH_DONE = "publish_done"
    STALE_HEAD_DETECTED = "stale_head_detected"


class CheckConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"


class PublishStatus(StrEnum):
    PUBLISHED = "published"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_OFFLINE = "skipped_offline"
    STALE_HEAD = "stale_head"
    HELD_AMBIGUOUS = "held_ambiguous"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"


class IsolationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    OFFLINE_FIXTURE = "offline_fixture"


class CostStatus(StrEnum):
    MEASURED = "measured"
    UNKNOWN = "unknown"


class WebhookAction(StrEnum):
    OPENED = "opened"
    REOPENED = "reopened"
    SYNCHRONIZE = "synchronize"


# ---------------------------------------------------------------------------
# Scalar types and validators
# ---------------------------------------------------------------------------

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_UUID4 = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SEGMENT = r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$"
_SAFE_ID_PATTERN = r"^[A-Za-z0-9._:@/+=-]{1,128}$"


def _reject_bool_and_text(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("measurement must be a JSON number")
    return value


def _round_measurement(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("measurement must be finite")
    rounded = round(float(value), MEASUREMENT_DECIMALS)
    return 0.0 if rounded == 0 else rounded


def _utc_input(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and len(value) <= 40:
        return value
    raise ValueError("timestamp must be an ISO 8601 string or datetime")


def _require_utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Canonical UTC timestamp text: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return _require_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


StrictStr = Annotated[str, StringConstraints(strict=True)]
Measurement = Annotated[
    float, BeforeValidator(_reject_bool_and_text), Field(allow_inf_nan=False),
    AfterValidator(_round_measurement),
]
Probability = Annotated[Measurement, Field(ge=0.0, le=1.0)]
UtcTimestamp = Annotated[
    datetime,
    BeforeValidator(_utc_input),
    AfterValidator(_require_utc),
    PlainSerializer(format_timestamp, return_type=str, when_used="json"),
]
Sha40 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
Sha256Hex = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
Uuid4Str = Annotated[str, StringConstraints(strict=True, pattern=_UUID4)]
RunId = Uuid4Str
AttemptId = Uuid4Str
PathSegment = Annotated[str, StringConstraints(strict=True, pattern=_SEGMENT)]
PositiveId = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_ID)]
PrNumber = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
NonNegInt = Annotated[StrictInt, Field(ge=0, le=INT64_MAX)]
Int64 = Annotated[StrictInt, Field(ge=INT64_MIN, le=INT64_MAX)]
Seed = Annotated[StrictInt, Field(ge=0, le=MAX_SEED)]
PriceCents = Annotated[StrictInt, Field(ge=0, le=MAX_PRICE_CENTS)]
DiscountPercent = Annotated[StrictInt, Field(ge=0, le=MAX_DISCOUNT_PERCENT)]
ItemCount = Annotated[StrictInt, Field(ge=0, le=MAX_ITEMS)]
VersionTag = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._@:+-]{1,64}$")]
SafeToken = Annotated[str, StringConstraints(strict=True, pattern=_SAFE_ID_PATTERN)]
ShortText = Annotated[str, StringConstraints(strict=True, max_length=300)]
RelativePath = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=200,
        pattern=r"^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._/-]{1,200}$",
    ),
]


def _ok_hex40(value: str) -> bool:
    return bool(_HEX40.match(value))


def _check_vector_keys(vector: Mapping[HypothesisId, float]) -> None:
    if not vector:
        raise ValueError("model vector must not be empty")


def _probability_vector(vector: dict[HypothesisId, float]) -> dict[HypothesisId, float]:
    _check_vector_keys(vector)
    total = math.fsum(vector.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise ValueError(f"probability vector must sum to 1 (got {total!r})")
    return vector


def _interior_vector(vector: dict[HypothesisId, float]) -> dict[HypothesisId, float]:
    _check_vector_keys(vector)
    if any(not 0.0 < value < 1.0 for value in vector.values()):
        raise ValueError("likelihoods must be strictly inside (0, 1)")
    return vector


#: Normalized probability mass keyed by hypothesis ID (prior, posterior, belief).
ProbabilityVector = Annotated[
    dict[HypothesisId, Probability], AfterValidator(_probability_vector)
]
#: ``q_h(x) = P(mismatch | h, x)`` keyed by hypothesis ID, strictly interior.
LikelihoodVector = Annotated[dict[HypothesisId, Probability], AfterValidator(_interior_vector)]
#: Predicted integer output per hypothesis; ``null`` for models with no predictor (``other``).
PredictionVector = dict[HypothesisId, Int64 | None]


def _sorted_unique_tags(tags: list[CoverageTag]) -> list[CoverageTag]:
    if list(tags) != sorted(set(tags)):
        raise ValueError("coverage tags must be sorted and deduplicated")
    return tags


CoverageTags = Annotated[
    list[CoverageTag], Field(max_length=len(CoverageTag)), AfterValidator(_sorted_unique_tags)
]


# ---------------------------------------------------------------------------
# Base models
# ---------------------------------------------------------------------------


class Contract(BaseModel):
    """Base for every frozen DTO: immutable, strict, extra keys forbidden."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=False,
        validate_default=True,
        use_enum_values=False,
        ser_json_inf_nan="null",
        str_strip_whitespace=False,
        allow_inf_nan=False,
        regex_engine="python-re",
    )

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def canonical_sha256(self) -> str:
        return sha256_hex(self.canonical_bytes())


# ---------------------------------------------------------------------------
# Repository targets and enrollment
# ---------------------------------------------------------------------------


class PrRef(Contract):
    """Parsed, normalized PR URL (owner/repo/number only, before enrollment lookup)."""

    owner: PathSegment
    repo: PathSegment
    number: PrNumber

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


class PrTarget(PrRef):
    """PR plus immutable IDs assigned server-side from trusted enrollment."""

    repo_id: PositiveId
    installation_id: PositiveId


class PinnedRevision(PrTarget):
    base_sha: Sha40
    head_sha: Sha40
    resolved_at: UtcTimestamp


class PublicRevision(Contract):
    """Public projection of ``PinnedRevision``: no ``installation_id``."""

    owner: PathSegment
    repo: PathSegment
    number: PrNumber
    repo_id: PositiveId
    base_sha: Sha40
    head_sha: Sha40
    resolved_at: UtcTimestamp


class Enrollment(Contract):
    """Trusted operator enrollment; lives in trusted config, never in the PR."""

    schema_version: Literal["1.0"]
    repo_id: PositiveId
    repo_full_name: Annotated[
        str, StringConstraints(strict=True, max_length=201,
                               pattern=r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
    ]
    installation_id: PositiveId
    visibility: Literal["public"]
    source_path: Literal["checkout.py"]
    entrypoint: Literal["checkout.total_cents"]
    contract_id: Literal["checkout-rounding@1"]
    contract_sha256: Sha256Hex
    oracle_version: VersionTag
    harness_version: VersionTag
    harness_sha256: Sha256Hex
    snapshot_id: SafeToken
    image_digest: ImageDigest
    enabled: StrictBool

    @property
    def owner(self) -> str:
        return self.repo_full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repo_full_name.split("/", 1)[1]


class EnrollmentRegistry(Contract):
    """File format of ``PROOFHUNTER_ENROLLMENTS`` (see config/enrollments.example.json)."""

    schema_version: Literal["1.0"]
    enrollments: Annotated[list[Enrollment], Field(max_length=32)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [e.repo_id for e in self.enrollments]
        names = [e.repo_full_name.lower() for e in self.enrollments]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("enrollments must have unique repo_id and repo_full_name")
        return self


def enrollment_fingerprint(enrollment: Enrollment) -> str:
    """SHA-256 of the canonical enrollment without operator-only ``installation_id``."""
    data = enrollment.model_dump(mode="json")
    data.pop("installation_id")
    return canonical_sha256(data)


# ---------------------------------------------------------------------------
# HTTP request/response DTOs
# ---------------------------------------------------------------------------


class RunRequest(Contract):
    pr_url: Annotated[str, StringConstraints(strict=True, min_length=1,
                                             max_length=MAX_PR_URL_LENGTH)]
    seed: Seed | None = None


class ReplayRequest(Contract):
    probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)]


class CostRecord(Contract):
    status: CostStatus
    #: Integer micro-USD when measured, ``null`` when unknown.
    amount_usd_micros: NonNegInt | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status is CostStatus.MEASURED) != (self.amount_usd_micros is not None):
            raise ValueError("amount_usd_micros is required iff cost is measured")
        return self


class BudgetLimits(Contract):
    """Per-run limits (PRD section 2 defaults); enforced before spending."""

    max_pairs: Annotated[StrictInt, Field(ge=1, le=6)] = 6
    max_confirmations: Annotated[StrictInt, Field(ge=0, le=1)] = 1
    max_sandbox_invocations: Annotated[StrictInt, Field(ge=1, le=MAX_ATTEMPTS)] = 15
    max_meta_requests: Annotated[StrictInt, Field(ge=0, le=2)] = 2
    max_snap_requests: Annotated[StrictInt, Field(ge=0, le=3)] = 3
    max_candidates: Annotated[StrictInt, Field(ge=1, le=MAX_CANDIDATES)] = MAX_CANDIDATES
    max_hypotheses: Annotated[StrictInt, Field(ge=2, le=MAX_HYPOTHESES)] = MAX_HYPOTHESES
    deadline_seconds: Annotated[StrictInt, Field(ge=1, le=180)] = 180
    invocation_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=5)] = 5
    max_output_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_OUTPUT_BYTES)] = MAX_OUTPUT_BYTES
    max_context_bytes: Annotated[
        StrictInt, Field(ge=1, le=MAX_MODEL_CONTEXT_BYTES)
    ] = MAX_MODEL_CONTEXT_BYTES


class BudgetUsage(Contract):
    pairs: Annotated[StrictInt, Field(ge=0, le=6)]
    confirmations: Annotated[StrictInt, Field(ge=0, le=1)]
    sandbox_invocations: Annotated[StrictInt, Field(ge=0, le=MAX_ATTEMPTS)]
    meta_requests: Annotated[StrictInt, Field(ge=0, le=2)]
    snap_requests: Annotated[StrictInt, Field(ge=0, le=3)]
    candidates_generated: Annotated[StrictInt, Field(ge=0, le=MAX_CANDIDATES)]


class Budget(Contract):
    """Limits plus consumed counts, deadline, wall clock and cost (or unknown)."""

    limits: BudgetLimits
    used: BudgetUsage
    deadline_at: UtcTimestamp
    wall_clock_ms: NonNegInt | None
    cost: CostRecord

    @model_validator(mode="after")
    def _within_limits(self) -> Self:
        pairs = (
            ("pairs", self.used.pairs, self.limits.max_pairs),
            ("confirmations", self.used.confirmations, self.limits.max_confirmations),
            ("sandbox_invocations", self.used.sandbox_invocations,
             self.limits.max_sandbox_invocations),
            ("meta_requests", self.used.meta_requests, self.limits.max_meta_requests),
            ("snap_requests", self.used.snap_requests, self.limits.max_snap_requests),
            ("candidates_generated", self.used.candidates_generated, self.limits.max_candidates),
        )
        for name, used, limit in pairs:
            if used > limit:
                raise ValueError(f"budget usage {name}={used} exceeds limit {limit}")
        return self

    @property
    def remaining_pairs(self) -> int:
        return self.limits.max_pairs - self.used.pairs


class Coverage(Contract):
    required: CoverageTags
    observed: CoverageTags
    missing: CoverageTags
    #: Distinct usable paired matches (base = head = expected) by input hash.
    distinct_usable_pairs: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    coverage_guard_used: StrictBool

    @model_validator(mode="after")
    def _missing_is_difference(self) -> Self:
        expected = sorted(set(self.required) - set(self.observed))
        if list(self.missing) != expected:
            raise ValueError("missing must equal required - observed (sorted)")
        return self

    @property
    def complete(self) -> bool:
        return not self.missing


class EvidenceCounts(Contract):
    matches: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    head_mismatches: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    shared_mismatches: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    base_mismatches: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    excluded: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]
    confirmations: Annotated[StrictInt, Field(ge=0, le=1)]


class RunStatusDetail(Contract):
    """Optional status detail: mode, coverage progress and short evidence counts."""

    run_kind: RunKind
    execution_mode: ExecutionMode
    repo_full_name: Annotated[str, StringConstraints(strict=True, max_length=201)] | None
    pr_number: PrNumber | None
    base_sha: Sha40 | None
    head_sha: Sha40 | None
    coverage: Coverage | None
    evidence: EvidenceCounts
    provider_modes: dict[ProviderName, ProviderMode]
    parent_receipt_sha256: Sha256Hex | None


class RunStatus(Contract):
    run_id: RunId
    status: RunState
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    is_stale: StrictBool | None
    attempts_used: Annotated[StrictInt, Field(ge=0, le=MAX_ATTEMPTS)]
    budget: Budget
    verdict: Verdict | None
    reason_code: ReasonCode | None
    receipt_sha256: Sha256Hex | None
    detail: RunStatusDetail | None = None

    @model_validator(mode="after")
    def _terminal_consistency(self) -> Self:
        if self.status in ACTIVE_STATES and self.verdict is not None:
            raise ValueError("active runs have no verdict")
        if self.status is RunState.FAILED and self.verdict is not None:
            raise ValueError("failed runs have no valid verdict")
        if self.status is RunState.COMPLETED and self.verdict is None:
            raise ValueError("completed runs carry a verdict")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self


class ErrorBody(Contract):
    code: ReasonCode
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=300)]
    request_id: SafeToken
    retryable: StrictBool


class ErrorEnvelope(Contract):
    error: ErrorBody


# ---------------------------------------------------------------------------
# Hypotheses (Meta) and planning context
# ---------------------------------------------------------------------------


class SourceRef(Contract):
    path: RelativePath
    line: Annotated[StrictInt, Field(ge=1, le=100_000)]


class PlanConstraints(Contract):
    min_items: ItemCount | None
    max_items: ItemCount | None
    discount_percent: DiscountPercent | None
    require_fractional_cent: StrictBool | None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if (self.min_items is not None and self.max_items is not None
                and self.min_items > self.max_items):
            raise ValueError("min_items must be <= max_items")
        return self


class HypothesisProposal(Contract):
    mode: HypothesisMode
    rationale: ShortText
    source_refs: Annotated[list[SourceRef], Field(max_length=4)]
    suggested_strategy: Strategy
    constraints: PlanConstraints


class HypothesisPlanPayload(Contract):
    """Exact provider-facing JSON (Meta structured output). No IDs, no provenance."""

    hypotheses: Annotated[list[HypothesisProposal], Field(max_length=MAX_PROPOSALS)]


class ProviderProvenance(Contract):
    """Sanitized metadata for one provider interaction (no bodies, no credentials)."""

    provider: ProviderName
    mode: ProviderMode
    model_id: SafeToken | None
    request_id: SafeToken | None
    latency_ms: NonNegInt | None
    attempts: Annotated[StrictInt, Field(ge=0, le=3)]
    reason_code: ReasonCode | None


class HypothesisPlan(HypothesisPlanPayload):
    """``MetaPort.propose`` result: validated proposals plus provenance."""

    provenance: ProviderProvenance


class ContextFile(Contract):
    path: RelativePath
    start_line: Annotated[StrictInt, Field(ge=1, le=100_000)]
    text: Annotated[str, StringConstraints(strict=True, max_length=MAX_MODEL_CONTEXT_BYTES)]

    @property
    def end_line(self) -> int:
        return self.start_line + max(self.text.count("\n") + (0 if self.text.endswith("\n")
                                                               else 1), 1) - 1


class PlanningContext(Contract):
    """Bounded, sanitized model context. Repo text is data, never instructions."""

    run_id: RunId
    contract_id: Literal["checkout-rounding@1"]
    entrypoint: Literal["checkout.total_cents"]
    contract_text: Annotated[str, StringConstraints(strict=True, max_length=4096)]
    files: Annotated[list[ContextFile], Field(max_length=4)]
    diff: Annotated[str, StringConstraints(strict=True, max_length=MAX_MODEL_CONTEXT_BYTES)]
    supported_modes: Annotated[list[HypothesisMode], Field(min_length=1, max_length=3)]
    max_hypotheses: Annotated[StrictInt, Field(ge=1, le=MAX_PROPOSALS)]

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        total = len(self.contract_text.encode()) + len(self.diff.encode()) + sum(
            len(f.text.encode()) for f in self.files
        )
        if total > MAX_MODEL_CONTEXT_BYTES:
            raise ValueError(f"model context {total} bytes exceeds {MAX_MODEL_CONTEXT_BYTES}")
        return self

    def contains_ref(self, ref: SourceRef) -> bool:
        """True when ``ref`` points inside a supplied context file."""
        return any(f.path == ref.path and f.start_line <= ref.line <= f.end_line
                   for f in self.files)


class RetainedHypothesis(Contract):
    id: HypothesisId
    source: HypothesisSource
    rationale: ShortText | None
    suggested_strategy: Strategy | None
    constraints: PlanConstraints | None
    raw_prior_weight: Probability


class HypothesisSummary(Contract):
    """Receipt record of retained hypotheses and the persisted initial prior."""

    plan_source: PlanSource
    prior_policy: VersionTag
    retained: Annotated[list[RetainedHypothesis], Field(min_length=2, max_length=MAX_HYPOTHESES)]
    initial_prior: ProbabilityVector
    rejected_proposals: Annotated[StrictInt, Field(ge=0, le=MAX_PROPOSALS)]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [h.id for h in self.retained]
        if len(set(ids)) != len(ids):
            raise ValueError("retained hypotheses must be unique")
        if not set(ids) >= MANDATORY_HYPOTHESES:
            raise ValueError("H0 and other are mandatory")
        if set(self.initial_prior) != set(ids):
            raise ValueError("initial_prior keys must equal retained hypothesis IDs")
        return self


class Belief(Contract):
    probabilities: ProbabilityVector
    prior_policy: VersionTag
    likelihood_policy: VersionTag
    updates: Annotated[StrictInt, Field(ge=0, le=MAX_PROBES)]

    @model_validator(mode="after")
    def _mandatory(self) -> Self:
        if not set(self.probabilities) >= MANDATORY_HYPOTHESES:
            raise ValueError("belief must include H0 and other")
        return self


class InitialBelief(Contract):
    belief: Belief
    hypotheses: HypothesisSummary


# ---------------------------------------------------------------------------
# Candidates, routing and selection
# ---------------------------------------------------------------------------


class CandidateInput(Contract):
    """Legal input of ``checkout-rounding@1``."""

    prices_cents: Annotated[list[PriceCents], Field(max_length=MAX_ITEMS)]
    discount_percent: DiscountPercent

    def as_input(self) -> CandidateInput:
        return CandidateInput(prices_cents=list(self.prices_cents),
                              discount_percent=self.discount_percent)


class Candidate(CandidateInput):
    input_sha256: Sha256Hex
    strategy: Strategy
    is_anchor: StrictBool
    coverage_tags: CoverageTags
    generator_version: VersionTag

    @model_validator(mode="after")
    def _hash_matches(self) -> Self:
        if self.input_sha256 != input_sha256(self):
            raise ValueError("input_sha256 does not match canonical input hash")
        return self


class CandidateBank(Contract):
    generator_version: VersionTag
    seed: Seed
    candidates: Annotated[list[Candidate], Field(max_length=MAX_CANDIDATES)]
    bank_sha256: Sha256Hex

    @model_validator(mode="after")
    def _unique_and_hashed(self) -> Self:
        hashes = [c.input_sha256 for c in self.candidates]
        if len(set(hashes)) != len(hashes):
            raise ValueError("candidate bank must be deduplicated by input hash")
        if sum(c.is_anchor for c in self.candidates) > MAX_COMMON_ANCHORS:
            raise ValueError("too many common anchors")
        for strategy in Strategy:
            count = sum(1 for c in self.candidates
                        if c.strategy is strategy and not c.is_anchor)
            if count > MAX_CANDIDATES_PER_STRATEGY:
                raise ValueError(f"too many candidates for {strategy}")
        if self.bank_sha256 != pool_sha256(self.candidates):
            raise ValueError("bank_sha256 does not match candidate hashes")
        return self

    def pool_for(self, strategy: Strategy, used_input_sha256: Iterable[str]) -> list[Candidate]:
        """Unseen candidates of ``strategy`` plus unused common anchors, bank order."""
        used = set(used_input_sha256)
        return [c for c in self.candidates if c.input_sha256 not in used
                and (c.is_anchor or c.strategy is strategy)]

    def eligible_strategies(self, used_input_sha256: Iterable[str]) -> tuple[Strategy, ...]:
        """Strategies with at least one unseen non-anchor candidate, in enum order."""
        used = set(used_input_sha256)
        return tuple(s for s in Strategy if any(
            c.strategy is s and not c.is_anchor and c.input_sha256 not in used
            for c in self.candidates))


class DecisionState(Contract):
    """Structured bounded state for ``RoutePort.choose``; never raw repo text."""

    run_id: RunId
    contract_id: Literal["checkout-rounding@1"]
    step: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)]
    remaining_pairs: Annotated[StrictInt, Field(ge=0, le=6)]
    snap_requests_remaining: Annotated[StrictInt, Field(ge=0, le=3)]
    observed_coverage: CoverageTags
    missing_coverage: CoverageTags
    unseen_candidates: dict[Strategy, Annotated[StrictInt, Field(ge=0, le=MAX_CANDIDATES)]]
    last_outcome: BinaryOutcome | None
    belief: ProbabilityVector


class RouteDecision(Contract):
    choice: Strategy
    eligible: Annotated[list[Strategy], Field(min_length=1, max_length=3)]
    mode: RouteMode
    model_id: SafeToken | None
    probabilities: dict[Strategy, Probability] | None
    confidence: Probability | None
    #: Operator-only sanitized provider request ID; dropped from public projections.
    provider_request_id: SafeToken | None
    latency_ms: NonNegInt | None
    reason_code: RouteReasonCode

    @model_validator(mode="after")
    def _agree(self) -> Self:
        if len(set(self.eligible)) != len(self.eligible):
            raise ValueError("eligible actions must be unique")
        if self.choice not in self.eligible:
            raise ValueError("choice must be one of the eligible actions")
        if self.probabilities is not None:
            if not set(self.probabilities) <= set(self.eligible):
                raise ValueError("probabilities must be keyed by eligible actions")
            total = math.fsum(self.probabilities.values())
            if abs(total - 1.0) > ROUTE_PROBABILITY_TOLERANCE:
                raise ValueError("route probabilities must be normalized within 0.01")
        if self.mode is RouteMode.LIVE and self.model_id is None:
            raise ValueError("live decisions record model_id")
        if self.mode is not RouteMode.LIVE and self.provider_request_id is not None:
            raise ValueError("only live decisions carry a provider request ID")
        return self


class PublicRouteDecision(Contract):
    choice: Strategy
    eligible: Annotated[list[Strategy], Field(min_length=1, max_length=3)]
    mode: RouteMode
    model_id: SafeToken | None
    probabilities: dict[Strategy, Probability] | None
    confidence: Probability | None
    latency_ms: NonNegInt | None
    reason_code: RouteReasonCode


class SelectionPolicy(Contract):
    likelihood_policy: VersionTag = LIKELIHOOD_POLICY
    selection_policy: VersionTag = SELECTION_POLICY
    eig_round_decimals: Annotated[StrictInt, Field(ge=1, le=15)] = MEASUREMENT_DECIMALS
    negative_eig_tolerance: Annotated[Measurement, Field(ge=0.0, le=1e-6)] = 1e-12
    remaining_pairs: Annotated[StrictInt, Field(ge=0, le=6)]
    missing_coverage: CoverageTags


class ProbeSelection(Contract):
    candidate: Candidate
    candidate_pool_sha256: Sha256Hex
    pool_size: Annotated[StrictInt, Field(ge=1, le=MAX_CANDIDATES)]
    eig_bits: Annotated[Measurement, Field(ge=0.0, le=3.0)]
    p_mismatch: Probability
    prior: ProbabilityVector
    likelihood_mismatch: LikelihoodVector
    predictions: PredictionVector
    reason: SelectionReason

    @model_validator(mode="after")
    def _keys(self) -> Self:
        if set(self.likelihood_mismatch) != set(self.prior) or set(self.predictions) != set(
                self.prior):
            raise ValueError("selection vectors must share hypothesis keys")
        return self


# ---------------------------------------------------------------------------
# Sources, execution and oracle
# ---------------------------------------------------------------------------


class SourceFile(Contract):
    side: Literal["base", "head"]
    commit_sha: Sha40
    path: Literal["checkout.py"]
    blob_sha: Sha40
    #: Exact UTF-8 source text; its UTF-8 bytes hash to ``content_sha256``.
    content: Annotated[str, StringConstraints(strict=True, max_length=MAX_SOURCE_BYTES)]
    content_sha256: Sha256Hex
    size_bytes: Annotated[StrictInt, Field(ge=0, le=MAX_SOURCE_BYTES)]

    @model_validator(mode="after")
    def _verified(self) -> Self:
        data = self.content.encode("utf-8")
        if len(data) != self.size_bytes or len(data) > MAX_SOURCE_BYTES:
            raise ValueError("size_bytes mismatch or oversize source")
        if sha256_hex(data) != self.content_sha256:
            raise ValueError("content_sha256 mismatch")
        if git_blob_sha1(data) != self.blob_sha:
            raise ValueError("blob_sha mismatch")
        return self


class SourceBundle(Contract):
    revision: PinnedRevision
    base: SourceFile
    head: SourceFile
    context: PlanningContext

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if self.base.side != "base" or self.head.side != "head":
            raise ValueError("bundle sides are base and head")
        if (self.base.commit_sha != self.revision.base_sha
                or self.head.commit_sha != self.revision.head_sha):
            raise ValueError("source files must be bound to the pinned SHAs")
        return self


class ResourcePolicy(Contract):
    execution_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=5)] = 5
    control_deadline_seconds: Annotated[StrictInt, Field(ge=1, le=60)] = 30
    max_output_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_OUTPUT_BYTES)] = MAX_OUTPUT_BYTES
    cpu_count: Annotated[StrictInt, Field(ge=1, le=2)] = 1
    memory_mib: Annotated[StrictInt, Field(ge=64, le=1024)] = 256
    max_processes: Annotated[StrictInt, Field(ge=1, le=64)] = 16


class ExecutionRequest(Contract):
    """One sandbox invocation. Carries no expected result and no oracle data."""

    run_id: RunId
    attempt_id: AttemptId
    side: Side
    commit_sha: Sha40
    source_path: Literal["checkout.py"]
    source_content: Annotated[str, StringConstraints(strict=True, max_length=MAX_SOURCE_BYTES)]
    source_sha256: Sha256Hex
    input: CandidateInput
    input_sha256: Sha256Hex
    entrypoint: Literal["checkout.total_cents"]
    resources: ResourcePolicy
    snapshot_id: SafeToken
    image_digest: ImageDigest
    harness_version: VersionTag
    harness_sha256: Sha256Hex

    @model_validator(mode="after")
    def _hashes(self) -> Self:
        if sha256_hex(self.source_content.encode("utf-8")) != self.source_sha256:
            raise ValueError("source_sha256 mismatch")
        if input_sha256(self.input) != self.input_sha256:
            raise ValueError("input_sha256 mismatch")
        return self


class SandboxObservation(Contract):
    attempt_id: AttemptId
    side: Side
    commit_sha: Sha40
    source_sha256: Sha256Hex
    input_sha256: Sha256Hex
    runner_mode: RunnerMode
    snapshot_id: SafeToken
    image_digest: ImageDigest
    harness_sha256: Sha256Hex
    #: Operator-only; never present in public projections.
    sandbox_id: SafeToken | None
    started_at: UtcTimestamp
    duration_ms: NonNegInt
    outcome: ObservationOutcome
    value: Int64 | None
    exit_code: Int64 | None
    stdout_sha256: Sha256Hex | None
    stderr_sha256: Sha256Hex | None
    truncated: StrictBool

    @model_validator(mode="after")
    def _value_only_on_ok(self) -> Self:
        if (self.outcome is ObservationOutcome.OK) != (self.value is not None):
            raise ValueError("value is an integer only when outcome is ok")
        return self

    @property
    def usable(self) -> bool:
        return self.outcome is ObservationOutcome.OK


class PublicObservation(Contract):
    """``SandboxObservation`` without operator identifiers (no ``sandbox_id``)."""

    attempt_id: AttemptId
    side: Side
    commit_sha: Sha40
    source_sha256: Sha256Hex
    input_sha256: Sha256Hex
    runner_mode: RunnerMode
    snapshot_id: SafeToken
    image_digest: ImageDigest
    harness_sha256: Sha256Hex
    started_at: UtcTimestamp
    duration_ms: NonNegInt
    outcome: ObservationOutcome
    value: Int64 | None
    exit_code: Int64 | None
    stdout_sha256: Sha256Hex | None
    stderr_sha256: Sha256Hex | None
    truncated: StrictBool


class IsolationAttestation(Contract):
    """Runner preflight result; live untrusted execution requires ``verified``."""

    runner_mode: RunnerMode
    status: IsolationStatus
    snapshot_id: SafeToken
    image_digest: ImageDigest
    harness_sha256: Sha256Hex
    network_block_all: StrictBool
    egress_denied: StrictBool | None
    metadata_denied: StrictBool | None
    checked_at: UtcTimestamp
    config_fingerprint: Sha256Hex
    reason_code: ReasonCode | None


class OracleResult(Contract):
    contract_id: Literal["checkout-rounding@1"]
    contract_sha256: Sha256Hex
    oracle_version: VersionTag
    input_sha256: Sha256Hex
    trust_tier: TrustTier
    expected_cents: Int64 | None
    status: OracleStatus

    @model_validator(mode="after")
    def _expected_only_on_ok(self) -> Self:
        if (self.status is OracleStatus.OK) != (self.expected_cents is not None):
            raise ValueError("expected_cents is present only when status is ok")
        return self


# ---------------------------------------------------------------------------
# Probe records
# ---------------------------------------------------------------------------


class _ProbeChecks:
    probe_no: int
    candidate: Candidate
    prior: dict[HypothesisId, float]
    posterior: dict[HypothesisId, float] | None
    likelihood_mismatch: dict[HypothesisId, float]
    predictions: dict[HypothesisId, int | None]
    evidence_kind: EvidenceKind
    exclusion_reason: ExclusionReason | None

    def _check_probe(self, base: Any, head: Any, confirmation: Any, oracle: OracleResult) -> None:
        if base.side is not Side.BASE or head.side is not Side.HEAD:
            raise ValueError("base/head observations have the wrong side")
        if confirmation is not None and confirmation.side is not Side.CONFIRM:
            raise ValueError("confirmation must have side=confirm")
        observations = [o for o in (base, head, confirmation) if o is not None]
        digest = self.candidate.input_sha256
        if oracle.input_sha256 != digest or any(o.input_sha256 != digest for o in observations):
            raise ValueError("oracle and observations must bind the candidate input hash")
        if confirmation is not None and (confirmation.commit_sha != head.commit_sha
                                         or confirmation.source_sha256 != head.source_sha256):
            raise ValueError("confirmation must replay the head source")
        ids = [o.attempt_id for o in observations]
        if len(set(ids)) != len(ids):
            raise ValueError("each observation needs a fresh attempt ID")
        excluded = self.exclusion_reason is not None
        if excluded != (self.posterior is None):
            raise ValueError("posterior is null exactly when the probe is excluded")
        if excluded and self.evidence_kind in (EvidenceKind.MATCH, EvidenceKind.HEAD_MISMATCH):
            raise ValueError("usable evidence kinds cannot carry an exclusion reason")
        if not excluded and self.evidence_kind not in (EvidenceKind.MATCH,
                                                       EvidenceKind.HEAD_MISMATCH):
            raise ValueError("only match/head_mismatch update the posterior")
        keys = set(self.prior)
        if set(self.likelihood_mismatch) != keys or set(self.predictions) != keys or (
                self.posterior is not None and set(self.posterior) != keys):
            raise ValueError("model vectors must share hypothesis keys")


class ProbeRecord(Contract, _ProbeChecks):
    """One committed logical probe (operator view, includes operator identifiers)."""

    probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)]
    candidate: Candidate
    candidate_pool_sha256: Sha256Hex
    selection_reason: SelectionReason
    route: RouteDecision
    oracle: OracleResult
    base: SandboxObservation
    head: SandboxObservation
    confirmation: SandboxObservation | None
    confirmation_status: ConfirmationStatus
    prior: ProbabilityVector
    posterior: ProbabilityVector | None
    likelihood_mismatch: LikelihoodVector
    predictions: PredictionVector
    eig_bits: Annotated[Measurement, Field(ge=0.0, le=3.0)]
    evidence_kind: EvidenceKind
    exclusion_reason: ExclusionReason | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        self._check_probe(self.base, self.head, self.confirmation, self.oracle)
        return self


class PublicProbeRecord(Contract, _ProbeChecks):
    probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)]
    candidate: Candidate
    candidate_pool_sha256: Sha256Hex
    selection_reason: SelectionReason
    route: PublicRouteDecision
    oracle: OracleResult
    base: PublicObservation
    head: PublicObservation
    confirmation: PublicObservation | None
    confirmation_status: ConfirmationStatus
    prior: ProbabilityVector
    posterior: ProbabilityVector | None
    likelihood_mismatch: LikelihoodVector
    predictions: PredictionVector
    eig_bits: Annotated[Measurement, Field(ge=0.0, le=3.0)]
    evidence_kind: EvidenceKind
    exclusion_reason: ExclusionReason | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        self._check_probe(self.base, self.head, self.confirmation, self.oracle)
        return self


class UnpairedAttemptSummary(Contract):
    attempt_id: AttemptId
    side: Side
    input_sha256: Sha256Hex
    status: AttemptStatus
    outcome: ObservationOutcome | None
    charged: StrictBool
    reason_code: ReasonCode | None


# ---------------------------------------------------------------------------
# Verdict, replay and receipt
# ---------------------------------------------------------------------------


class VerdictResult(Contract):
    verdict: Verdict
    reason_code: ReasonCode
    counterexample_probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)] | None
    wording: Annotated[str, StringConstraints(strict=True, max_length=300)]
    next_requirement: ShortText | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        confirmed = self.verdict in (Verdict.CONFIRMED_REGRESSION,
                                     Verdict.CONFIRMED_EXISTING_DEFECT)
        if confirmed != (self.counterexample_probe_no is not None):
            raise ValueError("counterexample_probe_no is set exactly for confirmed verdicts")
        if self.wording != VERDICT_WORDING[self.verdict]:
            raise ValueError("wording must be the frozen verdict wording")
        return self


class SourceHashes(Contract):
    base: Sha256Hex
    head: Sha256Hex


class PolicyVersions(Contract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    canonical_serializer: VersionTag = CANONICAL_SERIALIZER_VERSION
    prior_policy: VersionTag = PRIOR_POLICY
    likelihood_policy: VersionTag = LIKELIHOOD_POLICY
    generator_version: VersionTag = GENERATOR_VERSION
    selection_policy: VersionTag = SELECTION_POLICY
    verdict_policy: VersionTag = VERDICT_POLICY
    route_fallback_policy: VersionTag = ROUTE_FALLBACK_POLICY
    oracle_version: VersionTag = ORACLE_VERSION
    harness_version: VersionTag = HARNESS_VERSION


class ProviderUsage(Contract):
    mode: ProviderMode
    model_id: SafeToken | None
    calls: Annotated[StrictInt, Field(ge=0, le=MAX_ATTEMPTS)]
    fallbacks: Annotated[StrictInt, Field(ge=0, le=MAX_ATTEMPTS)]
    reason_code: ReasonCode | None


_REPLAY_COMMAND = re.compile(
    r"^uv run --locked python -m proofhunter replay (--offline|--profile live) "
    r"--receipt \./receipt-[0-9a-f-]{36}\.json --probe (counterexample|[1-9][0-9]?)$"
)


def build_replay_command(execution_mode: ExecutionMode, run_id: str, probe: int | None) -> str:
    """Shell-ready replay command from trusted flags and validated identifiers only.

    ``probe=None`` selects the receipt's confirming probe (``--probe counterexample``).
    """
    if not re.match(_UUID4, run_id):
        raise ValueError("run_id must be a lowercase UUIDv4")
    if probe is not None and not 1 <= probe <= MAX_PROBES:
        raise ValueError("probe out of range")
    flag = "--offline" if execution_mode is ExecutionMode.OFFLINE_FIXTURE else "--profile live"
    selector = "counterexample" if probe is None else str(probe)
    return (f"uv run --locked python -m proofhunter replay {flag} "
            f"--receipt ./receipt-{run_id}.json --probe {selector}")


class Replay(Contract):
    probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)]
    input: CandidateInput
    input_sha256: Sha256Hex
    base_sha: Sha40
    head_sha: Sha40
    base_source_sha256: Sha256Hex
    head_source_sha256: Sha256Hex
    harness_version: VersionTag
    harness_sha256: Sha256Hex
    contract_id: Literal["checkout-rounding@1"]
    contract_sha256: Sha256Hex
    oracle_version: VersionTag
    snapshot_id: SafeToken
    image_digest: ImageDigest
    execution_mode: ExecutionMode
    command: Annotated[str, StringConstraints(strict=True, max_length=256)]

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if input_sha256(self.input) != self.input_sha256:
            raise ValueError("replay input hash mismatch")
        match = _REPLAY_COMMAND.match(self.command)
        if match is None:
            raise ValueError("replay command must come from build_replay_command")
        offline = match.group(1) == "--offline"
        if offline != (self.execution_mode is ExecutionMode.OFFLINE_FIXTURE):
            raise ValueError("replay command flag must match execution_mode")
        return self


class ReceiptV1(Contract):
    """Sealed public-safe receipt. Contains no prompts, sources, raw output bytes,
    operator identifiers (installation or sandbox IDs, provider request IDs) or
    credentials. ``receipt_sha256`` is ``null`` until sealed."""

    schema_version: Literal["1.0"]
    run_id: RunId
    run_kind: RunKind
    parent_receipt_sha256: Sha256Hex | None
    pinned_revision: PublicRevision | None
    source_file_sha256: SourceHashes | None
    enrollment_fingerprint: Sha256Hex | None
    execution_mode: ExecutionMode
    providers: dict[ProviderName, ProviderUsage]
    budget: Budget
    seed: Seed
    policy_versions: PolicyVersions
    hypotheses: HypothesisSummary | None
    probes: Annotated[list[PublicProbeRecord], Field(max_length=MAX_PROBES)]
    unpaired_attempt_summaries: Annotated[list[UnpairedAttemptSummary],
                                          Field(max_length=MAX_ATTEMPTS)]
    coverage: Coverage
    #: ``null`` only for a ``failed`` internal job (sanitized ``reason_code``).
    verdict: Verdict | None
    reason_code: ReasonCode
    counterexample_probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)] | None
    replay: Replay | None
    created_at: UtcTimestamp
    completed_at: UtcTimestamp
    receipt_sha256: Sha256Hex | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        numbers = [p.probe_no for p in self.probes]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("probe numbers must be 1..n in order")
        if self.pinned_revision is None and self.replay is not None:
            raise ValueError("no executable replay without a pinned revision")
        if (self.run_kind is RunKind.REPLAY) != (self.parent_receipt_sha256 is not None):
            raise ValueError("replay runs (and only replay runs) carry parent_receipt_sha256")
        confirmed = self.verdict in (Verdict.CONFIRMED_REGRESSION,
                                     Verdict.CONFIRMED_EXISTING_DEFECT)
        if confirmed != (self.counterexample_probe_no is not None):
            raise ValueError("counterexample_probe_no is set exactly for confirmed verdicts")
        if confirmed and (self.replay is None
                          or self.replay.probe_no != self.counterexample_probe_no):
            raise ValueError("confirmed verdicts bind a replay of the counterexample probe")
        if self.completed_at < self.created_at:
            raise ValueError("completed_at precedes created_at")
        if self.pinned_revision is not None and self.replay is not None and (
                self.replay.base_sha != self.pinned_revision.base_sha
                or self.replay.head_sha != self.pinned_revision.head_sha):
            raise ValueError("replay must bind the pinned SHAs")
        offline_modes = {ProviderMode.OFFLINE_FIXTURE, ProviderMode.STUB, ProviderMode.NOT_CALLED,
                         ProviderMode.DISABLED, ProviderMode.FALLBACK, ProviderMode.POLICY}
        if self.execution_mode is ExecutionMode.OFFLINE_FIXTURE and any(
                u.mode not in offline_modes for u in self.providers.values()):
            raise ValueError("offline_fixture receipts cannot claim live provider modes")
        if self.execution_mode is ExecutionMode.OFFLINE_FIXTURE and any(
                o.runner_mode is not RunnerMode.OFFLINE_FIXTURE
                for p in self.probes for o in (p.base, p.head, p.confirmation) if o is not None):
            raise ValueError("offline_fixture receipts contain only offline_fixture observations")
        if self.receipt_sha256 is not None and self.receipt_sha256 != compute_receipt_sha256(self):
            raise ValueError("receipt_sha256 does not match the canonical receipt hash")
        return self


# ---------------------------------------------------------------------------
# Persistence, recovery, quotas and publishing
# ---------------------------------------------------------------------------


class RunLease(Contract):
    run_id: RunId
    owner: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
    epoch: Annotated[StrictInt, Field(ge=1, le=INT64_MAX)]
    expires_at: UtcTimestamp


class RunEvent(Contract):
    """Append-only, timestamped stage checkpoint or recovery event (sanitized)."""

    run_id: RunId
    seq: Annotated[StrictInt, Field(ge=1, le=INT64_MAX)]
    at: UtcTimestamp
    kind: RunEventKind
    from_state: RunState | None
    to_state: RunState | None
    reason_code: ReasonCode | None
    detail: Annotated[str, StringConstraints(strict=True, max_length=200)] | None

    @model_validator(mode="after")
    def _transition(self) -> Self:
        if self.kind is RunEventKind.STATE_CHANGED:
            if self.from_state is None or self.to_state is None:
                raise ValueError("state_changed requires from_state and to_state")
            if self.to_state not in STATE_TRANSITIONS[self.from_state]:
                raise ValueError(f"illegal transition {self.from_state} -> {self.to_state}")
        return self


class AttemptRecord(Contract):
    """Append-only raw attempt (distinct from logical observation keys)."""

    run_id: RunId
    attempt_id: AttemptId
    side: Side
    input_sha256: Sha256Hex
    probe_no: Annotated[StrictInt, Field(ge=1, le=MAX_PROBES)] | None
    status: AttemptStatus
    charged: StrictBool
    lease_epoch: Annotated[StrictInt, Field(ge=1, le=INT64_MAX)]
    created_at: UtcTimestamp
    observation: SandboxObservation | None
    reason_code: ReasonCode | None

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if self.observation is not None and (
                self.observation.attempt_id != self.attempt_id
                or self.observation.side is not self.side
                or self.observation.input_sha256 != self.input_sha256):
            raise ValueError("attempt observation must match attempt identity")
        if self.status is AttemptStatus.COMPLETED and self.observation is None:
            raise ValueError("completed attempts carry an observation")
        return self


class QuotaPolicy(Contract):
    accepted_runs_per_hour: Annotated[StrictInt, Field(ge=0, le=6)] = 6
    max_active_runs: Annotated[StrictInt, Field(ge=1, le=2)] = 2
    max_active_per_requester: Annotated[StrictInt, Field(ge=1, le=1)] = 1


class QuotaReservation(Contract):
    run_id: RunId
    requester_scope: Annotated[
        str, StringConstraints(strict=True, pattern=r"^[a-z0-9:._-]{1,128}$")
    ]
    reserved_at: UtcTimestamp
    window_started_at: UtcTimestamp
    run_kind: RunKind


class QuotaDecision(Contract):
    allowed: StrictBool
    reason_code: ReasonCode | None
    retry_after_seconds: Annotated[StrictInt, Field(ge=1, le=3600)] | None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.allowed != (self.reason_code is None and self.retry_after_seconds is None):
            raise ValueError("denied quota decisions carry reason_code and retry_after_seconds")
        return self


class CheckIntent(Contract):
    """Advisory Check Run intent stored transactionally with the receipt (outbox)."""

    run_id: RunId
    owner: PathSegment
    repo: PathSegment
    repo_id: PositiveId
    installation_id: PositiveId
    head_sha: Sha40
    external_id: RunId
    name: Literal["ProofHunter"]
    status: Literal["completed"]
    conclusion: CheckConclusion
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
    summary: Annotated[str, StringConstraints(strict=True, max_length=4000)]
    receipt_sha256: Sha256Hex
    execution_mode: Literal["live"]

    @model_validator(mode="after")
    def _external_id(self) -> Self:
        if self.external_id != self.run_id:
            raise ValueError("external_id must equal run_id")
        return self


def check_conclusion_for(verdict: Verdict | None) -> CheckConclusion:
    if verdict is Verdict.CONFIRMED_REGRESSION:
        return CheckConclusion.FAILURE
    if verdict is Verdict.NO_DEFECT_FOUND:
        return CheckConclusion.SUCCESS
    return CheckConclusion.NEUTRAL


class PublishResult(Contract):
    run_id: RunId
    status: PublishStatus
    check_run_id: PositiveId | None
    attempts: Annotated[StrictInt, Field(ge=0, le=10)]
    reason_code: ReasonCode | None
    published_at: UtcTimestamp | None


class GithubWebhookEvent(Contract):
    """Validated ``pull_request`` webhook delivery (after HMAC verification)."""

    delivery_id: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9-]{1,64}$")]
    event: Literal["pull_request"]
    action: WebhookAction
    installation_id: PositiveId
    repo_id: PositiveId
    owner: PathSegment
    repo: PathSegment
    number: PrNumber
    head_sha: Sha40
    base_sha: Sha40
    body_sha256: Sha256Hex


# ---------------------------------------------------------------------------
# Canonical serializer and hashing
# ---------------------------------------------------------------------------


class CanonicalEncodingError(ValueError):
    """Raised when a value cannot be canonically encoded."""


def _check_canonical(value: Any, path: str) -> None:
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise CanonicalEncodingError(f"{path}: integer outside signed 64-bit range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalEncodingError(f"{path}: NaN/Infinity is not allowed")
        if round(value, MEASUREMENT_DECIMALS) != value:
            raise CanonicalEncodingError(f"{path}: float must be rounded to 12 decimals")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_canonical(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalEncodingError(f"{path}: object keys must be strings")
            _check_canonical(item, f"{path}.{key}")
        return
    raise CanonicalEncodingError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Encode JSON-compatible data with ``ph-canonical-json-v1``.

    Accepts ``dict``/``list``/``tuple``/``str``/``int``/``float``/``bool``/``None``
    (use ``model.model_dump(mode="json")`` for DTOs). Raises
    ``CanonicalEncodingError`` for NaN/Infinity, unrounded floats, out-of-range
    integers, non-string keys or unsupported types.
    """
    _check_canonical(value, "$")
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
        return text.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as error:
        raise CanonicalEncodingError(str(error)) from error


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return sha256_hex(canonical_json(value))


def git_blob_sha1(data: bytes) -> str:
    """Git blob object ID of ``data`` (``sha1("blob <len>\\0" + data)``)."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def input_sha256(value: CandidateInput | Mapping[str, Any]) -> str:
    """Canonical hash of ``{"discount_percent", "prices_cents"}`` only."""
    if isinstance(value, BaseModel):
        data = {"discount_percent": value.discount_percent,
                "prices_cents": list(value.prices_cents)}
    else:
        data = {"discount_percent": value["discount_percent"],
                "prices_cents": list(value["prices_cents"])}
    return canonical_sha256(data)


def pool_sha256(candidates: Sequence[Candidate]) -> str:
    """Hash of a candidate pool: canonical JSON array of sorted input hashes."""
    return canonical_sha256(sorted(c.input_sha256 for c in candidates))


def compute_receipt_sha256(receipt: ReceiptV1) -> str:
    """Canonical SHA-256 of a receipt with ``receipt_sha256`` removed."""
    data = receipt.model_dump(mode="json")
    data.pop("receipt_sha256", None)
    return canonical_sha256(data)


def verify_receipt_sha256(receipt_json: bytes | str) -> bool:
    """Parse untrusted receipt JSON and check its stored hash. Never raises."""
    try:
        data = json.loads(receipt_json)
        if not isinstance(data, dict):
            return False
        stored = data.get("receipt_sha256")
        if not isinstance(stored, str):
            return False
        ReceiptV1.model_validate_json(receipt_json)
        data.pop("receipt_sha256")
        return canonical_sha256(data) == stored
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Projections and frozen built-ins
# ---------------------------------------------------------------------------


def public_revision(revision: PinnedRevision) -> PublicRevision:
    return PublicRevision(owner=revision.owner, repo=revision.repo, number=revision.number,
                          repo_id=revision.repo_id, base_sha=revision.base_sha,
                          head_sha=revision.head_sha, resolved_at=revision.resolved_at)


def public_route(route: RouteDecision) -> PublicRouteDecision:
    data = route.model_dump()
    data.pop("provider_request_id")
    return PublicRouteDecision.model_validate(data)


def public_observation(observation: SandboxObservation) -> PublicObservation:
    data = observation.model_dump()
    data.pop("sandbox_id")
    return PublicObservation.model_validate(data)


def public_probe(probe: ProbeRecord) -> PublicProbeRecord:
    return PublicProbeRecord(
        probe_no=probe.probe_no, candidate=probe.candidate,
        candidate_pool_sha256=probe.candidate_pool_sha256,
        selection_reason=probe.selection_reason, route=public_route(probe.route),
        oracle=probe.oracle, base=public_observation(probe.base),
        head=public_observation(probe.head),
        confirmation=None if probe.confirmation is None else public_observation(
            probe.confirmation),
        confirmation_status=probe.confirmation_status, prior=probe.prior,
        posterior=probe.posterior, likelihood_mismatch=probe.likelihood_mismatch,
        predictions=probe.predictions, eig_bits=probe.eig_bits,
        evidence_kind=probe.evidence_kind, exclusion_reason=probe.exclusion_reason,
    )


def _no_constraints() -> PlanConstraints:
    return PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                           require_fractional_cent=None)


def builtin_hypothesis_payload() -> HypothesisPlanPayload:
    """Deterministic built-in plan used on Meta failure or when only H0/other survive."""
    return HypothesisPlanPayload(hypotheses=[
        HypothesisProposal(
            mode=HypothesisMode.PER_ITEM_ROUNDING,
            rationale="Built-in: each line is discounted and rounded separately.",
            source_refs=[], suggested_strategy=Strategy.CONSTRAINT,
            constraints=PlanConstraints(min_items=2, max_items=None, discount_percent=None,
                                        require_fractional_cent=True),
        ),
        HypothesisProposal(
            mode=HypothesisMode.HALF_EVEN_ROUNDING,
            rationale="Built-in: half-cent ties round to even instead of half up.",
            source_refs=[], suggested_strategy=Strategy.BOUNDARY,
            constraints=PlanConstraints(min_items=None, max_items=None, discount_percent=None,
                                        require_fractional_cent=True),
        ),
        HypothesisProposal(
            mode=HypothesisMode.OTHER,
            rationale="Built-in: unmodeled deviation from the enrolled contract.",
            source_refs=[], suggested_strategy=Strategy.SEEDED_FUZZ,
            constraints=_no_constraints(),
        ),
    ])


#: Common anchors as ``(prices, discount)`` pairs (PRD section 4).
COMMON_ANCHORS: Final[tuple[tuple[tuple[int, ...], int], ...]] = (
    ((), 17),
    ((1, 2), 0),
    ((99,), 100),
    ((15, 15, 15), 10),
)
DEMO_SENTINEL: Final = ((15, 15, 15), 10)


__all__ = [name for name in dir() if not name.startswith("_")]
