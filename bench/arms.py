"""Label-free arm boundary. Scoring metadata is deliberately not accepted here."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from time import perf_counter_ns

from fixtures.adapters import FixtureRoute, SequentialIds, initial_budget
from proofhunter import bayes, oracle
from proofhunter.contracts import (
    CandidateBank,
    CandidateInput,
    ConfirmationStatus,
    DecisionState,
    Enrollment,
    ExecutionMode,
    ExecutionRequest,
    HypothesisPlan,
    PolicyVersions,
    ProbeRecord,
    ReasonCode,
    ResourcePolicy,
    RouteDecision,
    RouteMode,
    RouteReasonCode,
    SandboxObservation,
    SelectionPolicy,
    SelectionReason,
    Side,
    SourceFile,
    Strategy,
    build_replay_command,
    input_sha256,
)
from proofhunter.ports import RunnerPort

ARMS = ("fixed_boundary", "random", "eig_deterministic", "meta_snap_eig")
FIXED_ORDER = (Strategy.BOUNDARY, Strategy.CONSTRAINT, Strategy.SEEDED_FUZZ)


@dataclass(frozen=True)
class ArmInput:
    base: SourceFile
    head: SourceFile
    enrollment: Enrollment
    plan: HypothesisPlan
    bank: CandidateBank


async def run_arm(inputs: ArmInput, arm: str, seed: int, runner: RunnerPort) -> dict[str, object]:
    if arm not in ARMS:
        raise ValueError("unknown arm")
    start = perf_counter_ns()
    rng = random.Random(seed)  # noqa: S311
    ids = SequentialIds()
    run_id = ids.new_run_id()
    attestation = await runner.attest()
    limits = initial_budget(attestation.checked_at).limits
    belief = bayes.initialize(inputs.plan).belief
    binding = oracle.ProvenanceBinding.for_run(
        base_sha=inputs.base.commit_sha, head_sha=inputs.head.commit_sha,
        base_source_sha256=inputs.base.content_sha256,
        head_source_sha256=inputs.head.content_sha256, enrollment=inputs.enrollment,
        execution_mode=ExecutionMode.OFFLINE_FIXTURE)
    route = FixtureRoute()
    used: set[str] = set()
    probes: list[ProbeRecord] = []
    guard = False
    stop = ReasonCode.BUDGET_EXHAUSTED
    counts: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    if attestation.reason_code is not None:
        stop = ReasonCode.ISOLATION_UNVERIFIED

    async def observe(side: Side, candidate_input: CandidateInput) -> SandboxObservation:
        counts[side.value] += 1
        source = inputs.base if side is Side.BASE else inputs.head
        e = inputs.enrollment
        request = ExecutionRequest(
            run_id=run_id, attempt_id=ids.new_attempt_id(), side=side,
            commit_sha=source.commit_sha, source_path="checkout.py",
            source_content=source.content, source_sha256=source.content_sha256,
            input=candidate_input, input_sha256=input_sha256(candidate_input),
            entrypoint=e.entrypoint, resources=ResourcePolicy(), snapshot_id=e.snapshot_id,
            image_digest=e.image_digest, harness_version=e.harness_version,
            harness_sha256=e.harness_sha256)
        result = await runner.observe(request)
        if not result.usable:
            failures[result.outcome.value] += 1
        return result

    while stop is not ReasonCode.ISOLATION_UNVERIFIED and len(probes) < limits.max_pairs:
        pool = [c for c in inputs.bank.candidates if c.input_sha256 not in used]
        if not pool:
            stop = ReasonCode.INPUTS_EXHAUSTED
            break
        coverage = oracle.compute_coverage(probes, guard)
        decision = None
        if arm == "meta_snap_eig":
            eligible = inputs.bank.eligible_strategies(used)
            if not eligible:
                stop = ReasonCode.INPUTS_EXHAUSTED
                break
            state = DecisionState(
                run_id=run_id, contract_id=inputs.enrollment.contract_id, step=len(probes) + 1,
                remaining_pairs=limits.max_pairs - len(probes),
                snap_requests_remaining=limits.max_snap_requests - modes["stub"],
                observed_coverage=coverage.observed, missing_coverage=coverage.missing,
                unseen_candidates={s: sum(not c.is_anchor for c in inputs.bank.pool_for(s, used))
                                   for s in eligible}, last_outcome=None,
                belief=belief.probabilities)
            decision = await route.choose(state, eligible)
            modes[decision.mode.value] += 1
            pool = inputs.bank.pool_for(decision.choice, used)
        elif arm == "fixed_boundary":
            pool = [min(pool, key=lambda c: (not c.is_anchor,
                                             FIXED_ORDER.index(c.strategy), c.input_sha256))]
        elif arm == "random":
            pool = [rng.choice(pool)]
        selection = bayes.select(belief, pool, SelectionPolicy(
            remaining_pairs=limits.max_pairs - len(probes), missing_coverage=coverage.missing))
        guard = guard or selection.reason is SelectionReason.COVERAGE_GUARD
        candidate = selection.candidate
        if decision is None:
            decision = RouteDecision(
                choice=candidate.strategy, eligible=[candidate.strategy], mode=RouteMode.POLICY,
                model_id=None, probabilities=None, confidence=None, provider_request_id=None,
                latency_ms=None, reason_code=RouteReasonCode.SINGLE_ELIGIBLE)
        used.add(candidate.input_sha256)
        expected = oracle.evaluate(inputs.enrollment, candidate)
        if oracle.trusted_expected(expected) is None:
            stop = ReasonCode.ORACLE_UNAVAILABLE
            break
        base = await observe(Side.BASE, candidate.as_input())
        head = await observe(Side.HEAD, candidate.as_input())
        assessment = oracle.assess_probe(expected, base, head,
                                        input_digest=candidate.input_sha256, binding=binding)
        confirmation = None
        if assessment.needs_confirmation and (
                counts["confirm"] < limits.max_confirmations):
            confirmation = await observe(Side.CONFIRM, candidate.as_input())
        assessment = oracle.assess_probe(expected, base, head, confirmation,
                                        input_digest=candidate.input_sha256, binding=binding)
        kind, exclusion = assessment.evidence_kind, assessment.exclusion_reason
        status = assessment.confirmation_status
        posterior = None
        updated = belief
        if assessment.outcome is not None:
            updated = bayes.update(belief, selection, assessment.outcome)
            posterior = updated.probabilities
        probes.append(ProbeRecord(
            probe_no=len(probes) + 1, candidate=candidate,
            candidate_pool_sha256=selection.candidate_pool_sha256,
            selection_reason=selection.reason, route=decision, oracle=expected,
            base=base, head=head,
            confirmation=confirmation, confirmation_status=status, prior=belief.probabilities,
            posterior=posterior, likelihood_mismatch=selection.likelihood_mismatch,
            predictions=selection.predictions, eig_bits=selection.eig_bits,
            evidence_kind=kind, exclusion_reason=exclusion))
        if arm in ("eig_deterministic", "meta_snap_eig"):
            belief = updated
        if status is ConfirmationStatus.REPRODUCED:
            stop = ReasonCode.CONFIRMED_EVIDENCE
            break
    coverage = oracle.compute_coverage(probes, guard)
    verdict = oracle.classify(probes, coverage, stop)
    first = verdict.counterexample_probe_no
    unusable = Counter(p.exclusion_reason.value for p in probes if p.exclusion_reason is not None)
    return {
        "arm": arm, "seed": seed, "bank_seed": inputs.bank.seed,
        "bank_sha256": inputs.bank.bank_sha256, "policy_versions": PolicyVersions().model_dump(),
        "bench_policy": "bench-v2-production-anchor-boundary-constraint-fuzz-hash",
        "budget": {"max_pairs": limits.max_pairs, "max_confirmations": limits.max_confirmations},
        "runner_mode": "offline_fixture", "provider_mode": "recorded/offline_fixture",
        "attempted_mode": "stub" if arm == "meta_snap_eig" else "none",
        "provider_usage": dict(modes),
        "fallback_rate": modes["fallback"] / max(1, sum(modes.values())),
        "base_invocations": counts["base"], "head_invocations": counts["head"],
        "confirm_invocations": counts["confirm"],
        "pairs_to_first_confirmed_witness": first, "censored": first is None,
        "verdict": verdict.verdict.value, "reason_code": verdict.reason_code.value,
        "coverage": coverage.model_dump(mode="json"),
        "unusable_by_reason": dict(unusable), "failures_by_reason": dict(failures),
        "wall_ms": (perf_counter_ns() - start) / 1_000_000,
        "replay_command": build_replay_command(ExecutionMode.OFFLINE_FIXTURE, run_id, first)
        if first is not None else None,
        "probes": [p.model_dump(mode="json") for p in probes],
    }
