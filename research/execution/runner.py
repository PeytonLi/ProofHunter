"""Episode runner (``I.EpisodeRunner``): one (case, arm, seed, repetition) under the frozen budget.

Loop: coverage guard -> selector choice -> trusted oracle -> base and head invocation (bounded
retries for infra outcomes) -> production pair assessment -> one fresh-head confirmation after a
usable mismatch -> feedback. The verdict comes from production ``oracle.classify``.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Final

from pydantic import ValidationError

from fixtures import catalog
from proofhunter import bayes, oracle
from proofhunter.contracts import (
    Candidate,
    ConfirmationStatus,
    Enrollment,
    EvidenceKind,
    ExecutionMode,
    HypothesisPlanPayload,
    ObservationOutcome,
    ProbeRecord,
    ProviderMode,
    ReasonCode,
    RouteDecision,
    RouteMode,
    RouteReasonCode,
    RunnerMode,
    SandboxObservation,
    SelectionPolicy,
    SelectionReason,
    Side,
    Verdict,
    builtin_hypothesis_payload,
    canonical_json,
    canonical_sha256,
    sha256_hex,
)
from research.execution.pool import as_plan, shared_pool
from research.execution.process_sandbox import PhasedExecution, ProcessTemplateExecutor
from research.execution.sandbox import Execution, Executor
from research.schemas.interfaces import (
    CaseSources,
    EpisodeContext,
    EpisodeState,
    HypothesisGenerator,
    Ledger,
    PairFeedback,
    Selector,
)
from research.schemas.models import (
    ArmSpec,
    Confirmation,
    CostStatus,
    GeneratorInput,
    Invocations,
    MeasurementMode,
    ModelRefs,
    Outcome,
    ProbeSummary,
    ProtocolV1,
    RawResultRow,
    Timings,
    TokenUsage,
)

REPO: Final = Path(__file__).resolve().parents[2]
RETRYABLE: Final = frozenset({ObservationOutcome.TIMEOUT, ObservationOutcome.INFRA_ERROR})
GENERATOR_FAILURE_REASONS: Final = ("generator_unavailable", "generator_invalid_output")

#: Reviews that ``CaseSources`` are exactly reviewed template output; returns a reason or None.
SourceReview = Callable[[CaseSources], str | None]
#: Builds the generator prompt (generator lane ``research.generator.prompt.build_input``).
PromptBuilder = Callable[[CaseSources], GeneratorInput]


class BudgetViolation(RuntimeError):
    """Raised if the runner would exceed a frozen bound (a runner bug, never expected)."""


def implementation_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def unified_diff(base: bytes, head: bytes) -> str:
    return "".join(difflib.unified_diff(
        base.decode().splitlines(keepends=True), head.decode().splitlines(keepends=True),
        fromfile="a/checkout.py", tofile="b/checkout.py"))


def template_review(sources: CaseSources) -> str | None:
    """Default review: sources must equal the dataset lane's reviewed template rendering."""
    case = sources.case
    if hashlib.sha256(sources.base_source).hexdigest() != case.base_source_sha256:
        return "base_source_hash_mismatch"
    if hashlib.sha256(sources.head_source).hexdigest() != case.head_source_sha256:
        return "head_source_hash_mismatch"
    try:
        templates: Any = importlib.import_module("research.data.templates")
    except ModuleNotFoundError:
        return "template_registry_unavailable"
    info = {t.template_id: t for t in templates.list_templates()}.get(case.template_id)
    if info is None or (info.template_version, info.template_sha256) != (
        case.template_version, case.template_sha256
    ):
        return "template_not_reviewed"
    rendered = templates.render(case.template_id, case.params)
    if rendered != (sources.base_source, sources.head_source):
        return "sources_differ_from_template_render"
    return None


def default_prompt_builder(sources: CaseSources) -> GeneratorInput:
    built: GeneratorInput = importlib.import_module("research.generator.prompt").build_input(
        sources)
    return built


def _commit(source_sha256: str) -> str:
    return hashlib.sha1(bytes.fromhex(source_sha256)).hexdigest()  # noqa: S324 - identifier only


