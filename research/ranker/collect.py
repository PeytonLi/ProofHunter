"""Track A observation collection over reviewed template sources (train/validation only).

At every decision step the behaviour policy's whole coverage-guarded pool is executed (base,
head, and a fresh-head confirmation after a usable mismatch), so each step yields a ranking group
of executed candidates. Each row records the behaviour policy's probability of choosing it. The
chosen candidate's feedback drives the episode; it stops at the first confirmed witness or after
``MAX_PAIRS`` steps. Labels are never read.
"""

from __future__ import annotations

import difflib
import json
import random
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final, Literal

from proofhunter import bayes
from proofhunter import contracts as c
from proofhunter.oracle.rounding import rounding_half_up_once
from research.ranker.executor import CallResult, Reviewer, run_batch, verify_reviewed
from research.ranker.features import FEATURE_SET_VERSION, CheckoutFeatures, builtin_plan
from research.schemas import interfaces as I
from research.schemas import models as M

PROTOCOL: Final = Path(__file__).resolve().parents[1] / "spec" / "protocol-v1.json"
_EPS: Final = re.compile(r"^eig_eps(0\.\d+|1\.0)$")
TrainOrValidation = Literal[M.Split.TRAIN, M.Split.VALIDATION]
Runner = Callable[[bytes, Sequence[tuple[Sequence[int], int]]], tuple[list[CallResult], float]]


def replicate_seeds() -> list[int]:
    return [int(s) for s in json.loads(PROTOCOL.read_text())["replicate_seeds"]]


def epsilon(policy: str) -> float:
    """``eig_eps<e>``: epsilon-greedy over production EIG; ``uniform_random`` is eps 1."""
    if policy == "uniform_random":
        return 1.0
    match = _EPS.match(policy)
    if not match or not 0 < float(match.group(1)) <= 1:
        raise ValueError("policy must be uniform_random or eig_eps<e> with 0 < e <= 1")
    return float(match.group(1))


def guard(pool: Sequence[c.Candidate], missing: Sequence[c.CoverageTag],
          remaining: int) -> list[c.Candidate]:
    """Production coverage guard (bayes.select) applied before any arm chooses."""
    need = set(missing)
    if need and remaining <= len(need):
        covering = [x for x in pool if need.intersection(x.coverage_tags)]
        if covering:
            return covering
    return list(pool)


def context_for(sources: I.CaseSources, seed: int) -> I.EpisodeContext:
    case = sources.case
    diff = "".join(difflib.unified_diff(
        sources.base_source.decode().splitlines(keepends=True),
        sources.head_source.decode().splitlines(keepends=True), "a/checkout.py", "b/checkout.py"))
    plan = builtin_plan()
    return I.EpisodeContext(
        case_id=case.case_id, seed=seed, base_source=sources.base_source,
        head_source=sources.head_source, unified_diff=diff,
        bank=bayes.generate(plan, case.candidate_seed),
        plan=c.builtin_hypothesis_payload())


def _value(r: CallResult) -> int | None:
    return r.value if r.error is None else None


