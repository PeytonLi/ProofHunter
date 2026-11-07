"""Reference offline run over the fixture ports (wire-contract example).

``simulate_case`` exercises every frozen DTO end to end with the fixture
adapters and ``fixtures.reference``. It produces the committed sample receipts
and probe records. It is not the production pipeline (owned by integration).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fixtures import catalog, reference
from fixtures.adapters import (
    FixtureMeta,
    FixtureRoute,
    FixtureRunner,
    FixtureSource,
    SequentialIds,
    SteppingClock,
    initial_budget,
)
from proofhunter.contracts import (
    SCHEMA_VERSION,
    AttemptRecord,
    AttemptStatus,
    BinaryOutcome,
    Budget,
    BudgetUsage,
    CandidateInput,
    ConfirmationStatus,
    DecisionState,
    EvidenceKind,
    ExecutionMode,
    ExecutionRequest,
    HypothesisPlan,
    PolicyVersions,
    ProbeRecord,
    ProviderMode,
    ProviderName,
    ProviderProvenance,
    ProviderUsage,
    PrTarget,
    ReasonCode,
    ReceiptV1,
    Replay,
    ResourcePolicy,
    RouteMode,
    RunKind,
    SelectionPolicy,
    SelectionReason,
    Side,
    SourceHashes,
    build_replay_command,
    builtin_hypothesis_payload,
    enrollment_fingerprint,
    input_sha256,
    public_probe,
    public_revision,
)
from proofhunter.ports import AdapterError


@dataclass
class SimulationResult:
    receipt: ReceiptV1
    probes: list[ProbeRecord]
    attempts: list[AttemptRecord] = field(default_factory=list)


async def simulate_case_async(case_name: str, seed: int = 17, *,
                              runner: FixtureRunner | None = None,
                              meta: FixtureMeta | None = None,
                              route: FixtureRoute | None = None) -> SimulationResult:
    clock = SteppingClock()
    ids = SequentialIds()
    enrollment = catalog.demo_enrollment()
    case = catalog.PR_CASES[case_name]
    run_id = ids.new_run_id()
    created = clock.now()
    budget = initial_budget(created)
    source = FixtureSource(clock)
    meta = meta or FixtureMeta()
    route = route or FixtureRoute()
    runner = runner or FixtureRunner(enrollment, clock=clock)
    target = PrTarget(owner=catalog.DEMO_OWNER, repo=catalog.DEMO_REPO, number=case.number,
                      repo_id=enrollment.repo_id, installation_id=enrollment.installation_id)
    revision = await source.pin(target)
    bundle = await source.fetch(revision, enrollment)
    meta_mode = ProviderMode.OFFLINE_FIXTURE
    try:
        plan = await meta.propose(bundle.context)
    except AdapterError as error:
        meta_mode = ProviderMode.FALLBACK
        plan = HypothesisPlan(hypotheses=builtin_hypothesis_payload().hypotheses,
                              provenance=ProviderProvenance(
                                  provider=ProviderName.META, mode=ProviderMode.FALLBACK,
                                  model_id=None, request_id=None, latency_ms=None, attempts=1,
                                  reason_code=error.code))
    initial = reference.initialize(plan)
    bank = reference.generate(plan, seed)
    belief = initial.belief
    used: set[str] = set()
    probes: list[ProbeRecord] = []
    attempts: list[AttemptRecord] = []
    guard = False
    stop = ReasonCode.BUDGET_EXHAUSTED
    invocations = 0
    snap_calls = 0
    confirmations = 0
    attestation = await runner.attest()
    if attestation.reason_code is not None:
        stop = ReasonCode.ISOLATION_UNVERIFIED

    def request(side: Side, candidate: CandidateInput) -> ExecutionRequest:
        source_file = bundle.base if side is Side.BASE else bundle.head
        return ExecutionRequest(
            run_id=run_id, attempt_id=ids.new_attempt_id(), side=side,
            commit_sha=source_file.commit_sha, source_path="checkout.py",
            source_content=source_file.content, source_sha256=source_file.content_sha256,
            input=candidate, input_sha256=input_sha256(candidate),
            entrypoint="checkout.total_cents",
            resources=ResourcePolicy(), snapshot_id=enrollment.snapshot_id,
            image_digest=enrollment.image_digest, harness_version=enrollment.harness_version,
            harness_sha256=enrollment.harness_sha256)

    async def run(side: Side, candidate: CandidateInput, probe_no: int):  # type: ignore[no-untyped-def]
        nonlocal invocations
        invocations += 1
        req = request(side, candidate)
        obs = await runner.observe(req)
        attempts.append(AttemptRecord(
            run_id=run_id, attempt_id=req.attempt_id, side=side, input_sha256=req.input_sha256,
            probe_no=probe_no, status=AttemptStatus.COMPLETED, charged=True, lease_epoch=1,
            created_at=obs.started_at, observation=obs, reason_code=None))
        return obs

    step = 0
    while stop is not ReasonCode.ISOLATION_UNVERIFIED and step < budget.limits.max_pairs:
        eligible = bank.eligible_strategies(used)
        if not eligible:
            stop = ReasonCode.INPUTS_EXHAUSTED
            break
        step += 1
        coverage = reference.compute_coverage(probes, guard)
        state = DecisionState(
            run_id=run_id, contract_id="checkout-rounding@1", step=step,
            remaining_pairs=budget.limits.max_pairs - len(probes),
            snap_requests_remaining=budget.limits.max_snap_requests - snap_calls,
            observed_coverage=coverage.observed, missing_coverage=coverage.missing,
            unseen_candidates={s: len([c for c in bank.pool_for(s, used) if not c.is_anchor])
                               for s in eligible},
            last_outcome=None, belief=belief.probabilities)
        decision = await route.choose(state, eligible)
        if decision.mode in (RouteMode.LIVE, RouteMode.STUB):
            snap_calls += 1
        pool = bank.pool_for(decision.choice, used)
        selection = reference.select(belief, pool, SelectionPolicy(
            remaining_pairs=budget.limits.max_pairs - len(probes),
            missing_coverage=coverage.missing))
        guard = guard or selection.reason is SelectionReason.COVERAGE_GUARD
        candidate = selection.candidate
        used.add(candidate.input_sha256)
        oracle = reference.evaluate(enrollment, candidate)
        probe_no = len(probes) + 1
        base = await run(Side.BASE, candidate.as_input(), probe_no)
        head = await run(Side.HEAD, candidate.as_input(), probe_no)
        kind, exclusion = reference.classify_pair(oracle, base, head)
        confirmation = None
        if kind in (EvidenceKind.HEAD_MISMATCH, EvidenceKind.SHARED_MISMATCH) and (
                confirmations < budget.limits.max_confirmations):
            confirmations += 1
            confirmation = await run(Side.CONFIRM, candidate.as_input(), probe_no)
            confirmation = confirmation.model_copy(update={"side": Side.CONFIRM})
        status = reference.confirmation_status(head, confirmation)
        posterior = None
        if exclusion is None:
            outcome = (BinaryOutcome.MATCH if kind is EvidenceKind.MATCH
                       else BinaryOutcome.MISMATCH)
            new_belief = reference.update(belief, selection, outcome)
            posterior = new_belief.probabilities
        probes.append(ProbeRecord(
            probe_no=probe_no, candidate=candidate,
            candidate_pool_sha256=selection.candidate_pool_sha256,
            selection_reason=selection.reason, route=decision, oracle=oracle, base=base,
            head=head, confirmation=confirmation, confirmation_status=status,
            prior=belief.probabilities, posterior=posterior,
            likelihood_mismatch=selection.likelihood_mismatch,
            predictions=selection.predictions, eig_bits=selection.eig_bits,
            evidence_kind=kind, exclusion_reason=exclusion))
        if exclusion is None:
            belief = new_belief
        if status is ConfirmationStatus.REPRODUCED:
            stop = ReasonCode.CONFIRMED_EVIDENCE
            break

    coverage = reference.compute_coverage(probes, guard)
    verdict = reference.classify(probes, coverage, stop)
    replay = None
    if verdict.counterexample_probe_no is not None:
        probe = probes[verdict.counterexample_probe_no - 1]
        replay = Replay(
            probe_no=probe.probe_no, input=probe.candidate.as_input(),
            input_sha256=probe.candidate.input_sha256, base_sha=revision.base_sha,
            head_sha=revision.head_sha, base_source_sha256=bundle.base.content_sha256,
            head_source_sha256=bundle.head.content_sha256,
            harness_version=enrollment.harness_version, harness_sha256=enrollment.harness_sha256,
            contract_id=enrollment.contract_id, contract_sha256=enrollment.contract_sha256,
            oracle_version=enrollment.oracle_version, snapshot_id=enrollment.snapshot_id,
            image_digest=enrollment.image_digest, execution_mode=ExecutionMode.OFFLINE_FIXTURE,
            command=build_replay_command(ExecutionMode.OFFLINE_FIXTURE, run_id, None))
    completed = clock.now()
    final_budget = Budget(
        limits=budget.limits,
        used=BudgetUsage(pairs=len(probes), confirmations=confirmations,
                         sandbox_invocations=invocations,
                         meta_requests=0 if meta_mode is ProviderMode.OFFLINE_FIXTURE else 1,
                         snap_requests=snap_calls, candidates_generated=len(bank.candidates)),
        deadline_at=budget.deadline_at,
        wall_clock_ms=int((completed - created).total_seconds() * 1000), cost=budget.cost)
    providers = {
        ProviderName.GITHUB: ProviderUsage(mode=ProviderMode.OFFLINE_FIXTURE, model_id=None,
                                           calls=0, fallbacks=0, reason_code=None),
        ProviderName.META: ProviderUsage(mode=meta_mode, model_id=None, calls=0,
                                         fallbacks=int(meta_mode is ProviderMode.FALLBACK),
                                         reason_code=plan.provenance.reason_code),
        ProviderName.SNAP: ProviderUsage(mode=ProviderMode.STUB, model_id=None, calls=0,
                                         fallbacks=0, reason_code=None),
        ProviderName.RUNNER: ProviderUsage(mode=ProviderMode.OFFLINE_FIXTURE, model_id=None,
                                           calls=0, fallbacks=0, reason_code=None),
    }
    receipt = ReceiptV1(
        schema_version=SCHEMA_VERSION, run_id=run_id, run_kind=RunKind.VERIFICATION,
        parent_receipt_sha256=None, pinned_revision=public_revision(revision),
        source_file_sha256=SourceHashes(base=bundle.base.content_sha256,
                                        head=bundle.head.content_sha256),
        enrollment_fingerprint=enrollment_fingerprint(enrollment),
        execution_mode=ExecutionMode.OFFLINE_FIXTURE, providers=providers, budget=final_budget,
        seed=seed, policy_versions=PolicyVersions(), hypotheses=initial.hypotheses,
        probes=[public_probe(p) for p in probes], unpaired_attempt_summaries=[],
        coverage=coverage, verdict=verdict.verdict, reason_code=verdict.reason_code,
        counterexample_probe_no=verdict.counterexample_probe_no, replay=replay,
        created_at=created, completed_at=completed, receipt_sha256=None)
    return SimulationResult(receipt=reference.seal(receipt), probes=probes, attempts=attempts)


def simulate_case(case_name: str, seed: int = 17, **kwargs: object) -> SimulationResult:
    return asyncio.run(simulate_case_async(case_name, seed, **kwargs))  # type: ignore[arg-type]