def _outcome(kind: EvidenceKind) -> Outcome:
    return {
        EvidenceKind.MATCH: Outcome.MATCH,
        EvidenceKind.HEAD_MISMATCH: Outcome.MISMATCH,
        EvidenceKind.BASE_MISMATCH: Outcome.BASE_MISMATCH,
        EvidenceKind.SHARED_MISMATCH: Outcome.BASE_MISMATCH,
    }.get(kind, Outcome.UNUSABLE)


def _confirmation(status: ConfirmationStatus) -> Confirmation:
    return {
        ConfirmationStatus.REPRODUCED: Confirmation.REPRODUCED,
        ConfirmationStatus.DISAGREED: Confirmation.NOT_REPRODUCED,
        ConfirmationStatus.UNUSABLE: Confirmation.UNUSABLE,
    }.get(status, Confirmation.NOT_RUN)


@dataclass
class _Episode:
    run_id: str
    enrollment: Enrollment
    sources: CaseSources
    executor: Executor
    max_invocations: int
    max_retries: int
    counts: dict[str, int] = field(default_factory=lambda: {"base": 0, "head": 0, "confirm": 0})
    failed: int = 0
    retries: int = 0
    attempts: int = 0
    execution_ms: float = 0.0
    confirmation_ms: float = 0.0
    startup_ms: float | None = None
    cleanup_ms: float | None = None

    @property
    def invocations(self) -> int:
        return sum(self.counts.values())

    def observe(self, side: Side, candidate: Candidate) -> SandboxObservation:
        if self.invocations >= self.max_invocations:
            raise BudgetViolation("invocation ceiling reached")
        self.counts[side.value] += 1
        self.attempts += 1
        source = self.sources.base_source if side is Side.BASE else self.sources.head_source
        source_sha = hashlib.sha256(source).hexdigest()
        result: Execution = self.executor(side.value, source, candidate.as_input())
        run_ms = result.duration_ms
        if isinstance(result, PhasedExecution):
            self.startup_ms = (self.startup_ms or 0.0) + result.startup_ms
            self.cleanup_ms = (self.cleanup_ms or 0.0) + result.cleanup_ms
            run_ms = result.execution_ms
            if not result.cleaned:
                result = Execution(ObservationOutcome.INFRA_ERROR, None, result.duration_ms)
        if side is Side.CONFIRM:
            self.confirmation_ms += run_ms
        else:
            self.execution_ms += run_ms
        ok = result.outcome is ObservationOutcome.OK
        if not ok:
            self.failed += 1
        stdout = canonical_json({"value": result.value}) if ok else b""
        attempt_id = (
            f"00000000-0000-4000-8000-{int(self.run_id[-8:], 16) % 16**4:04x}{self.attempts:08x}"
        )
        e = self.enrollment
        return SandboxObservation(
            attempt_id=attempt_id, side=side, commit_sha=_commit(source_sha),
            source_sha256=source_sha, input_sha256=candidate.input_sha256,
            runner_mode=RunnerMode.OFFLINE_FIXTURE, snapshot_id=e.snapshot_id,
            image_digest=e.image_digest, harness_sha256=e.harness_sha256, sandbox_id=None,
            started_at=datetime.now(UTC), duration_ms=int(result.duration_ms),
            outcome=result.outcome, value=result.value if ok else None,
            exit_code=0 if ok else (1 if result.outcome is ObservationOutcome.NONZERO else None),
            stdout_sha256=sha256_hex(stdout), stderr_sha256=sha256_hex(b""), truncated=False)

    def observe_with_retry(
        self, side: Side, candidate: Candidate
    ) -> tuple[SandboxObservation, int]:
        used = 0
        observation = self.observe(side, candidate)
        while (observation.outcome in RETRYABLE and self.retries < self.max_retries
               and self.invocations < self.max_invocations):
            self.retries += 1
            used += 1
            observation = self.observe(side, candidate)
        return observation, used


