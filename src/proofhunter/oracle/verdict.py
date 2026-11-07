"""Coverage and verdict rules (``verdict-rules-v1``, PRD section 5).

The classifier never trusts stored evidence labels: it recomputes every probe
from its oracle result and observations, checks cross-probe provenance, and
returns ``inconclusive`` with ``trust_failure`` when a record does not agree with
its own evidence. Posterior values never set a verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from proofhunter.contracts import (
    REQUIRED_COVERAGE_TAGS,
    VERDICT_WORDING,
    ConfirmationStatus,
    Coverage,
    CoverageTag,
    EvidenceKind,
    ExclusionReason,
    OracleStatus,
    ProbeRecord,
    PublicProbeRecord,
    ReasonCode,
    TrustTier,
    Verdict,
    VerdictResult,
)
from proofhunter.oracle.evidence import PairAssessment, assess_probe

AnyProbe = ProbeRecord | PublicProbeRecord

#: Stop reasons that block an evidence-backed clean result.
BLOCKING_STOP_REASONS: Final = frozenset({
    ReasonCode.ISOLATION_UNVERIFIED, ReasonCode.PROVIDER_UNAVAILABLE, ReasonCode.TRUST_FAILURE,
    ReasonCode.REVISION_UNAVAILABLE, ReasonCode.UNSUPPORTED_CONTRACT, ReasonCode.SOURCE_INVALID,
    ReasonCode.DEADLINE_EXCEEDED, ReasonCode.ORACLE_UNAVAILABLE, ReasonCode.INTERNAL_ERROR,
    ReasonCode.LEASE_LOST, ReasonCode.FORK_UNSUPPORTED, ReasonCode.REPO_NOT_ENROLLED,
})
#: Stop reasons compatible with a completed evidence search.
ORDINARY_STOP_REASONS: Final = frozenset({
    ReasonCode.BUDGET_EXHAUSTED, ReasonCode.INPUTS_EXHAUSTED, ReasonCode.COVERAGE_COMPLETE,
    ReasonCode.CONFIRMED_EVIDENCE,
})
MIN_CLEAN_PAIRS: Final = 3


class EvidenceInconsistency(ValueError):
    """A probe record disagrees with its own evidence or with sibling probes."""


@dataclass(frozen=True, slots=True)
class AuditedProbe:
    probe: AnyProbe
    assessment: PairAssessment
    duplicate: bool


def _recompute(probe: AnyProbe) -> PairAssessment:
    return assess_probe(probe.oracle, probe.base, probe.head, probe.confirmation,
                        input_digest=probe.candidate.input_sha256)


def audit_probes(probes: Sequence[AnyProbe]) -> list[AuditedProbe]:
    """Recompute each probe and check cross-probe provenance.

    Raises ``EvidenceInconsistency`` when a stored ``evidence_kind``,
    ``exclusion_reason`` or ``confirmation_status`` differs from the recomputed
    value (a ``duplicate`` exclusion is accepted only for a repeated input), when
    base or head bytes/commits change between probes, when snapshot, image,
    harness or runner mode differ, or when oracle versions are mixed.
    """
    audited: list[AuditedProbe] = []
    seen: set[str] = set()
    sides: dict[str, set[tuple[str, str]]] = {"base": set(), "head": set()}
    images: set[tuple[str, str, str, str]] = set()
    oracles: set[tuple[str, str, str]] = set()
    for index, probe in enumerate(probes, start=1):
        if probe.probe_no != index:
            raise EvidenceInconsistency("probe numbers must be 1..n in order")
        digest = probe.candidate.input_sha256
        duplicate = digest in seen
        seen.add(digest)
        assessment = _recompute(probe)
        if duplicate and probe.exclusion_reason is ExclusionReason.DUPLICATE:
            if probe.evidence_kind is not EvidenceKind.EXCLUDED:
                raise EvidenceInconsistency(f"probe {index}: duplicate must be excluded")
        elif duplicate:
            raise EvidenceInconsistency(f"probe {index}: repeated input without exclusion")
        elif (probe.evidence_kind, probe.exclusion_reason, probe.confirmation_status) != (
                assessment.evidence_kind, assessment.exclusion_reason,
                assessment.confirmation_status):
            raise EvidenceInconsistency(f"probe {index}: stored evidence does not match")
        observations = [o for o in (probe.base, probe.head, probe.confirmation) if o is not None]
        sides["base"].add((probe.base.commit_sha, probe.base.source_sha256))
        sides["head"].add((probe.head.commit_sha, probe.head.source_sha256))
        images.update((o.snapshot_id, o.image_digest, o.harness_sha256, o.runner_mode.value)
                      for o in observations)
        oracles.add((probe.oracle.contract_id, probe.oracle.contract_sha256,
                     probe.oracle.oracle_version))
        audited.append(AuditedProbe(probe, assessment, duplicate))
    if len(sides["base"]) > 1 or len(sides["head"]) > 1:
        raise EvidenceInconsistency("base or head provenance changed between probes")
    if len(images) > 1:
        raise EvidenceInconsistency("observations used different snapshot/image/harness")
    if len(oracles) > 1:
        raise EvidenceInconsistency("probes used different oracle bindings")
    return audited


def _coverage(audited: Sequence[AuditedProbe], coverage_guard_used: bool) -> Coverage:
    observed: set[CoverageTag] = set()
    usable: set[str] = set()
    for item in audited:
        if not item.duplicate and item.assessment.evidence_kind is EvidenceKind.MATCH:
            observed.update(item.probe.candidate.coverage_tags)
            usable.add(item.probe.candidate.input_sha256)
    required = sorted(REQUIRED_COVERAGE_TAGS)
    return Coverage(required=required, observed=sorted(observed),
                    missing=sorted(set(required) - observed),
                    distinct_usable_pairs=len(usable), coverage_guard_used=coverage_guard_used)


def compute_coverage(probes: Sequence[AnyProbe], coverage_guard_used: bool) -> Coverage:
    """Required tags observed only on exact base = head = expected pairs, by input hash.

    Evidence is recomputed per probe; stored labels are not trusted here.
    """
    seen: set[str] = set()
    audited: list[AuditedProbe] = []
    for probe in probes:
        digest = probe.candidate.input_sha256
        audited.append(AuditedProbe(probe, _recompute(probe), digest in seen))
        seen.add(digest)
    return _coverage(audited, coverage_guard_used)


def _result(verdict: Verdict, reason: ReasonCode, probe_no: int | None = None,
            next_requirement: str | None = None) -> VerdictResult:
    return VerdictResult(verdict=verdict, reason_code=reason, counterexample_probe_no=probe_no,
                         wording=VERDICT_WORDING[verdict], next_requirement=next_requirement)


def _inconclusive(reason: ReasonCode, next_requirement: str) -> VerdictResult:
    return _result(Verdict.INCONCLUSIVE, reason, next_requirement=next_requirement[:300])


def _t1(probe: AnyProbe) -> bool:
    return probe.oracle.status is OracleStatus.OK and probe.oracle.trust_tier is TrustTier.T1


def classify(probes: Sequence[AnyProbe], coverage: Coverage,
             stop_reason: ReasonCode) -> VerdictResult:
    """Apply ``verdict-rules-v1`` to recomputed probe evidence.

    Order: record integrity, confirmed chain (takes precedence), replay flake,
    changed pre-existing mismatch, oracle availability, trust and isolation
    failures, blocking stop reasons, clean coverage, then insufficient evidence.
    """
    try:
        audited = audit_probes(probes)
    except EvidenceInconsistency:
        return _inconclusive(ReasonCode.TRUST_FAILURE,
                             "Probe records must agree with their own evidence and provenance.")
    recomputed = _coverage(audited, coverage.coverage_guard_used)
    if recomputed != coverage:
        return _inconclusive(ReasonCode.TRUST_FAILURE,
                             "Reported coverage must match the recomputed probe evidence.")
    live = [a for a in audited if not a.duplicate]
    for item in live:
        if item.assessment.confirmation_status is not ConfirmationStatus.REPRODUCED:
            continue
        if item.assessment.evidence_kind is EvidenceKind.HEAD_MISMATCH:
            return _result(Verdict.CONFIRMED_REGRESSION, ReasonCode.CONFIRMED_EVIDENCE,
                           item.probe.probe_no)
        if item.assessment.evidence_kind is EvidenceKind.SHARED_MISMATCH:
            return _result(Verdict.CONFIRMED_EXISTING_DEFECT, ReasonCode.CONFIRMED_EVIDENCE,
                           item.probe.probe_no)
    kinds = [a.assessment.evidence_kind for a in live]
    exclusions = {a.assessment.exclusion_reason for a in live}
    if any(a.assessment.confirmation_status is ConfirmationStatus.DISAGREED for a in live):
        return _inconclusive(ReasonCode.NONDETERMINISTIC,
                             "A fresh head replay must reproduce the mismatch.")
    if EvidenceKind.BASE_MISMATCH in kinds:
        return _inconclusive(ReasonCode.PREEXISTING_MISMATCH_CHANGED,
                             "Base and head disagree with each other and the oracle.")
    if ExclusionReason.ORACLE_UNAVAILABLE in exclusions or (
            stop_reason is ReasonCode.ORACLE_UNAVAILABLE):
        return _inconclusive(ReasonCode.ORACLE_UNAVAILABLE, "A trusted T1 oracle is required.")
    if ExclusionReason.TRUST_FAILURE in exclusions:
        return _inconclusive(ReasonCode.TRUST_FAILURE,
                             "Observations must bind the pinned input, source and image.")
    if ExclusionReason.DENIED in exclusions:
        return _inconclusive(ReasonCode.ISOLATION_UNVERIFIED,
                             "The runner must execute in a verified isolated environment.")
    if stop_reason in BLOCKING_STOP_REASONS:
        return _inconclusive(stop_reason, "Resolve the blocking dependency and rerun.")
    if stop_reason not in ORDINARY_STOP_REASONS or stop_reason is ReasonCode.CONFIRMED_EVIDENCE:
        return _inconclusive(ReasonCode.TRUST_FAILURE if stop_reason is
                             ReasonCode.CONFIRMED_EVIDENCE else stop_reason,
                             "The stop reason is not supported by reproduced evidence.")
    unresolved = any(kind is not EvidenceKind.MATCH for kind in kinds)
    if (not unresolved and live and all(_t1(a.probe) for a in live) and recomputed.complete
            and recomputed.distinct_usable_pairs >= MIN_CLEAN_PAIRS):
        return _result(Verdict.NO_DEFECT_FOUND, ReasonCode.COVERAGE_COMPLETE)
    if unresolved:
        reason = (ReasonCode.NONDETERMINISTIC if EvidenceKind.HEAD_MISMATCH in kinds
                  else ReasonCode.COVERAGE_INSUFFICIENT)
        return _inconclusive(reason, "Every probe must be a usable paired match.")
    missing = ", ".join(recomputed.missing) or f"{MIN_CLEAN_PAIRS} distinct usable pairs"
    return _inconclusive(ReasonCode.COVERAGE_INSUFFICIENT, f"Missing coverage: {missing}.")
