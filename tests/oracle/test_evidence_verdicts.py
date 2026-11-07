from __future__ import annotations

from typing import Any

import pytest

from fixtures import catalog, reference
from fixtures.adapters import FixtureRunner, SteppingClock
from fixtures.simulate import SimulationResult, simulate_case
from proofhunter.contracts import (
    REQUIRED_COVERAGE_TAGS,
    ConfirmationStatus,
    Coverage,
    Enrollment,
    EvidenceKind,
    ExclusionReason,
    ExecutionMode,
    ObservationOutcome,
    ProbeRecord,
    ReasonCode,
    SandboxObservation,
    Side,
    Verdict,
)
from proofhunter.oracle import (
    ProvenanceBinding,
    assess_probe,
    classify,
    classify_pair,
    compute_coverage,
    confirmation_status,
    evaluate,
)

SENTINEL = [15, 15, 15]


def counterexample(result: SimulationResult) -> ProbeRecord:
    number = result.receipt.counterexample_probe_no
    assert number is not None
    return result.probes[number - 1]


def run(probes: list[ProbeRecord], stop: ReasonCode, guard: bool = False) -> Any:
    return classify(probes, compute_coverage(probes, guard), stop)


def binding(enrollment: Enrollment, base: str = "base", head: str = "buggy"
            ) -> ProvenanceBinding:
    revisions = catalog.revisions()
    return ProvenanceBinding.for_run(
        base_sha=revisions[base].commit_sha, head_sha=revisions[head].commit_sha,
        base_source_sha256=revisions[base].source_sha256,
        head_source_sha256=revisions[head].source_sha256, enrollment=enrollment,
        execution_mode=ExecutionMode.OFFLINE_FIXTURE)


def flaky(value: int | ObservationOutcome) -> SimulationResult:
    runner = FixtureRunner(catalog.demo_enrollment(), clock=SteppingClock(),
                           faults={(Side.CONFIRM, None): value})
    return simulate_case("buggy", runner=runner)


# ---------------------------------------------------------------- 41/41/42/42


def test_buggy_chain_is_41_41_42_42(cases: dict[str, SimulationResult],
                                    enrollment: Enrollment) -> None:
    probe = counterexample(cases["buggy"])
    assert probe.candidate.prices_cents == SENTINEL
    assert probe.candidate.discount_percent == 10
    assert probe.confirmation is not None
    oracle = evaluate(enrollment, probe.candidate)
    assert (oracle.expected_cents, probe.base.value, probe.head.value,
            probe.confirmation.value) == (41, 41, 42, 42)
    assessment = assess_probe(oracle, probe.base, probe.head, probe.confirmation,
                              binding=binding(enrollment))
    assert assessment.evidence_kind is EvidenceKind.HEAD_MISMATCH
    assert assessment.exclusion_reason is None
    assert assessment.confirmation_status is ConfirmationStatus.REPRODUCED
    verdict = run(cases["buggy"].probes, ReasonCode.CONFIRMED_EVIDENCE)
    assert verdict.verdict is Verdict.CONFIRMED_REGRESSION
    assert verdict.reason_code is ReasonCode.CONFIRMED_EVIDENCE
    assert verdict.counterexample_probe_no == probe.probe_no


