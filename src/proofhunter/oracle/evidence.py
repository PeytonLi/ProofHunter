"""Exact outcome comparison and provenance binding for one probe pair.

Every sandbox observation is hostile data. A value counts only when the
observation is a well-formed, untruncated ``ok`` envelope whose provenance binds
the exact pinned commit, source bytes, input, snapshot, image and harness the
control plane requested. Comparison is exact integer equality against the T1
oracle; booleans and non-integers are never equal to an integer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from proofhunter.contracts import (
    BinaryOutcome,
    ConfirmationStatus,
    Enrollment,
    EvidenceKind,
    ExclusionReason,
    ExecutionMode,
    ObservationOutcome,
    OracleResult,
    OracleStatus,
    RunnerMode,
    Side,
    TrustTier,
)


class Observation(Protocol):
    """Fields shared by ``SandboxObservation`` and ``PublicObservation``."""

    @property
    def attempt_id(self) -> str: ...
    @property
    def side(self) -> Side: ...
    @property
    def commit_sha(self) -> str: ...
    @property
    def source_sha256(self) -> str: ...
    @property
    def input_sha256(self) -> str: ...
    @property
    def runner_mode(self) -> RunnerMode: ...
    @property
    def snapshot_id(self) -> str: ...
    @property
    def image_digest(self) -> str: ...
    @property
    def harness_sha256(self) -> str: ...
    @property
    def outcome(self) -> ObservationOutcome: ...
    @property
    def value(self) -> int | None: ...
    @property
    def truncated(self) -> bool: ...


_OUTCOME_EXCLUSIONS = {
    ObservationOutcome.TIMEOUT: ExclusionReason.TIMEOUT,
    ObservationOutcome.NONZERO: ExclusionReason.NONZERO_EXIT,
    ObservationOutcome.INVALID: ExclusionReason.INVALID_OUTPUT,
    ObservationOutcome.DENIED: ExclusionReason.DENIED,
    ObservationOutcome.INFRA_ERROR: ExclusionReason.INFRA_ERROR,
}
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


@dataclass(frozen=True, slots=True)
class ProvenanceBinding:
    """What the control plane requested for one run; observations must match it."""

    base_sha: str
    head_sha: str
    base_source_sha256: str
    head_source_sha256: str
    snapshot_id: str
    image_digest: str
    harness_sha256: str
    runner_mode: RunnerMode

    @classmethod
    def for_run(cls, *, base_sha: str, head_sha: str, base_source_sha256: str,
                head_source_sha256: str, enrollment: Enrollment,
                execution_mode: ExecutionMode) -> ProvenanceBinding:
        runner_mode = (RunnerMode.OFFLINE_FIXTURE
                       if execution_mode is ExecutionMode.OFFLINE_FIXTURE else RunnerMode.DAYTONA)
        return cls(base_sha=base_sha, head_sha=head_sha, base_source_sha256=base_source_sha256,
                   head_source_sha256=head_source_sha256, snapshot_id=enrollment.snapshot_id,
                   image_digest=enrollment.image_digest,
                   harness_sha256=enrollment.harness_sha256, runner_mode=runner_mode)

    def expects(self, side: Side) -> tuple[str, str]:
        """``(commit_sha, source_sha256)`` expected for ``side``; confirm replays head."""
        if side is Side.BASE:
            return self.base_sha, self.base_source_sha256
        return self.head_sha, self.head_source_sha256


@dataclass(frozen=True, slots=True)
class PairAssessment:
    """Classification of one probe; ``outcome`` is the Bayes observation or ``None``."""

    evidence_kind: EvidenceKind
    exclusion_reason: ExclusionReason | None
    confirmation_status: ConfirmationStatus
    outcome: BinaryOutcome | None

    @property
    def needs_confirmation(self) -> bool:
        return (self.evidence_kind in (EvidenceKind.HEAD_MISMATCH, EvidenceKind.SHARED_MISMATCH)
                and self.confirmation_status is ConfirmationStatus.NOT_ATTEMPTED)


def exact_value(observation: Observation) -> int | None:
    """The observed integer if the envelope is usable, else ``None``.

    Rejects non-``ok`` outcomes, truncated streams, booleans, non-integers and
    values outside signed 64-bit, even if a DTO was constructed without validation.
    """
    if observation.outcome is not ObservationOutcome.OK or observation.truncated is not False:
        return None
    value = observation.value
    if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
        return None
    return value


def trusted_expected(oracle: OracleResult) -> int | None:
    """Expected cents only for an ``ok`` T1 oracle result."""
    if oracle.status is not OracleStatus.OK or oracle.trust_tier is not TrustTier.T1:
        return None
    expected = oracle.expected_cents
    return expected if type(expected) is int else None


def provenance_ok(observation: Observation, side: Side, input_digest: str,
                  binding: ProvenanceBinding | None) -> bool:
    """Side and input always bind; with ``binding`` the full execution provenance binds."""
    if observation.side is not side or observation.input_sha256 != input_digest:
        return False
    if binding is None:
        return True
    commit, source = binding.expects(side)
    return (observation.commit_sha == commit and observation.source_sha256 == source
            and observation.snapshot_id == binding.snapshot_id
            and observation.image_digest == binding.image_digest
            and observation.harness_sha256 == binding.harness_sha256
            and observation.runner_mode is binding.runner_mode)


def _unusable_reason(observation: Observation) -> ExclusionReason:
    if observation.outcome is ObservationOutcome.OK:
        return ExclusionReason.INVALID_OUTPUT
    return _OUTCOME_EXCLUSIONS.get(observation.outcome, ExclusionReason.INFRA_ERROR)


def classify_pair(oracle: OracleResult, base: Observation, head: Observation, *,
                  input_digest: str | None = None,
                  binding: ProvenanceBinding | None = None
                  ) -> tuple[EvidenceKind, ExclusionReason | None]:
    """Compare base and head with the T1 expectation for the same input."""
    digest = oracle.input_sha256 if input_digest is None else input_digest
    if oracle.input_sha256 != digest:
        return EvidenceKind.EXCLUDED, ExclusionReason.TRUST_FAILURE
    expected = trusted_expected(oracle)
    if expected is None:
        return EvidenceKind.EXCLUDED, ExclusionReason.ORACLE_UNAVAILABLE
    if base.attempt_id == head.attempt_id:
        return EvidenceKind.EXCLUDED, ExclusionReason.TRUST_FAILURE
    for observation, side in ((base, Side.BASE), (head, Side.HEAD)):
        if not provenance_ok(observation, side, digest, binding):
            return EvidenceKind.EXCLUDED, ExclusionReason.TRUST_FAILURE
    if binding is None and (base.snapshot_id != head.snapshot_id
                            or base.image_digest != head.image_digest
                            or base.harness_sha256 != head.harness_sha256
                            or base.runner_mode is not head.runner_mode):
        return EvidenceKind.EXCLUDED, ExclusionReason.TRUST_FAILURE
    for observation in (base, head):
        if exact_value(observation) is None:
            return EvidenceKind.EXCLUDED, _unusable_reason(observation)
    base_value, head_value = exact_value(base), exact_value(head)
    if base_value != expected:
        if head_value == base_value:
            return EvidenceKind.SHARED_MISMATCH, ExclusionReason.BASE_MISMATCH
        return EvidenceKind.BASE_MISMATCH, ExclusionReason.BASE_MISMATCH
    if head_value == expected:
        return EvidenceKind.MATCH, None
    return EvidenceKind.HEAD_MISMATCH, None


def confirmation_status(head: Observation, confirmation: Observation | None, *,
                        binding: ProvenanceBinding | None = None) -> ConfirmationStatus:
    """Fresh head replay: same source, input and image, new attempt, same value."""
    if confirmation is None:
        return ConfirmationStatus.NOT_ATTEMPTED
    if (confirmation.attempt_id == head.attempt_id
            or not provenance_ok(confirmation, Side.CONFIRM, head.input_sha256, binding)
            or confirmation.commit_sha != head.commit_sha
            or confirmation.source_sha256 != head.source_sha256
            or confirmation.snapshot_id != head.snapshot_id
            or confirmation.image_digest != head.image_digest
            or confirmation.harness_sha256 != head.harness_sha256
            or confirmation.runner_mode is not head.runner_mode):
        return ConfirmationStatus.UNUSABLE
    value, original = exact_value(confirmation), exact_value(head)
    if value is None or original is None:
        return ConfirmationStatus.UNUSABLE
    return ConfirmationStatus.REPRODUCED if value == original else ConfirmationStatus.DISAGREED


def assess_probe(oracle: OracleResult, base: Observation, head: Observation,
                 confirmation: Observation | None = None, *, input_digest: str | None = None,
                 binding: ProvenanceBinding | None = None) -> PairAssessment:
    """Full assessment of one probe, including the Bayes observation if usable.

    Only ``match`` and ``head_mismatch`` feed a posterior update. The head
    confirmation never contributes a second update.
    """
    kind, exclusion = classify_pair(oracle, base, head, input_digest=input_digest,
                                    binding=binding)
    status = ConfirmationStatus.NOT_ATTEMPTED
    if kind in (EvidenceKind.HEAD_MISMATCH, EvidenceKind.SHARED_MISMATCH):
        status = confirmation_status(head, confirmation, binding=binding)
    elif confirmation is not None:
        status = ConfirmationStatus.UNUSABLE
    outcome = None
    if exclusion is None:
        outcome = BinaryOutcome.MATCH if kind is EvidenceKind.MATCH else BinaryOutcome.MISMATCH
    return PairAssessment(evidence_kind=kind, exclusion_reason=exclusion,
                          confirmation_status=status, outcome=outcome)
