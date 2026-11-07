"""Track A episode runner: coverage guard, 6 pairs, 1 fresh-head confirmation, 2 retries.

Production evidence semantics are reused read-only: ``proofhunter.oracle`` computes the
trusted expected value before execution, assesses each pair, decides whether the
reserved confirmation runs and classifies the verdict; ``proofhunter.bayes`` provides the
coverage guard and probe bookkeeping. Selectors only choose among guarded candidates.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fixtures.adapters import SequentialIds
from fixtures.catalog import demo_enrollment
from proofhunter import bayes, oracle
from proofhunter.contracts import (
    Candidate,
    ConfirmationStatus,
    Enrollment,
    EvidenceKind,
    ExecutionMode,
    ExecutionRequest,
    HypothesisPlan,
    HypothesisPlanPayload,
    ObservationOutcome,
    ProbeRecord,
    ProviderMode,
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
    builtin_hypothesis_payload,
    git_blob_sha1,
    sha256_hex,
)
from research.execution.allowlist import ReviewedSourceAllowlist, from_template_render
from research.execution.budget import EpisodeBudget
from research.execution.pool import builtin_plan, plan_from_payload, seeded_pool
from research.execution.sandbox import LocalTemplateSandbox, PhaseTiming
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
    Confirmation,
    CostStatus,
    Invocations,
    MeasurementMode,
    ModelRefs,
    Outcome,
    ProbeSummary,
    ProtocolV1,
    RawResultRow,
    Timings,
    TokenUsage,
    Track,
)

OUTCOME = {
    EvidenceKind.MATCH: Outcome.MATCH,
    EvidenceKind.HEAD_MISMATCH: Outcome.MISMATCH,
    EvidenceKind.SHARED_MISMATCH: Outcome.BASE_MISMATCH,
    EvidenceKind.BASE_MISMATCH: Outcome.BASE_MISMATCH,
    EvidenceKind.EXCLUDED: Outcome.UNUSABLE,
}
CONFIRMATION = {
    ConfirmationStatus.NOT_ATTEMPTED: Confirmation.NOT_RUN,
    ConfirmationStatus.REPRODUCED: Confirmation.REPRODUCED,
    ConfirmationStatus.DISAGREED: Confirmation.NOT_REPRODUCED,
    ConfirmationStatus.UNUSABLE: Confirmation.UNUSABLE,
}
#: Deterministic failures: retrying cannot change the result, so no retry is spent.
NO_RETRY = frozenset({ObservationOutcome.DENIED})


def implementation_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,  # noqa: S607
                             timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "0" * 40
    return out if len(out) == 40 else "0" * 40


def _source(side: str, case_id: str, data: bytes) -> SourceFile:
    commit = hashlib.sha1(f"research-track-a:{case_id}:{side}:{sha256_hex(data)}".encode(),
                          usedforsecurity=False).hexdigest()
    return SourceFile(side=side, commit_sha=commit, path="checkout.py",  # type: ignore[arg-type]
                      blob_sha=git_blob_sha1(data), content=data.decode("utf-8"),
                      content_sha256=sha256_hex(data), size_bytes=len(data))


def unified_diff(base: bytes, head: bytes) -> str:
    return "".join(difflib.unified_diff(
        base.decode().splitlines(keepends=True), head.decode().splitlines(keepends=True),
        "a/checkout.py", "b/checkout.py"))


def guarded(available: list[Candidate], missing: set[Any], remaining: int
            ) -> tuple[list[Candidate], bool]:
    """Production coverage guard (``bayes.select``), applied identically before every arm."""
    if missing and remaining <= len(missing):
        covering = [c for c in available if missing.intersection(c.coverage_tags)]
        if covering:
            return covering, True
    return available, False


@dataclass
class EpisodeTrace:
    """Execution-side evidence kept beside the label-free row (not a frozen schema)."""

    probes: list[ProbeRecord] = field(default_factory=list)
    observations: list[SandboxObservation] = field(default_factory=list)
    timings: list[tuple[Side, bool, PhaseTiming]] = field(default_factory=list)
    stop: ReasonCode = ReasonCode.BUDGET_EXHAUSTED
    plan_fallback: str | None = None


@dataclass
class TrackARunner:
    """Implements ``research.schemas.interfaces.EpisodeRunner`` for Track A."""

    protocol: ProtocolV1
    ledger: Ledger
    allowlist: ReviewedSourceAllowlist | None = None
    enrollment: Enrollment = field(default_factory=demo_enrollment)
    sandbox_factory: Callable[[ReviewedSourceAllowlist, Enrollment], LocalTemplateSandbox] = (
        LocalTemplateSandbox)
    implementation: str = field(default_factory=implementation_sha)
    episodes: int = 0
    last_trace: EpisodeTrace | None = None

    def run(self, sources: CaseSources, selector: Selector,
            generator: HypothesisGenerator | None, *, arm_id: str, seed: int,
            repetition: int, mode: MeasurementMode) -> RawResultRow:
        wall0 = time.perf_counter()
        case = sources.case
        allowlist = self.allowlist if self.allowlist is not None else from_template_render(
            [sources])
        sandbox = self.sandbox_factory(allowlist, self.enrollment)
        trace = EpisodeTrace()
        base_file = _source("base", case.case_id, sources.base_source)
        head_file = _source("head", case.case_id, sources.head_source)
        binding = oracle.ProvenanceBinding.for_run(
            base_sha=base_file.commit_sha, head_sha=head_file.commit_sha,
            base_source_sha256=base_file.content_sha256,
            head_source_sha256=head_file.content_sha256, enrollment=self.enrollment,
            execution_mode=ExecutionMode.OFFLINE_FIXTURE)

        gen0 = time.perf_counter()
        plan, payload = self._plan(sources, generator, trace)
        generation_ms = (time.perf_counter() - gen0) * 1000 if generator is not None else None
        bank = seeded_pool(case, self.protocol, seed)
        diff = unified_diff(sources.base_source, sources.head_source)
        selector.reset(EpisodeContext(case.case_id, seed, sources.base_source,
                                      sources.head_source, diff, bank,
                                      payload))

        budget = EpisodeBudget.from_protocol(self.protocol)
        belief = bayes.initialize(plan).belief
        ids = SequentialIds(namespace=seed & 0xFFFF)
        run_id = ids.new_run_id()
        used: set[str] = set()
        history: list[PairFeedback] = []
        retries: list[int] = []
        guard_used = False
        resources = ResourcePolicy(
            execution_timeout_seconds=self.protocol.invocation_timeout_seconds)

        def observe(side: Side, candidate: Candidate, *, retry: bool) -> SandboxObservation:
            budget.charge(side, candidate.input_sha256, retry=retry)
            source = base_file if side is Side.BASE else head_file
            inp = candidate.as_input()
            request = ExecutionRequest(
                run_id=run_id, attempt_id=ids.new_attempt_id(), side=side,
                commit_sha=source.commit_sha, source_path="checkout.py",
                source_content=source.content, source_sha256=source.content_sha256,
                input=inp, input_sha256=candidate.input_sha256,
                entrypoint=self.enrollment.entrypoint, resources=resources,
                snapshot_id=self.enrollment.snapshot_id,
                image_digest=self.enrollment.image_digest,
                harness_version=self.enrollment.harness_version,
                harness_sha256=self.enrollment.harness_sha256)
            obs, timing = sandbox.invoke(request)
            if not obs.usable:
                budget.failed += 1
            trace.observations.append(obs)
            trace.timings.append((side, retry, timing))
            return obs

        def observe_with_retry(side: Side, candidate: Candidate) -> tuple[SandboxObservation,
                                                                          int]:
            obs = observe(side, candidate, retry=False)
            spent = 0
            while (not obs.usable and obs.outcome not in NO_RETRY and budget.can_retry()):
                obs = observe(side, candidate, retry=True)
                spent += 1
            return obs, spent

        while budget.remaining_pairs > 0:
            available = [c for c in bank.candidates if c.input_sha256 not in used]
            if not available:
                trace.stop = ReasonCode.INPUTS_EXHAUSTED
                break
            coverage = oracle.compute_coverage(trace.probes, guard_used)
            pool, applied = guarded(available, set(coverage.missing), budget.remaining_pairs)
            choice = selector.choose(EpisodeState(
                step=len(trace.probes) + 1, remaining_pairs=budget.remaining_pairs,
                available=tuple(pool), missing_coverage=tuple(coverage.missing),
                history=tuple(history)))
            candidate = next((c for c in pool if c.input_sha256 == choice), None)
            if candidate is None:
                raise ValueError(f"selector {arm_id!r} chose a candidate outside the guarded pool")
            selection = bayes.select(belief, [candidate], SelectionPolicy(
                remaining_pairs=budget.remaining_pairs, missing_coverage=coverage.missing))
            guard_used = guard_used or applied or (
                selection.reason is SelectionReason.COVERAGE_GUARD)
            used.add(candidate.input_sha256)
            expected = oracle.evaluate(self.enrollment, candidate)
            if oracle.trusted_expected(expected) is None:
                trace.stop = ReasonCode.ORACLE_UNAVAILABLE
                break
            budget.open_pair(candidate.input_sha256)
            base, base_retries = observe_with_retry(Side.BASE, candidate)
            head, head_retries = observe_with_retry(Side.HEAD, candidate)
            assessment = oracle.assess_probe(expected, base, head,
                                             input_digest=candidate.input_sha256,
                                             binding=binding)
            confirmation = None
            if assessment.needs_confirmation and budget.confirm < budget.max_confirmations:
                confirmation = observe(Side.CONFIRM, candidate, retry=False)
                assessment = oracle.assess_probe(expected, base, head, confirmation,
                                                 input_digest=candidate.input_sha256,
                                                 binding=binding)
            updated = (bayes.update(belief, selection, assessment.outcome)
                       if assessment.outcome is not None else belief)
            trace.probes.append(ProbeRecord(
                probe_no=len(trace.probes) + 1, candidate=candidate,
                candidate_pool_sha256=selection.candidate_pool_sha256,
                selection_reason=selection.reason, route=RouteDecision(
                    choice=candidate.strategy, eligible=[candidate.strategy],
                    mode=RouteMode.POLICY, model_id=None, probabilities=None, confidence=None,
                    provider_request_id=None, latency_ms=None,
                    reason_code=RouteReasonCode.SINGLE_ELIGIBLE),
                oracle=expected, base=base, head=head, confirmation=confirmation,
                confirmation_status=assessment.confirmation_status,
                prior=belief.probabilities,
                posterior=updated.probabilities if assessment.outcome is not None else None,
                likelihood_mismatch=selection.likelihood_mismatch,
                predictions=selection.predictions, eig_bits=selection.eig_bits,
                evidence_kind=assessment.evidence_kind,
                exclusion_reason=assessment.exclusion_reason))
            belief = updated
            retries.append(base_retries + head_retries)
            history.append(PairFeedback(
                step=len(trace.probes), candidate_input_sha256=candidate.input_sha256,
                outcome=OUTCOME[assessment.evidence_kind],
                expected_cents=oracle.trusted_expected(expected),
                base_cents=oracle.exact_value(base), head_cents=oracle.exact_value(head)))
            if assessment.confirmation_status is ConfirmationStatus.REPRODUCED:
                trace.stop = ReasonCode.CONFIRMED_EVIDENCE
                break
            if ObservationOutcome.DENIED in (base.outcome, head.outcome):
                trace.stop = ReasonCode.ISOLATION_UNVERIFIED
                break

        coverage = oracle.compute_coverage(trace.probes, guard_used)
        verdict = oracle.classify(trace.probes, coverage, trace.stop)
        probes = [ProbeSummary(
            step=p.probe_no, candidate_input_sha256=p.candidate.input_sha256,
            outcome=OUTCOME[p.evidence_kind], confirmation=CONFIRMATION[p.confirmation_status],
            retries=r) for p, r in zip(trace.probes, retries, strict=True)]
        first = next((p.step for p in probes if p.outcome is Outcome.MISMATCH
                      and p.confirmation is Confirmation.REPRODUCED), None)
        phase = trace.timings
        probe_t = [t for s, _, t in phase if s is not Side.CONFIRM]
        conf_t = [t for s, _, t in phase if s is Side.CONFIRM]
        launched = [t for _, _, t in phase if t.refusal is None]
        self.last_trace = trace
        cold = self.episodes == 0
        self.episodes += 1
        local_only = generator is None and getattr(selector, "paid", False) is False
        row = RawResultRow(
            schema_version="research-raw-result-v1", protocol_version="research-protocol-v1",
            run_id=_ident(f"{arm_id}:{case.case_id}:{seed}:{repetition}"),
            implementation_sha=self.implementation, track=Track.A,
            case_id=case.case_id, split=case.split, arm_id=arm_id, seed=seed,
            repetition=repetition, measurement_mode=mode, snap_routing="disabled",
            pool_sha256=bank.bank_sha256, models=_model_refs(selector, generator),
            probes=probes,
            invocations=Invocations(base=budget.base, head=budget.head, confirm=budget.confirm,
                                    failed=budget.failed),
            verdict=verdict.verdict, reason_code=verdict.reason_code.value,
            pairs_to_first_confirmed_witness=first, censored=first is None,
            timings=Timings(
                wall_ms=(time.perf_counter() - wall0) * 1000, queue_ms=None,
                generation_ms=generation_ms, feature_ms=None, ranking_ms=None,
                sandbox_startup_ms=sum(t.startup_ms for t in launched) if launched else None,
                execution_ms=sum(t.execution_ms for t in probe_t) if probe_t else None,
                confirmation_ms=(sum(t.startup_ms + t.execution_ms for t in conf_t)
                                 if conf_t else None),
                cleanup_ms=sum(t.cleanup_ms for _, _, t in phase) if phase else None,
                cold_start=cold),
            tokens=TokenUsage(input_tokens=None, output_tokens=None, reasoning_tokens=None),
            cost_usd=0.0 if local_only else None,
            cost_status=CostStatus.MEASURED if local_only else CostStatus.UNKNOWN)
        if not all(t.cleaned for _, _, t in phase):
            raise RuntimeError("sandbox cleanup evidence missing for an invocation")
        return row

    def _plan(self, sources: CaseSources, generator: HypothesisGenerator | None,
              trace: EpisodeTrace) -> tuple[HypothesisPlan, HypothesisPlanPayload]:
        if generator is not None:
            try:
                prompt = importlib.import_module("research.generator.prompt")
                payload = HypothesisPlanPayload.model_validate(
                    generator.propose(prompt.build_input(sources)).model_dump(mode="json"))
                return plan_from_payload(payload, ProviderMode.LIVE), payload
            except Exception as error:
                trace.plan_fallback = type(error).__name__
        return builtin_plan(), builtin_hypothesis_payload()


def _ident(text: str) -> str:
    return text if len(text) <= 128 else "sha256:" + sha256_hex(text.encode())


def _model_refs(selector: Selector, generator: HypothesisGenerator | None) -> ModelRefs:
    refs = getattr(selector, "model_refs", None)
    if isinstance(refs, ModelRefs):
        return refs
    return ModelRefs(generator_model_id=getattr(generator, "generator_id", None),
                     generator_revision=None, adapter_sha256=None,
                     ranker_checkpoint_sha256=None, frontier_model_id=None, prompt_sha256=None)


def make_runner(protocol: ProtocolV1, ledger: Ledger) -> TrackARunner:
    """Frozen entry point; allowlist is re-derived from the reviewed template registry."""
    return TrackARunner(protocol=protocol, ledger=ledger)

