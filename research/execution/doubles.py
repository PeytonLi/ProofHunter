"""Reviewed offline doubles for integration tests and drills (no provider or dataset code).

Sources come from the frozen, reviewed production benchmark corpus (``bench/corpus``); they are
used only to exercise the runner and are never research training or evaluation cases.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from bench.corpus import ROOT, load
from bench.freeze import load_policy
from fixtures.catalog import demo_enrollment
from proofhunter import bayes
from proofhunter.contracts import (
    BinaryOutcome,
    HypothesisPlanPayload,
    ObservationOutcome,
    ProviderMode,
    SelectionPolicy,
    Strategy,
    Verdict,
    builtin_hypothesis_payload,
    canonical_sha256,
    input_sha256,
)
from research.execution.allowlist import from_bench_corpus
from research.execution.pool import as_plan
from research.execution.runner import ResearchEpisodeRunner
from research.execution.sandbox import (
    Executor,
    FaultInjectingExecutor,
    FaultKey,
    ProcessTemplateExecutor,
    ReviewedTemplateExecutor,
)
from research.schemas.interfaces import CaseSources, EpisodeContext, EpisodeState
from research.schemas.models import (
    ArmSpec,
    CaseLabel,
    CaseManifest,
    CaseRecord,
    CheckoutInput,
    ContextFile,
    GeneratorInput,
    LabelKind,
    LedgerEntry,
    ProtocolV1,
    Split,
    Track,
    Witness,
)

STRATEGY_ORDER = (Strategy.BOUNDARY, Strategy.CONSTRAINT, Strategy.SEEDED_FUZZ)
TEST_SHA = "0" * 40


def bench_sources(case_id: str, *, split: Split = Split.VALIDATION,
                  candidate_seed: int = 1633385816) -> CaseSources:
    case = next(c for c in load().cases if c.case_id == case_id)
    base = (ROOT / case.base_path).read_bytes()
    head = (ROOT / case.head_path).read_bytes()
    params: dict[str, int | bool | str | list[int]] = {"bench_case": case_id}
    record = CaseRecord(
        schema_version="research-case-v1", track=Track.A, case_id=f"bench-{case_id}",
        split=split, family_id=f"bench_{case_id}", defect_origin=f"bench-{case_id}",
        near_dup_cluster=f"bench-{case_id}", template_id=f"bench-{case_id}",
        template_version="1", template_sha256=hashlib.sha256(base + head).hexdigest(),
        params=params, params_sha256=canonical_sha256(params),
        contract_id="checkout-rounding@1",
        base_source_sha256=hashlib.sha256(base).hexdigest(),
        head_source_sha256=hashlib.sha256(head).hexdigest(), candidate_seed=candidate_seed)
    return CaseSources(case=record, base_source=base, head_source=head)


def bench_review(sources: CaseSources) -> str | None:
    reviewed = set(load_policy().reviewed_source_sha256)
    for data, digest in ((sources.base_source, sources.case.base_source_sha256),
                         (sources.head_source, sources.case.head_source_sha256)):
        if hashlib.sha256(data).hexdigest() != digest or digest not in reviewed:
            return "template_not_reviewed"
    return None


def bench_prompt(sources: CaseSources) -> GeneratorInput:
    sides: tuple[tuple[Literal["base", "head"], bytes], ...] = (
        ("base", sources.base_source), ("head", sources.head_source))
    files = [ContextFile(side=side, path="checkout.py", sha256=hashlib.sha256(data).hexdigest(),
                         content=data.decode())
             for side, data in sides]
    return GeneratorInput(
        prompt_format_version="integration-double-v1", contract_id="checkout-rounding@1",
        behavior_spec_sha256="0" * 64,
        allowed_test_interface="checkout.total_cents(prices_cents, discount_percent)",
        context_files=files, unified_diff="")


@dataclass
class MemoryLedger:
    rows: list[LedgerEntry] = field(default_factory=list)

    def append(self, entry: LedgerEntry) -> None:
        self.rows.append(LedgerEntry.model_validate(entry.model_dump()))

    def entries(self) -> Sequence[LedgerEntry]:
        return tuple(self.rows)


def offline_runner(protocol: ProtocolV1, ledger: MemoryLedger | None = None, *,
                faults: Mapping[FaultKey, ObservationOutcome | int] | None = None,
                timeout_seconds: float | None = None,
                isolated: bool = False) -> ResearchEpisodeRunner:
    timeout = timeout_seconds or protocol.invocation_timeout_seconds
    inner: Executor
    if isolated:
        inner = ProcessTemplateExecutor(from_bench_corpus(), demo_enrollment(),
                                        timeout_seconds=timeout)
    else:
        reviewed = set(load_policy().reviewed_source_sha256)
        inner = ReviewedTemplateExecutor(reviewed.__contains__, timeout_seconds=timeout)
    return ResearchEpisodeRunner(
        protocol=protocol, ledger=ledger or MemoryLedger(),
        executor=FaultInjectingExecutor(inner, dict(faults or {})), review=bench_review,
        prompt_builder=bench_prompt, implementation=TEST_SHA)


class FixedOrderDouble:
    arm_id = "fixed_order"

    def reset(self, context: EpisodeContext) -> None:
        self.context = context

    def choose(self, state: EpisodeState) -> str:
        best = min(state.available, key=lambda c: (
            not c.is_anchor, STRATEGY_ORDER.index(c.strategy), c.input_sha256))
        return best.input_sha256


class EigDouble:
    """Production EIG over the remaining pool with belief replayed from feedback."""

    arm_id = "builtin_eig"

    def reset(self, context: EpisodeContext) -> None:
        self.plan = as_plan(context.plan, mode=ProviderMode.FALLBACK)
        self.bank = {c.input_sha256: c for c in context.bank.candidates}

    def choose(self, state: EpisodeState) -> str:
        belief = bayes.initialize(self.plan).belief
        for item in state.history:
            if item.outcome.value in ("match", "mismatch"):
                selection = bayes.select(belief, [self.bank[item.candidate_input_sha256]],
                                         SelectionPolicy(remaining_pairs=1,
                                                                 missing_coverage=[]))
                outcome = (BinaryOutcome.MATCH if item.outcome.value == "match"
                           else BinaryOutcome.MISMATCH)
                belief = bayes.update(belief, selection, outcome)
        selection = bayes.select(belief, list(state.available), SelectionPolicy(
            remaining_pairs=state.remaining_pairs, missing_coverage=list(state.missing_coverage)))
        return selection.candidate.input_sha256


class ScriptedSelector:
    """Raises or returns an invalid choice at a given step (outage / malformed drills)."""

    def __init__(self, arm_id: str, *, fail_at: int | None = None,
                 invalid_at: int | None = None, fail_reset: bool = False) -> None:
        self.arm_id = arm_id
        self.fail_at, self.invalid_at, self.fail_reset = fail_at, invalid_at, fail_reset
        self.inner = FixedOrderDouble()
        self.seen_history: list[int] = []

    def reset(self, context: EpisodeContext) -> None:
        if self.fail_reset:
            raise ConnectionError("provider outage")
        self.inner.reset(context)

    def choose(self, state: EpisodeState) -> str:
        self.seen_history.append(len(state.history))
        if state.step == self.fail_at:
            raise TimeoutError("provider timeout")
        if state.step == self.invalid_at:
            return "f" * 64
        return self.inner.choose(state)


class StaticGenerator:
    generator_id = "double-static"
    revision = "double-r1"

    def __init__(self, payload: HypothesisPlanPayload | None = None,
                 error: Exception | None = None, raw: object = None) -> None:
        self.payload, self.error, self.raw = payload, error, raw

    def propose(self, prompt: GeneratorInput) -> HypothesisPlanPayload:
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return HypothesisPlanPayload.model_validate(self.raw)
        return self.payload or builtin_hypothesis_payload()


BENCH_IDS = ("per_line", "half_even", "floor", "aggregate_order", "ignored_discount",
             "clamped_discount", "item_count", "full_discount", "clean_divmod", "clean_split",
             "clean_commute", "existing")


class BenchStore:
    """``I.CaseStore`` over the reviewed bench corpus (integration self-test only)."""

    def __init__(self, split: Split = Split.VALIDATION) -> None:
        self.split = split
        self._sources = {f"bench-{cid}": bench_sources(cid, split=split) for cid in BENCH_IDS}

    def manifest(self) -> CaseManifest:
        cases = [s.case for s in self._sources.values()]
        return CaseManifest(
            schema_version="research-case-manifest-v1", track=Track.A,
            manifest_version="bench-double-v1", split_policy="grouped-family-origin-cluster-v1",
            dataset_commit=TEST_SHA, cases=cases,
            cases_sha256=canonical_sha256([c.model_dump(mode="json") for c in cases]))

    def sources(self, case_id: str) -> CaseSources:
        return self._sources[case_id]

    def labels(self, split: str, *, unseal: str | None = None) -> Sequence[CaseLabel]:
        if split == "test" and unseal is None:
            raise PermissionError("test labels are sealed")
        if split != self.split.value:
            return ()
        out = []
        for case in load().cases:
            w = case.witness
            if case.label == "clean" or w is None:
                out.append(CaseLabel(case_id=f"bench-{case.case_id}", kind=LabelKind.CLEAN,
                                     defect_family=None, expected_verdict=Verdict.NO_DEFECT_FOUND,
                                     witness=None, review_rationale=case.review_rationale))
                continue
            existing = case.expected_verdict == Verdict.CONFIRMED_EXISTING_DEFECT.value
            data = {"prices_cents": list(w.prices_cents), "discount_percent": w.discount_percent}
            out.append(CaseLabel(
                case_id=f"bench-{case.case_id}",
                kind=LabelKind.EXISTING_DEFECT if existing else LabelKind.INTRODUCED_REGRESSION,
                defect_family=case.defect_family.replace("-", "_"),
                expected_verdict=(Verdict.CONFIRMED_EXISTING_DEFECT if existing
                                  else Verdict.CONFIRMED_REGRESSION),
                witness=Witness(input=CheckoutInput.model_validate(data),
                                input_sha256=input_sha256(data),
                                expected_cents=w.expected,
                                base_cents=w.head if existing else w.expected,
                                head_cents=w.head, reproduced_by="reviewed_template_execution"),
                review_rationale=None))
        return out


class RandomDouble:
    def __init__(self, arm_id: str = "uniform_random", *, seed: int) -> None:
        import random

        self.arm_id, self._seed = arm_id, seed
        self._rng = random.Random(seed)  # noqa: S311

    def reset(self, context: EpisodeContext) -> None:
        import random

        self._rng = random.Random(f"{self._seed}:{context.case_id}")  # noqa: S311

    def choose(self, state: EpisodeState) -> str:
        return self._rng.choice(sorted(c.input_sha256 for c in state.available))


class DoubleFactory:
    """``ArmFactory`` stand-in over the doubles (labelled integration self-test)."""

    def selector(self, spec: ArmSpec, seed: int) -> object:
        if spec.selector == "fixed_order":
            return FixedOrderDouble()
        if spec.selector == "uniform_random":
            return RandomDouble(seed=seed)
        if spec.selector == "eig":
            return EigDouble()
        from research.execution.lanes import LaneMissing

        raise LaneMissing(f"no double for selector {spec.selector!r}")

    def generator(self, spec: ArmSpec) -> None:
        if spec.generator not in (None, "builtin_plan"):
            from research.execution.lanes import LaneMissing

            raise LaneMissing(f"no double for generator {spec.generator!r}")
        return None