def test_head_mismatch_needs_confirmation(cases: dict[str, SimulationResult]) -> None:
    probe = counterexample(cases["buggy"])
    assessment = assess_probe(probe.oracle, probe.base, probe.head, None)
    assert assessment.needs_confirmation
    unconfirmed = probe.model_copy(update={"confirmation": None,
                                           "confirmation_status": ConfirmationStatus.NOT_ATTEMPTED})
    verdict = run([unconfirmed], ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.verdict is Verdict.INCONCLUSIVE
    assert verdict.counterexample_probe_no is None


def test_half_even_mutant_returns_40(cases: dict[str, SimulationResult]) -> None:
    probe = counterexample(cases["half_even"])
    assert probe.head.value != probe.oracle.expected_cents
    assert run(cases["half_even"].probes, ReasonCode.CONFIRMED_EVIDENCE).verdict is (
        Verdict.CONFIRMED_REGRESSION)


# ---------------------------------------------------------------- pre-existing defect


def test_preexisting_defect_confirmed(cases: dict[str, SimulationResult]) -> None:
    result = cases["existing"]
    probe = counterexample(result)
    assert probe.evidence_kind is EvidenceKind.SHARED_MISMATCH
    assert probe.base.value == probe.head.value == 42
    assert probe.oracle.expected_cents == 41
    assert probe.posterior is None
    verdict = run(result.probes, ReasonCode.CONFIRMED_EVIDENCE)
    assert verdict.verdict is Verdict.CONFIRMED_EXISTING_DEFECT


def test_changed_preexisting_mismatch_is_inconclusive(cases: dict[str, SimulationResult]
                                                      ) -> None:
    probe = counterexample(cases["existing"])
    head = probe.head.model_copy(update={"value": 43})
    kind, reason = classify_pair(probe.oracle, probe.base, head)
    assert (kind, reason) == (EvidenceKind.BASE_MISMATCH, ExclusionReason.BASE_MISMATCH)
    changed = probe.model_copy(update={
        "head": head, "confirmation": None, "evidence_kind": EvidenceKind.BASE_MISMATCH,
        "confirmation_status": ConfirmationStatus.NOT_ATTEMPTED})
    verdict = run([changed], ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.reason_code is ReasonCode.PREEXISTING_MISMATCH_CHANGED


# ---------------------------------------------------------------- flake


def test_flaky_confirmation_is_nondeterministic() -> None:
    result = flaky(43)
    probe = counterexample_or_mismatch(result)
    assert probe.confirmation_status is ConfirmationStatus.DISAGREED
    verdict = run(result.probes, ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.verdict is Verdict.INCONCLUSIVE
    assert verdict.reason_code is ReasonCode.NONDETERMINISTIC


def test_unusable_confirmation_never_confirms() -> None:
    result = flaky(ObservationOutcome.TIMEOUT)
    probe = counterexample_or_mismatch(result)
    assert probe.confirmation_status is ConfirmationStatus.UNUSABLE
    assert run(result.probes, ReasonCode.BUDGET_EXHAUSTED).verdict is Verdict.INCONCLUSIVE


def counterexample_or_mismatch(result: SimulationResult) -> ProbeRecord:
    return next(p for p in result.probes if p.evidence_kind is EvidenceKind.HEAD_MISMATCH)


# ---------------------------------------------------------------- oracle unavailable


def test_oracle_unavailable_blocks_verdict(cases: dict[str, SimulationResult],
                                           enrollment: Enrollment) -> None:
    disabled = enrollment.model_copy(update={"oracle_version": "unreviewed-oracle@0"})
    probes = []
    for probe in cases["buggy"].probes:
        oracle = evaluate(disabled, probe.candidate)
        assessment = assess_probe(oracle, probe.base, probe.head, probe.confirmation)
        assert assessment.exclusion_reason is ExclusionReason.ORACLE_UNAVAILABLE
        assert assessment.outcome is None
        probes.append(probe.model_copy(update={
            "oracle": oracle, "evidence_kind": assessment.evidence_kind,
            "exclusion_reason": assessment.exclusion_reason,
            "confirmation_status": assessment.confirmation_status, "posterior": None}))
    verdict = run(probes, ReasonCode.CONFIRMED_EVIDENCE)
    assert verdict.verdict is Verdict.INCONCLUSIVE
    assert verdict.reason_code is ReasonCode.ORACLE_UNAVAILABLE


def test_no_oracle_before_execution_is_inconclusive() -> None:
    verdict = run([], ReasonCode.ORACLE_UNAVAILABLE)
    assert (verdict.verdict, verdict.reason_code) == (Verdict.INCONCLUSIVE,
                                                      ReasonCode.ORACLE_UNAVAILABLE)


# ---------------------------------------------------------------- clean coverage


def test_fixed_has_clean_coverage(cases: dict[str, SimulationResult]) -> None:
    result = cases["fixed"]
    coverage = compute_coverage(result.probes, result.receipt.coverage.coverage_guard_used)
    assert coverage == result.receipt.coverage
    assert coverage.complete
    assert coverage.distinct_usable_pairs >= 3
    verdict = classify(result.probes, coverage, ReasonCode.BUDGET_EXHAUSTED)
    assert (verdict.verdict, verdict.reason_code) == (Verdict.NO_DEFECT_FOUND,
                                                      ReasonCode.COVERAGE_COMPLETE)


def test_partial_coverage_is_insufficient(cases: dict[str, SimulationResult]) -> None:
    probes = cases["fixed"].probes[:2]
    verdict = run(probes, ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.verdict is Verdict.INCONCLUSIVE
    assert verdict.reason_code is ReasonCode.COVERAGE_INSUFFICIENT
    assert verdict.next_requirement is not None


def test_claimed_coverage_must_match_evidence(cases: dict[str, SimulationResult]) -> None:
    probes = cases["fixed"].probes[:2]
    claimed = Coverage(required=sorted(REQUIRED_COVERAGE_TAGS),
                       observed=sorted(REQUIRED_COVERAGE_TAGS), missing=[],
                       distinct_usable_pairs=6, coverage_guard_used=False)
    verdict = classify(probes, claimed, ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.reason_code is ReasonCode.TRUST_FAILURE


@pytest.mark.parametrize("stop", [ReasonCode.ISOLATION_UNVERIFIED, ReasonCode.DEADLINE_EXCEEDED,
                                  ReasonCode.PROVIDER_UNAVAILABLE, ReasonCode.INTERNAL_ERROR])
def test_blocking_stop_reason_prevents_clean_verdict(cases: dict[str, SimulationResult],
                                                     stop: ReasonCode) -> None:
    verdict = run(cases["fixed"].probes, stop, cases["fixed"].receipt.coverage.coverage_guard_used)
    assert (verdict.verdict, verdict.reason_code) == (Verdict.INCONCLUSIVE, stop)


def test_unusable_probe_blocks_clean_verdict() -> None:
    runner = FixtureRunner(catalog.demo_enrollment(), clock=SteppingClock(),
                           faults={(Side.HEAD, None): ObservationOutcome.TIMEOUT})
    result = simulate_case("fixed", runner=runner)
    assert all(p.exclusion_reason is ExclusionReason.TIMEOUT for p in result.probes)
    verdict = run(result.probes, ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.verdict is Verdict.INCONCLUSIVE


# ---------------------------------------------------------------- hostile output


def forge(obs: SandboxObservation, **changes: Any) -> SandboxObservation:
    return SandboxObservation.model_construct(**{**dict(obs), **changes})


@pytest.mark.parametrize("changes", [
    {"value": True}, {"value": 42.0}, {"value": "41"}, {"value": 2**64},
    {"value": None}, {"truncated": True},
])
def test_malformed_head_envelope_is_unusable(cases: dict[str, SimulationResult],
                                             changes: dict[str, Any]) -> None:
    probe = counterexample(cases["buggy"])
    kind, reason = classify_pair(probe.oracle, probe.base, forge(probe.head, **changes))
    assert (kind, reason) == (EvidenceKind.EXCLUDED, ExclusionReason.INVALID_OUTPUT)


def test_bool_is_never_equal_to_expected(enrollment: Enrollment,
                                         cases: dict[str, SimulationResult]) -> None:
    probe = next(p for p in cases["fixed"].probes
                 if p.oracle.expected_cents == 1 and p.base.value == 1)
    kind, _ = classify_pair(probe.oracle, probe.base, forge(probe.head, value=True))
    assert kind is EvidenceKind.EXCLUDED


@pytest.mark.parametrize("field", ["source_sha256", "commit_sha", "harness_sha256",
                                   "input_sha256", "image_digest", "snapshot_id"])
def test_spoofed_provenance_is_trust_failure(cases: dict[str, SimulationResult],
                                             enrollment: Enrollment, field: str) -> None:
    probe = counterexample(cases["buggy"])
    spoof = {"image_digest": "sha256:" + "e" * 64, "snapshot_id": "attacker-snapshot",
             "commit_sha": "e" * 40}.get(field, "e" * 64)
    head = probe.head.model_copy(update={field: spoof})
    kind, reason = classify_pair(probe.oracle, probe.base, head, binding=binding(enrollment))
    assert (kind, reason) == (EvidenceKind.EXCLUDED, ExclusionReason.TRUST_FAILURE)
    status = confirmation_status(probe.head, probe.confirmation.model_copy(  # type: ignore[union-attr]
        update={field: spoof}), binding=binding(enrollment))
    assert status is ConfirmationStatus.UNUSABLE


def test_head_output_spoofing_base_side_is_rejected(cases: dict[str, SimulationResult]) -> None:
    probe = counterexample(cases["buggy"])
    kind, reason = classify_pair(probe.oracle, probe.base,
                                 probe.head.model_copy(update={"side": Side.BASE}))
    assert reason is ExclusionReason.TRUST_FAILURE
    assert kind is EvidenceKind.EXCLUDED


def test_reused_attempt_cannot_confirm(cases: dict[str, SimulationResult]) -> None:
    probe = counterexample(cases["buggy"])
    replayed = probe.head.model_copy(update={"side": Side.CONFIRM})
    assert confirmation_status(probe.head, replayed) is ConfirmationStatus.UNUSABLE


def test_forged_evidence_label_is_trust_failure(cases: dict[str, SimulationResult]) -> None:
    probe = counterexample(cases["buggy"])
    forged = probe.model_copy(update={"evidence_kind": EvidenceKind.MATCH,
                                      "confirmation": None,
                                      "confirmation_status": ConfirmationStatus.NOT_ATTEMPTED})
    probes = [*cases["buggy"].probes[: probe.probe_no - 1], forged]
    verdict = classify(probes, reference.compute_coverage(probes, False),
                       ReasonCode.BUDGET_EXHAUSTED)
    assert verdict.reason_code is ReasonCode.TRUST_FAILURE
    assert verdict.verdict is Verdict.INCONCLUSIVE


def test_mixed_head_sources_are_trust_failure(cases: dict[str, SimulationResult]) -> None:
    probes = list(cases["fixed"].probes)
    other = catalog.revisions()["buggy"]
    probes[1] = probes[1].model_copy(update={"head": probes[1].head.model_copy(
        update={"source_sha256": other.source_sha256})})
    assert run(probes, ReasonCode.BUDGET_EXHAUSTED).reason_code is ReasonCode.TRUST_FAILURE


def test_denied_execution_maps_to_isolation() -> None:
    runner = FixtureRunner(catalog.demo_enrollment(), clock=SteppingClock(),
                           faults={(Side.BASE, None): ObservationOutcome.DENIED})
    result = simulate_case("buggy", runner=runner)
    assert run(result.probes, ReasonCode.BUDGET_EXHAUSTED).reason_code is (
        ReasonCode.ISOLATION_UNVERIFIED)


# ---------------------------------------------------------------- reference cross-check


@pytest.mark.parametrize(("case", "faults"), [
    ("buggy", {}), ("fixed", {}), ("half_even", {}), ("existing", {}),
    ("buggy", {(Side.CONFIRM, None): 43}),
    ("buggy", {(Side.CONFIRM, None): ObservationOutcome.INVALID}),
    ("fixed", {(Side.HEAD, None): ObservationOutcome.NONZERO}),
    ("fixed", {(Side.BASE, None): ObservationOutcome.TIMEOUT}),
])
def test_agrees_with_reference_rules(case: str, faults: dict[Any, Any]) -> None:
    runner = FixtureRunner(catalog.demo_enrollment(), clock=SteppingClock(), faults=faults)
    result = simulate_case(case, runner=runner)
    guard = result.receipt.coverage.coverage_guard_used
    stop = (ReasonCode.CONFIRMED_EVIDENCE if result.receipt.counterexample_probe_no
            else ReasonCode.BUDGET_EXHAUSTED)
    mine = classify(result.probes, compute_coverage(result.probes, guard), stop)
    theirs = reference.classify(result.probes, reference.compute_coverage(result.probes, guard),
                                stop)
    assert (mine.verdict, mine.reason_code, mine.counterexample_probe_no) == (
        theirs.verdict, theirs.reason_code, theirs.counterexample_probe_no)
    for probe in result.probes:
        assessment = assess_probe(probe.oracle, probe.base, probe.head, probe.confirmation)
        assert (assessment.evidence_kind, assessment.exclusion_reason,
                assessment.confirmation_status) == (probe.evidence_kind, probe.exclusion_reason,
                                                    probe.confirmation_status)