def _episode(sources: I.CaseSources, split: TrainOrValidation, seed: int, policy: str, eps: float,
             runner: Runner, extractor: CheckoutFeatures) -> Iterator[M.RankingObservation]:
    ctx = context_for(sources, seed)
    pool = list(ctx.bank.candidates)
    pool_hash = c.pool_sha256(pool)
    rng = random.Random(f"{ctx.case_id}|{seed}|{policy}")  # noqa: S311 - behaviour policy
    history: list[I.PairFeedback] = []
    chosen: set[str] = set()
    for step in range(1, M.MAX_PAIRS + 1):
        remaining = M.MAX_PAIRS - step + 1
        seen = {t for h in history if h.outcome in (M.Outcome.MATCH, M.Outcome.MISMATCH)
                for x in pool if x.input_sha256 == h.candidate_input_sha256
                for t in x.coverage_tags}
        missing = [t for t in c.REQUIRED_COVERAGE_TAGS if t not in seen]
        available = guard([x for x in pool if x.input_sha256 not in chosen], missing, remaining)
        if not available:
            return
        state = I.EpisodeState(step=step, remaining_pairs=remaining, available=available,
                               missing_coverage=missing, history=tuple(history))
        feats = {x.input_sha256: extractor.extract(ctx, state, x) for x in available}
        greedy = min(available, key=lambda x: (-round(float(feats[x.input_sha256]["eig_bits"]),
                                                      12), x.input_sha256))
        n = len(available)
        prob = {x.input_sha256: eps / n + (1 - eps if x is greedy else 0.0) for x in available}
        pick = rng.choice(available) if rng.random() < eps else greedy
        calls = [(x.prices_cents, x.discount_percent) for x in available]
        base, base_ms = runner(sources.base_source, calls)
        head, head_ms = runner(sources.head_source, calls)
        rows: dict[str, M.RankingObservation] = {}
        pending: list[int] = []
        prelim: list[tuple[M.Outcome, str | None, int, int | None, int | None]] = []
        for i, x in enumerate(available):
            expected = rounding_half_up_once(x.prices_cents, x.discount_percent)
            b, h = _value(base[i]), _value(head[i])
            reason = base[i].error or head[i].error
            if b is None or h is None:
                prelim.append((M.Outcome.UNUSABLE, reason, expected, b, h))
                continue
            outcome = (M.Outcome.BASE_MISMATCH if b != expected else
                       M.Outcome.MATCH if h == expected else M.Outcome.MISMATCH)
            if outcome is M.Outcome.MISMATCH:
                pending.append(i)
            prelim.append((outcome, None, expected, b, h))
        confirm: dict[int, CallResult] = {}
        confirm_ms = 0.0
        if pending:
            res, confirm_ms = runner(sources.head_source, [calls[i] for i in pending])
            confirm = dict(zip(pending, res, strict=True))
        per_call = (base_ms + head_ms + confirm_ms) / (2 * n + len(pending))
        for i, x in enumerate(available):
            outcome, reason, expected, b, h = prelim[i]
            usable = outcome is not M.Outcome.UNUSABLE
            if outcome is M.Outcome.MISMATCH:
                r = confirm[i]
                confirmation = (M.Confirmation.UNUSABLE if r.error is not None else
                                M.Confirmation.REPRODUCED if r.value == h else
                                M.Confirmation.NOT_REPRODUCED)
            else:
                confirmation = M.Confirmation.NOT_RUN
            relevance = (M.Relevance.POSITIVE if confirmation is M.Confirmation.REPRODUCED else
                         M.Relevance.ZERO if outcome is M.Outcome.MATCH else M.Relevance.UNKNOWN)
            calls_made = 2 + (i in confirm)
            rows[x.input_sha256] = M.RankingObservation(
                schema_version="research-ranking-observation-v1", track=M.Track.A,
                observation_id=f"{ctx.case_id}.{seed}.{policy}.s{step}.{x.input_sha256[:16]}",
                case_id=ctx.case_id, split=split, collection_policy=policy,
                episode_seed=seed, decision_step=step, remaining_pairs_before=remaining,
                pool_sha256=pool_hash, pool_size=len(pool),
                candidate=M.CheckoutCandidate(
                    kind="checkout_input", input_sha256=x.input_sha256,
                    input=M.CheckoutInput(prices_cents=list(x.prices_cents),
                                          discount_percent=x.discount_percent),
                    strategy=x.strategy, is_anchor=x.is_anchor,
                    coverage_tags=list(x.coverage_tags)),
                feature_set_version=FEATURE_SET_VERSION, features=dict(feats[x.input_sha256]),
                exploration=True, selection_probability=prob[x.input_sha256],
                outcome=outcome, unusable_reason=None if usable else (reason or "unknown"),
                expected_cents=expected if usable else None, base_cents=b if usable else None,
                head_cents=h if usable else None, confirmation=confirmation,
                relevance=relevance,
                cost=M.ExecutionCost(base_invocations=1, head_invocations=1,
                                     confirm_invocations=calls_made - 2, retries=0,
                                     wall_ms=per_call * calls_made),
                measurement_mode=M.MeasurementMode.OFFLINE)
        yield from rows.values()
        row = rows[pick.input_sha256]
        chosen.add(pick.input_sha256)
        history.append(I.PairFeedback(step=step, candidate_input_sha256=pick.input_sha256,
                                      outcome=row.outcome, expected_cents=row.expected_cents,
                                      base_cents=row.base_cents, head_cents=row.head_cents))
        if row.relevance is M.Relevance.POSITIVE:
            return


def collect_observations(store: I.CaseStore, split: M.Split, seeds: Sequence[int],
                         policy: str, *, reviewer: Reviewer = verify_reviewed,
                         runner: Runner = run_batch) -> Iterator[M.RankingObservation]:
    split = M.Split(split)
    allowed: TrainOrValidation
    if split is M.Split.TRAIN:
        allowed = M.Split.TRAIN
    elif split is M.Split.VALIDATION:
        allowed = M.Split.VALIDATION
    else:
        raise PermissionError("collection is limited to train and validation")
    yield from collect_cases(store, [c.case_id for c in store.manifest().cases
                                     if c.split is allowed], allowed, seeds, policy,
                             reviewer=reviewer, runner=runner)


def collect_cases(store: I.CaseStore, case_ids: Sequence[str], split: TrainOrValidation,
                  seeds: Sequence[int], policy: str, *, reviewer: Reviewer = verify_reviewed,
                  runner: Runner = run_batch) -> Iterator[M.RankingObservation]:
    eps = epsilon(policy)
    extractor = CheckoutFeatures()
    for case_id in case_ids:
        sources = store.sources(case_id)
        if sources.case.split is not split:
            raise PermissionError(f"{case_id} is not in split {split}")
        reviewer(sources)
        for seed in seeds:
            yield from _episode(sources, split, seed, policy, eps, runner, extractor)