@dataclass
class ResearchEpisodeRunner:
    protocol: ProtocolV1
    ledger: Ledger
    executor: Executor
    review: SourceReview = template_review
    prompt_builder: PromptBuilder = default_prompt_builder
    implementation: str = field(default_factory=implementation_sha)
    enrollment: Enrollment = field(default_factory=catalog.demo_enrollment)
    events: list[dict[str, object]] = field(default_factory=list)

    def arm(self, arm_id: str) -> ArmSpec:
        for spec in self.protocol.arms:
            if spec.arm_id == arm_id:
                return spec
        raise ValueError(f"arm {arm_id!r} is not in the frozen protocol")

    def run(
        self,
        sources: CaseSources,
        selector: Selector,
        generator: HypothesisGenerator | None,
        *,
        arm_id: str,
        seed: int,
        repetition: int,
        mode: MeasurementMode,
    ) -> RawResultRow:
        spec = self.arm(arm_id)
        case = sources.case
        start = perf_counter_ns()
        ledger_before = len(self.ledger.entries())
        run_id = "run-" + canonical_sha256({
            "implementation": self.implementation, "arm": arm_id, "case": case.case_id,
            "seed": seed, "repetition": repetition, "mode": mode.value})[:24]
        episode = _Episode(run_id, self.enrollment, sources, self.executor,
                           self.protocol.max_invocations_per_episode,
                           self.protocol.max_retries_per_episode)
        if hasattr(self.executor, "reset"):
            self.executor.reset()
        timings: dict[str, float | None] = {"generation_ms": None, "ranking_ms": None}
        probes: list[ProbeRecord] = []
        summaries: list[ProbeSummary] = []
        bank = shared_pool(case, self.protocol)

        def finish(verdict: str, reason: str) -> RawResultRow:
            return self._row(run_id, sources, spec, seed, repetition, mode, bank.bank_sha256,
                             summaries, episode, verdict, reason, timings, start, ledger_before,
                             selector, generator)

        reason = self.review(sources)
        if reason is not None:
            self.events.append({"run_id": run_id, "event": "source_review_failed",
                                "reason": reason})
            return finish("inconclusive", "source_invalid")

        plan_payload: HypothesisPlanPayload
        plan_mode = ProviderMode.FALLBACK
        if generator is None:
            plan_payload = builtin_hypothesis_payload()
        else:
            generation_start = perf_counter_ns()
            try:
                plan_payload = HypothesisPlanPayload.model_validate(
                    generator.propose(self.prompt_builder(sources)).model_dump())
                plan_mode = (ProviderMode.LIVE if mode is MeasurementMode.LIVE
                             else ProviderMode.OFFLINE_FIXTURE)
            except (ValidationError, ValueError) as error:
                plan_payload = builtin_hypothesis_payload()
                self._fallback(run_id, generator, "generator_invalid_output", error)
            except Exception as error:  # provider outage, timeout, transport
                plan_payload = builtin_hypothesis_payload()
                self._fallback(run_id, generator, "generator_unavailable", error)
            timings["generation_ms"] = (perf_counter_ns() - generation_start) / 1e6
        plan = as_plan(plan_payload, mode=plan_mode)
        context = EpisodeContext(
            case_id=case.case_id, seed=seed, base_source=sources.base_source,
            head_source=sources.head_source,
            unified_diff=unified_diff(sources.base_source, sources.head_source),
            bank=bank, plan=plan_payload)
        ranking_ns = 0
        try:
            t0 = perf_counter_ns()
            selector.reset(context)
            ranking_ns += perf_counter_ns() - t0
        except Exception as error:
            self.events.append({"run_id": run_id, "event": "selector_failed",
                                "phase": "reset", "error": type(error).__name__})
            timings["ranking_ms"] = ranking_ns / 1e6
            return finish("inconclusive", "provider_unavailable")

        belief = bayes.initialize(plan).belief
        binding = oracle.ProvenanceBinding.for_run(
            base_sha=_commit(case.base_source_sha256), head_sha=_commit(case.head_source_sha256),
            base_source_sha256=case.base_source_sha256,
            head_source_sha256=case.head_source_sha256, enrollment=self.enrollment,
            execution_mode=ExecutionMode.OFFLINE_FIXTURE)
        required = set(self.protocol.required_clean_coverage)
        used: set[str] = set()
        history: list[PairFeedback] = []
        guard = False
        stop = ReasonCode.BUDGET_EXHAUSTED
        while len(probes) < self.protocol.max_pairs:
            remaining = self.protocol.max_pairs - len(probes)
            pool = [c for c in bank.candidates if c.input_sha256 not in used]
            if not pool:
                stop = ReasonCode.INPUTS_EXHAUSTED
                break
            coverage = oracle.compute_coverage(probes, guard)
            missing = sorted(required.intersection(coverage.missing))
            available = pool
            if missing and remaining <= len(missing):
                covering = [c for c in pool if set(missing).intersection(c.coverage_tags)]
                if covering:
                    available, guard = covering, True
            state = EpisodeState(
                step=len(probes) + 1, remaining_pairs=remaining, available=tuple(available),
                missing_coverage=tuple(missing),
                history=tuple(history) if spec.feedback == "pair_observation" else ())
            try:
                t0 = perf_counter_ns()
                choice = selector.choose(state)
                ranking_ns += perf_counter_ns() - t0
            except Exception as error:
                self.events.append({"run_id": run_id, "event": "selector_failed",
                                    "phase": "choose", "error": type(error).__name__})
                stop = ReasonCode.PROVIDER_UNAVAILABLE
                break
            chosen = next((c for c in available if c.input_sha256 == choice), None)
            if chosen is None:
                self.events.append({"run_id": run_id, "event": "selector_invalid_choice"})
                stop = ReasonCode.PROVIDER_INVALID_RESPONSE
                break
            selection = bayes.select(belief, [chosen], SelectionPolicy(
                remaining_pairs=remaining, missing_coverage=[]))
            used.add(chosen.input_sha256)
            expected = oracle.evaluate(self.enrollment, chosen)
            if oracle.trusted_expected(expected) is None:
                stop = ReasonCode.ORACLE_UNAVAILABLE
                break
            base, base_retries = episode.observe_with_retry(Side.BASE, chosen)
            head, head_retries = episode.observe_with_retry(Side.HEAD, chosen)
            assessment = oracle.assess_probe(expected, base, head,
                                             input_digest=chosen.input_sha256, binding=binding)
            confirmation = None
            if (assessment.needs_confirmation
                    and episode.counts["confirm"] < self.protocol.max_confirmations
                    and episode.invocations < self.protocol.max_invocations_per_episode):
                confirmation = episode.observe(Side.CONFIRM, chosen)
            assessment = oracle.assess_probe(expected, base, head, confirmation,
                                             input_digest=chosen.input_sha256, binding=binding)
            posterior = None
            if assessment.outcome is not None:
                belief_after = bayes.update(belief, selection, assessment.outcome)
                posterior = belief_after.probabilities
            route = RouteDecision(
                choice=chosen.strategy, eligible=[chosen.strategy], mode=RouteMode.POLICY,
                model_id=None, probabilities=None, confidence=None, provider_request_id=None,
                latency_ms=None, reason_code=RouteReasonCode.SINGLE_ELIGIBLE)
            probes.append(ProbeRecord(
                probe_no=len(probes) + 1, candidate=chosen,
                candidate_pool_sha256=selection.candidate_pool_sha256,
                selection_reason=(SelectionReason.COVERAGE_GUARD if available is not pool
                                  else selection.reason),
                route=route, oracle=expected, base=base, head=head, confirmation=confirmation,
                confirmation_status=assessment.confirmation_status, prior=belief.probabilities,
                posterior=posterior, likelihood_mismatch=selection.likelihood_mismatch,
                predictions=selection.predictions, eig_bits=selection.eig_bits,
                evidence_kind=assessment.evidence_kind,
                exclusion_reason=assessment.exclusion_reason))
            if posterior is not None:
                belief = belief_after
            outcome = _outcome(assessment.evidence_kind)
            summaries.append(ProbeSummary(
                step=len(probes), candidate_input_sha256=chosen.input_sha256, outcome=outcome,
                confirmation=_confirmation(assessment.confirmation_status),
                retries=base_retries + head_retries))
            usable = outcome is not Outcome.UNUSABLE
            history.append(PairFeedback(
                step=len(probes), candidate_input_sha256=chosen.input_sha256, outcome=outcome,
                expected_cents=oracle.trusted_expected(expected),
                base_cents=base.value if usable else None,
                head_cents=head.value if usable else None))
            if assessment.confirmation_status is ConfirmationStatus.REPRODUCED:
                stop = ReasonCode.CONFIRMED_EVIDENCE
                break
        timings["ranking_ms"] = ranking_ns / 1e6
        coverage = oracle.compute_coverage(probes, guard)
        result = oracle.classify(probes, coverage, stop)
        return finish(result.verdict.value, result.reason_code.value)

    def _fallback(self, run_id: str, generator: HypothesisGenerator, reason: str,
                  error: Exception) -> None:
        self.events.append({"run_id": run_id, "event": "generator_fallback", "reason": reason,
                            "generator_id": generator.generator_id,
                            "error": type(error).__name__})

    def _row(self, run_id: str, sources: CaseSources, spec: ArmSpec, seed: int, repetition: int,
             mode: MeasurementMode, pool_sha256: str, summaries: Sequence[ProbeSummary],
             episode: _Episode, verdict: str, reason: str, timings: dict[str, float | None],
             start: int, ledger_before: int, selector: Selector,
             generator: HypothesisGenerator | None) -> RawResultRow:
        uses_model = generator is not None or spec.kind.value == "frontier"
        new_entries = list(self.ledger.entries())[ledger_before:]
        cost: float | None
        if new_entries:
            known = [e.cost_usd for e in new_entries]
            cost = None if any(c is None for c in known) else float(sum(c or 0.0 for c in known))
            estimated = any(e.cost_status is CostStatus.ESTIMATED for e in new_entries)
            status = (CostStatus.UNKNOWN if cost is None
                      else CostStatus.ESTIMATED if estimated else CostStatus.MEASURED)
        elif uses_model and mode is not MeasurementMode.OFFLINE:
            cost, status = None, CostStatus.UNKNOWN
        else:
            cost, status = 0.0, CostStatus.MEASURED
        first = next((p.step for p in summaries if p.outcome is Outcome.MISMATCH
                      and p.confirmation is Confirmation.REPRODUCED), None)
        return RawResultRow(
            schema_version="research-raw-result-v1", protocol_version="research-protocol-v1",
            run_id=run_id, implementation_sha=self.implementation, track=sources.case.track,
            case_id=sources.case.case_id, split=sources.case.split, arm_id=spec.arm_id,
            seed=seed, repetition=repetition, measurement_mode=mode, snap_routing="disabled",
            pool_sha256=pool_sha256,
            models=ModelRefs(
                generator_model_id=getattr(generator, "generator_id", None),
                generator_revision=getattr(generator, "revision", None),
                adapter_sha256=getattr(generator, "adapter_sha256", None),
                ranker_checkpoint_sha256=getattr(selector, "checkpoint_sha256", None),
                frontier_model_id=getattr(selector, "model_id", None),
                prompt_sha256=getattr(selector, "prompt_sha256", None)),
            probes=list(summaries),
            invocations=Invocations(base=episode.counts["base"], head=episode.counts["head"],
                                    confirm=episode.counts["confirm"], failed=episode.failed),
            verdict="failed" if verdict == "failed" else Verdict(verdict), reason_code=reason,
            pairs_to_first_confirmed_witness=first,
            censored=first is None,
            timings=Timings(
                wall_ms=(perf_counter_ns() - start) / 1e6, queue_ms=None,
                generation_ms=timings["generation_ms"], feature_ms=None,
                ranking_ms=timings["ranking_ms"], sandbox_startup_ms=episode.startup_ms,
                execution_ms=episode.execution_ms,
                confirmation_ms=episode.confirmation_ms if episode.counts["confirm"] else None,
                cleanup_ms=episode.cleanup_ms, cold_start=None),
            tokens=TokenUsage(input_tokens=None, output_tokens=None, reasoning_tokens=None),
            cost_usd=cost, cost_status=status)


def make_runner(protocol: ProtocolV1, ledger: Ledger) -> ResearchEpisodeRunner:
    """Frozen-interface factory: reviewed-template executor with the protocol timeout."""
    reviewed: set[str] = set()

    def review(sources: CaseSources) -> str | None:
        reason = template_review(sources)
        if reason is None:
            reviewed.update({sources.case.base_source_sha256, sources.case.head_source_sha256})
        return reason

    executor = ProcessTemplateExecutor(reviewed.__contains__,
                                       timeout_seconds=protocol.invocation_timeout_seconds)
    return ResearchEpisodeRunner(protocol=protocol, ledger=ledger, executor=executor,
                                 review=review)
