"""Lease-fenced verification using trusted production inference and oracle code."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from proofhunter import bayes, oracle
from proofhunter import contracts as c
from proofhunter.config import Settings, load_enrollments
from proofhunter.ports import (
    AdapterError,
    CheckPort,
    Clock,
    MetaPort,
    RoutePort,
    RunnerPort,
    RunRecord,
    RunStore,
    SourcePort,
)
from proofhunter.runner import create_runner_port

T = TypeVar("T")


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class VerificationPipeline:
    settings: Settings
    enrollment: c.Enrollment
    source: SourcePort
    meta: MetaPort
    route: RoutePort
    runner: RunnerPort
    checks: CheckPort
    store: RunStore
    clock: Clock
    fixture_bank: Path | None = None
    meta_request_allowance: int = 2
    route_requests_per_call: int = 2
    route_request_limit: int | None = None
    expected_pins: tuple[str, str] | None = None

    async def run(self, record: RunRecord, lease: c.RunLease) -> c.ReceiptV1:
        if not self.settings.offline:
            from proofhunter.github import create_repository_validator

            target, enrollment = create_repository_validator(
                self.settings, load_enrollments(self.settings.enrollments_path)
            ).resolve(record.target.canonical_url)
            if target != record.target:
                raise ValueError("enrollment identity mismatch")
            self.enrollment = enrollment
        mode = c.ExecutionMode.OFFLINE_FIXTURE if self.settings.offline else c.ExecutionMode.LIVE
        budget = record.status.budget
        usage = budget.used.model_dump()
        probes = list(record.probes)
        state = record.status.status
        seq = max((event.seq for event in record.events), default=0)
        checkpoint = record.checkpoint
        revision = checkpoint.pinned_revision if checkpoint else None
        sources = None
        initial = None
        replay = None
        snap_counted = 0
        recovery_blocked = False
        guard = any(probe.selection_reason is c.SelectionReason.COVERAGE_GUARD for probe in probes)
        stop = c.ReasonCode.BUDGET_EXHAUSTED
        provider_mode = (
            c.ProviderMode.OFFLINE_FIXTURE if self.settings.offline else c.ProviderMode.LIVE
        )
        providers = {
            name: c.ProviderUsage(
                mode=provider_mode, model_id=None, calls=0, fallbacks=0, reason_code=None
            )
            for name in c.ProviderName
        }

        async def external(awaitable: Awaitable[T]) -> T:
            remaining = max(0.0, (budget.deadline_at - self.clock.now()).total_seconds())
            try:
                return await asyncio.wait_for(awaitable, timeout=remaining)
            except TimeoutError as error:
                raise AdapterError(
                    c.ProviderName.RUNNER,
                    c.ReasonCode.BUDGET_EXHAUSTED,
                    "run deadline exhausted",
                    retryable=False,
                ) from error

        def stage(next_state: c.RunState) -> None:
            nonlocal state, seq
            seq += 1
            self.store.checkpoint(
                lease,
                c.RunEvent(
                    run_id=record.run_id,
                    seq=seq,
                    at=self.clock.now(),
                    kind=c.RunEventKind.STATE_CHANGED,
                    from_state=state,
                    to_state=next_state,
                    reason_code=None,
                    detail=None,
                ),
            )
            state = next_state

        def available(invocations: int) -> bool:
            return (
                usage["sandbox_invocations"] + invocations <= budget.limits.max_sandbox_invocations
                and self.clock.now() < budget.deadline_at
            )

        def reserve(meta: int, snap: int) -> None:
            nonlocal checkpoint
            checkpoint = self.store.reserve_progress(
                lease,
                c.RunCheckpoint(
                    pinned_revision=revision,
                    provider_allocations=c.ProviderAllocations(meta=meta, snap=snap),
                ),
            )
            usage["meta_requests"] = checkpoint.provider_allocations.meta
            usage["snap_requests"] = checkpoint.provider_allocations.snap

        try:
            if state is c.RunState.QUEUED:
                stage(c.RunState.FETCHING)
            if checkpoint is None:
                usage["meta_requests"] = budget.limits.max_meta_requests
                usage["snap_requests"] = budget.limits.max_snap_requests
            else:
                usage["meta_requests"] = checkpoint.provider_allocations.meta
                usage["snap_requests"] = checkpoint.provider_allocations.snap
            if checkpoint is None or (
                revision is not None
                and (usage["meta_requests"] or usage["snap_requests"] or probes or record.attempts)
            ):
                recovery_blocked = True
                if probes:
                    sources = c.SourceHashes(
                        base=probes[0].base.source_sha256, head=probes[0].head.source_sha256
                    )
                raise AdapterError(
                    c.ProviderName.GITHUB,
                    c.ReasonCode.REVISION_UNAVAILABLE,
                    "durable recovery pin unavailable; operator review required",
                    retryable=False,
                )
            if revision is None:
                revision = await external(self.source.pin(record.target))
                if record.replay is not None:
                    revision = revision.model_copy(
                        update={
                            "base_sha": record.replay.base_sha,
                            "head_sha": record.replay.head_sha,
                        }
                    )
                reserve(usage["meta_requests"], usage["snap_requests"])
            if self.expected_pins is not None and (
                revision.base_sha, revision.head_sha
            ) != self.expected_pins:
                raise AdapterError(
                    c.ProviderName.GITHUB, c.ReasonCode.REVISION_UNAVAILABLE,
                    "expected pins differ", retryable=False,
                )
            bundle = await external(self.source.fetch(revision, self.enrollment))
            sources = c.SourceHashes(
                base=bundle.base.content_sha256, head=bundle.head.content_sha256
            )
            if record.replay is not None and (
                sources.base != record.replay.base_source_sha256
                or sources.head != record.replay.head_source_sha256
                or mode != record.replay.execution_mode
            ):
                raise AdapterError(
                    c.ProviderName.RUNNER,
                    c.ReasonCode.TRUST_FAILURE,
                    "replay binding mismatch",
                    retryable=False,
                )
            stage(c.RunState.PLANNING)
            plan = c.HypothesisPlan(
                hypotheses=c.builtin_hypothesis_payload().hypotheses,
                provenance=c.ProviderProvenance(
                    provider=c.ProviderName.META,
                    mode=c.ProviderMode.FALLBACK,
                    model_id=None,
                    request_id=None,
                    latency_ms=None,
                    attempts=0,
                    reason_code=c.ReasonCode.BUDGET_EXHAUSTED,
                ),
            )
            if (
                record.replay is None
                and budget.limits.max_meta_requests - usage["meta_requests"]
                >= self.meta_request_allowance
                and available(0)
            ):
                if self.meta_request_allowance:
                    reserve(
                        usage["meta_requests"] + self.meta_request_allowance, usage["snap_requests"]
                    )
                try:
                    plan = await external(self.meta.propose(bundle.context))
                except AdapterError as error:
                    plan = plan.model_copy(
                        update={
                            "provenance": plan.provenance.model_copy(
                                update={
                                    "reason_code": error.code,
                                    "attempts": getattr(error, "attempts", 0),
                                }
                            )
                        }
                    )
            providers[c.ProviderName.META] = c.ProviderUsage(
                mode=plan.provenance.mode,
                model_id=plan.provenance.model_id,
                calls=plan.provenance.attempts,
                fallbacks=int(plan.provenance.mode is c.ProviderMode.FALLBACK),
                reason_code=plan.provenance.reason_code,
            )
            initial = bayes.initialize(plan)
            bank = bayes.generate(plan, record.seed)
            if self.fixture_bank is not None and record.seed == 17:
                bank = c.CandidateBank.model_validate_json(self.fixture_bank.read_bytes())
            if len(bank.candidates) > budget.limits.max_candidates:
                candidates = bank.candidates[: budget.limits.max_candidates]
                bank = c.CandidateBank(
                    generator_version=bank.generator_version,
                    seed=bank.seed,
                    candidates=candidates,
                    bank_sha256=c.pool_sha256(candidates),
                )
            usage["candidates_generated"] = len(bank.candidates)
            belief = initial.belief
            binding = oracle.ProvenanceBinding.for_run(
                base_sha=revision.base_sha,
                head_sha=revision.head_sha,
                base_source_sha256=sources.base,
                head_source_sha256=sources.head,
                enrollment=self.enrollment,
                execution_mode=mode,
            )
            if oracle.evaluate(self.enrollment, bank.candidates[0]).status is not c.OracleStatus.OK:
                raise AdapterError(
                    c.ProviderName.RUNNER,
                    c.ReasonCode.ORACLE_UNAVAILABLE,
                    "oracle unavailable",
                    retryable=False,
                )
            attestation = await external(self.runner.attest())
            isolation_failed = (
                attestation.reason_code is not None
                or attestation.status
                not in (c.IsolationStatus.VERIFIED, c.IsolationStatus.OFFLINE_FIXTURE)
                or attestation.snapshot_id != self.enrollment.snapshot_id
                or attestation.image_digest != self.enrollment.image_digest
                or attestation.harness_sha256 != self.enrollment.harness_sha256
            )
            disabled_live_runner = not self.settings.offline and not (
                self.settings.daytona.enable_live_runner
            )
            if isolation_failed and not disabled_live_runner:
                raise AdapterError(
                    c.ProviderName.RUNNER,
                    c.ReasonCode.ISOLATION_UNVERIFIED,
                    "isolation unverified",
                    retryable=False,
                )
            if not isolation_failed:
                stage(c.RunState.TESTING)

            async def observe(
                side: c.Side, candidate: c.Candidate, number: int
            ) -> c.SandboxObservation:
                if not available(1):
                    raise AdapterError(
                        c.ProviderName.RUNNER,
                        c.ReasonCode.BUDGET_EXHAUSTED,
                        "execution budget exhausted",
                        retryable=False,
                    )
                source = bundle.base if side is c.Side.BASE else bundle.head
                request = c.ExecutionRequest(
                    run_id=record.run_id,
                    attempt_id=str(uuid4()),
                    side=side,
                    commit_sha=source.commit_sha,
                    source_path=self.enrollment.source_path,
                    source_content=source.content,
                    source_sha256=source.content_sha256,
                    input=candidate.as_input(),
                    input_sha256=candidate.input_sha256,
                    entrypoint=self.enrollment.entrypoint,
                    resources=c.ResourcePolicy(),
                    snapshot_id=self.enrollment.snapshot_id,
                    image_digest=self.enrollment.image_digest,
                    harness_version=self.enrollment.harness_version,
                    harness_sha256=self.enrollment.harness_sha256,
                )
                attempt = c.AttemptRecord(
                    run_id=record.run_id,
                    attempt_id=request.attempt_id,
                    side=side,
                    input_sha256=candidate.input_sha256,
                    probe_no=number,
                    status=c.AttemptStatus.RUNNING,
                    charged=True,
                    lease_epoch=lease.epoch,
                    created_at=self.clock.now(),
                    observation=None,
                    reason_code=None,
                )
                self.store.record_attempt(lease, attempt)
                usage["sandbox_invocations"] += 1
                result = await external(self.runner.observe(request))
                self.store.record_attempt(
                    lease,
                    attempt.model_copy(
                        update={"status": c.AttemptStatus.COMPLETED, "observation": result}
                    ),
                )
                return result

            used = {p.candidate.input_sha256 for p in probes}
            while len(probes) < budget.limits.max_pairs and available(
                2 + int(usage["confirmations"] < budget.limits.max_confirmations)
            ):
                eligible = bank.eligible_strategies(used)
                if not eligible:
                    stop = c.ReasonCode.INPUTS_EXHAUSTED
                    break
                coverage = oracle.compute_coverage(probes, guard)
                counter = getattr(self.route, "requests_spent", None)
                spent = counter(record.run_id) if counter else snap_counted
                if record.replay is None:
                    want = min(
                        min(budget.limits.max_snap_requests, self.route_request_limit) - spent
                        if self.route_request_limit is not None
                        else budget.limits.max_snap_requests - spent,
                        self.route_requests_per_call,
                    )
                    if usage["snap_requests"] - spent < want:
                        reserve(usage["meta_requests"], spent + want)
                decision = (
                    await external(
                        self.route.choose(
                            c.DecisionState(
                                run_id=record.run_id,
                                contract_id=self.enrollment.contract_id,
                                step=len(probes) + 1,
                                remaining_pairs=budget.limits.max_pairs - len(probes),
                                snap_requests_remaining=usage["snap_requests"] - spent,
                                observed_coverage=coverage.observed,
                                missing_coverage=coverage.missing,
                                unseen_candidates={
                                    s: sum(not x.is_anchor for x in bank.pool_for(s, used))
                                    for s in eligible
                                },
                                last_outcome=None,
                                belief=belief.probabilities,
                            ),
                            eligible,
                        )
                    )
                    if record.replay is None
                    else c.RouteDecision(
                        choice=eligible[0],
                        eligible=list(eligible),
                        mode=c.RouteMode.POLICY,
                        reason_code=c.RouteReasonCode.CALL_BUDGET,
                        probabilities=None,
                        model_id=None,
                        provider_request_id=None,
                        latency_ms=None,
                        confidence=None,
                    )
                )
                if record.replay is None:
                    snap_counted = (
                        counter(record.run_id)
                        if counter
                        else snap_counted
                        + int(decision.mode in (c.RouteMode.LIVE, c.RouteMode.STUB))
                    )
                previous = providers[c.ProviderName.SNAP]
                providers[c.ProviderName.SNAP] = c.ProviderUsage(
                    mode=c.ProviderMode(decision.mode.value)
                    if decision.mode is not c.RouteMode.POLICY
                    else previous.mode,
                    model_id=decision.model_id,
                    calls=snap_counted if counter else 0,
                    fallbacks=previous.fallbacks + int(decision.mode is c.RouteMode.FALLBACK),
                    reason_code=None,
                )
                if isolation_failed:
                    raise AdapterError(
                        c.ProviderName.RUNNER, c.ReasonCode.ISOLATION_UNVERIFIED,
                        "isolation unverified", retryable=False,
                    )
                pool = bank.pool_for(decision.choice, used)
                if record.replay is not None:
                    pool = [
                        bayes.make_candidate(
                            record.replay.input.prices_cents,
                            record.replay.input.discount_percent,
                            decision.choice,
                        )
                    ]
                selection = bayes.select(
                    belief,
                    pool,
                    c.SelectionPolicy(
                        remaining_pairs=budget.limits.max_pairs - len(probes),
                        missing_coverage=coverage.missing,
                    ),
                )
                guard |= selection.reason is c.SelectionReason.COVERAGE_GUARD
                candidate = selection.candidate
                used.add(candidate.input_sha256)
                expected = oracle.evaluate(self.enrollment, candidate)
                if expected.status is not c.OracleStatus.OK:
                    stop = c.ReasonCode.ORACLE_UNAVAILABLE
                    break
                number = len(probes) + 1
                base = await observe(c.Side.BASE, candidate, number)
                head = await observe(c.Side.HEAD, candidate, number)
                assessment = oracle.assess_probe(expected, base, head, binding=binding)
                confirmation = None
                if (
                    assessment.needs_confirmation
                    and usage["confirmations"] < budget.limits.max_confirmations
                    and available(1)
                ):
                    usage["confirmations"] += 1
                    confirmation = await observe(c.Side.CONFIRM, candidate, number)
                    assessment = oracle.assess_probe(
                        expected, base, head, confirmation, binding=binding
                    )
                posterior = (
                    bayes.update(belief, selection, assessment.outcome)
                    if assessment.outcome is not None
                    else None
                )
                probe = c.ProbeRecord(
                    probe_no=number,
                    candidate=candidate,
                    candidate_pool_sha256=selection.candidate_pool_sha256,
                    selection_reason=selection.reason,
                    route=decision,
                    oracle=expected,
                    base=base,
                    head=head,
                    confirmation=confirmation,
                    confirmation_status=assessment.confirmation_status,
                    prior=belief.probabilities,
                    posterior=posterior.probabilities if posterior else None,
                    likelihood_mismatch=selection.likelihood_mismatch,
                    predictions=selection.predictions,
                    eig_bits=selection.eig_bits,
                    evidence_kind=assessment.evidence_kind,
                    exclusion_reason=assessment.exclusion_reason,
                )
                self.store.commit_probe(lease, probe)
                probes.append(probe)
                usage["pairs"] = len(probes)
                if posterior is not None:
                    belief = posterior
                if assessment.confirmation_status is c.ConfirmationStatus.REPRODUCED:
                    stop = c.ReasonCode.CONFIRMED_EVIDENCE
                    break
                if record.replay is not None:
                    break
            stage(c.RunState.FINALIZING)
        except AdapterError as error:
            stop = error.code
        coverage = oracle.compute_coverage(probes, guard)
        verdict = oracle.classify(probes, coverage, stop)
        if recovery_blocked:
            verdict = c.VerdictResult(
                verdict=c.Verdict.INCONCLUSIVE,
                reason_code=c.ReasonCode.REVISION_UNAVAILABLE,
                counterexample_probe_no=None,
                wording=c.VERDICT_WORDING[c.Verdict.INCONCLUSIVE],
                next_requirement="Start a fresh run; partial execution cannot be safely resumed.",
            )
        if (
            verdict.counterexample_probe_no is not None
            and revision is not None
            and sources is not None
        ):
            replay = oracle.build_replay(
                run_id=record.run_id,
                probe=probes[verdict.counterexample_probe_no - 1],
                revision=revision,
                sources=sources,
                enrollment=self.enrollment,
                execution_mode=mode,
            )
        completed = self.clock.now()
        paired = {
            observation.attempt_id
            for probe in probes
            for observation in (probe.base, probe.head, probe.confirmation)
            if observation is not None
        }
        unpaired = [
            c.UnpairedAttemptSummary(
                attempt_id=attempt.attempt_id,
                side=attempt.side,
                input_sha256=attempt.input_sha256,
                status=attempt.status,
                outcome=attempt.observation.outcome if attempt.observation else None,
                charged=attempt.charged,
                reason_code=attempt.reason_code,
            )
            for attempt in self.store.load_run(record.run_id).attempts
            if attempt.attempt_id not in paired
        ]
        receipt = oracle.seal(
            c.ReceiptV1(
                schema_version=c.SCHEMA_VERSION,
                run_id=record.run_id,
                run_kind=record.run_kind,
                parent_receipt_sha256=record.parent_receipt_sha256,
                pinned_revision=c.public_revision(revision) if revision else None,
                source_file_sha256=sources,
                enrollment_fingerprint=c.enrollment_fingerprint(self.enrollment),
                execution_mode=mode,
                providers=providers,
                budget=budget.model_copy(
                    update={
                        "used": c.BudgetUsage(**usage),
                        "wall_clock_ms": max(
                            0, int((completed - record.status.created_at).total_seconds() * 1000)
                        ),
                    }
                ),
                seed=record.seed,
                policy_versions=c.PolicyVersions(),
                hypotheses=initial.hypotheses if initial else None,
                probes=[c.public_probe(p) for p in probes],
                unpaired_attempt_summaries=unpaired,
                coverage=coverage,
                verdict=verdict.verdict,
                reason_code=verdict.reason_code,
                counterexample_probe_no=verdict.counterexample_probe_no,
                replay=replay,
                created_at=record.status.created_at,
                completed_at=completed,
                receipt_sha256=None,
            )
        )
        check = None
        if (
            mode is c.ExecutionMode.LIVE
            and self.settings.github.enable_checks
            and revision is not None
        ):
            assert receipt.receipt_sha256 is not None
            check = c.CheckIntent(
                run_id=record.run_id,
                external_id=record.run_id,
                owner=revision.owner,
                repo=revision.repo,
                repo_id=revision.repo_id,
                installation_id=revision.installation_id,
                head_sha=revision.head_sha,
                name="ProofHunter",
                status="completed",
                conclusion=c.check_conclusion_for(receipt.verdict),
                title="ProofHunter verification",
                summary=receipt.reason_code.value,
                receipt_sha256=receipt.receipt_sha256,
                execution_mode="live",
            )
        self.store.finish(lease, receipt, check)
        return receipt


def compose(
    settings: Settings, store: RunStore, *, clock: Clock | None = None
) -> VerificationPipeline:
    """The only fixture imports in production are this explicit offline binding."""
    if settings.offline:
        from fixtures.adapters import FixtureChecks, FixtureMeta, FixtureRoute, FixtureSource

        enrollment = load_enrollments(settings.enrollments_path)[0]
        return VerificationPipeline(
            settings,
            enrollment,
            FixtureSource(),
            FixtureMeta(),
            FixtureRoute(),
            create_runner_port(settings),
            FixtureChecks(),
            store,
            clock or WallClock(),
            Path(__file__).resolve().parents[2] / "fixtures/demo/candidate_bank.seed17.json",
            meta_request_allowance=0,
            route_requests_per_call=1,
            route_request_limit=2,
        )
    from proofhunter.github import create_check_port, create_source_port
    from proofhunter.meta import create_meta_port
    from proofhunter.snap import create_route_port

    enrollments = load_enrollments(settings.enrollments_path)
    return VerificationPipeline(
        settings,
        enrollments[0],
        create_source_port(settings, enrollments),
        create_meta_port(settings),
        create_route_port(settings),
        create_runner_port(settings),
        create_check_port(settings),
        store,
        clock or WallClock(),
    )
