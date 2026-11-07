"""Deterministic and seeded Track A selectors behind ``research.schemas.interfaces.Selector``.

All selectors only read ``EpisodeContext``/``EpisodeState``; none see labels. ``builtin_eig``
wraps the production ``proofhunter.bayes.select``/``update`` read-only.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Final

from proofhunter import bayes
from proofhunter import contracts as c
from research.schemas.interfaces import EpisodeContext, EpisodeState, PairFeedback, Selector
from research.schemas.models import Outcome

FIXED_ORDER: Final = (c.Strategy.BOUNDARY, c.Strategy.CONSTRAINT, c.Strategy.SEEDED_FUZZ)
SELECTORS: Final = ("fixed_order", "uniform_random", "simple_heuristic", "eig")
#: arm_id -> selector name for arms whose selector lives in this lane (protocol-v1 ``arms``).
ARM_SELECTORS: Final = {
    "fixed_order": "fixed_order",
    "uniform_random": "uniform_random",
    "simple_heuristic": "simple_heuristic",
    "builtin_eig": "eig",
    "meta_muse_spark_eig": "eig",
    "meta_muse_wonder_eig": "eig",
    "qwen_untuned_eig": "eig",
    "qwen_tuned_eig": "eig",
}


def _require(state: EpisodeState) -> Sequence[c.Candidate]:
    if not state.available:
        raise ValueError("no available candidates")
    return state.available


def fixed_key(candidate: c.Candidate) -> tuple[bool, int, str]:
    return (not candidate.is_anchor, FIXED_ORDER.index(candidate.strategy),
            candidate.input_sha256)


def fixed_choice(available: Sequence[c.Candidate]) -> str:
    return min(available, key=fixed_key).input_sha256


class FixedOrder:
    """Anchors first, then boundary, constraint, seeded_fuzz; ties by input_sha256."""

    def __init__(self, arm_id: str = "fixed_order") -> None:
        self.arm_id = arm_id

    def reset(self, context: EpisodeContext) -> None:
        return None

    def choose(self, state: EpisodeState) -> str:
        return fixed_choice(_require(state))


class UniformRandom:
    """Uniform over ``state.available`` sorted by input_sha256; RNG reseeded per episode."""

    def __init__(self, seed: int, arm_id: str = "uniform_random") -> None:
        self.arm_id = arm_id
        self._seed = seed
        self._rng = random.Random(seed)  # noqa: S311

    def reset(self, context: EpisodeContext) -> None:
        self._rng = random.Random(f"{self._seed}:{context.case_id}:{context.seed}")  # noqa: S311

    def choose(self, state: EpisodeState) -> str:
        pool = sorted(_require(state), key=lambda x: x.input_sha256)
        return self._rng.choice(pool).input_sha256


def residue_lines(candidate: c.Candidate) -> int:
    d = candidate.discount_percent
    return sum(p * (100 - d) % 100 != 0 for p in candidate.prices_cents)


def disagreement(candidate: c.Candidate) -> int:
    prices, d = candidate.prices_cents, candidate.discount_percent
    h0 = bayes.predict_h0(prices, d)
    return sum(fn(prices, d) != h0 for fn in (bayes.predict_per_item, bayes.predict_half_even))


def heuristic_key(candidate: c.Candidate) -> tuple[int, int, int, str]:
    return (-disagreement(candidate), -residue_lines(candidate), -len(candidate.prices_cents),
            candidate.input_sha256)


class SimpleHeuristic:
    """Predictor disagreement count, then fractional-cent residue lines, then basket size."""

    def __init__(self, arm_id: str = "simple_heuristic") -> None:
        self.arm_id = arm_id

    def reset(self, context: EpisodeContext) -> None:
        return None

    def choose(self, state: EpisodeState) -> str:
        return min(_require(state), key=heuristic_key).input_sha256


def plan_from_payload(payload: c.HypothesisPlanPayload) -> c.HypothesisPlan:
    """Provenance-only wrapper so ``bayes.initialize`` keeps the payload's own hypotheses."""
    return c.HypothesisPlan(hypotheses=payload.hypotheses, provenance=c.ProviderProvenance(
        provider=c.ProviderName.META, mode=c.ProviderMode.OFFLINE_FIXTURE, model_id=None,
        request_id=None, latency_ms=None, attempts=0, reason_code=None))


class BuiltinEig:
    """Production Bayesian EIG (``proofhunter.bayes``), belief updated from pair feedback."""

    def __init__(self, arm_id: str = "builtin_eig") -> None:
        self.arm_id = arm_id
        self._belief: c.Belief | None = None
        self._pending: dict[str, c.ProbeSelection] = {}
        self._consumed = 0

    def reset(self, context: EpisodeContext) -> None:
        self._belief = bayes.initialize(plan_from_payload(context.plan)).belief
        self._pending = {}
        self._consumed = 0

    def _absorb(self, history: Sequence[PairFeedback]) -> None:
        assert self._belief is not None
        for item in history[self._consumed:]:
            selection = self._pending.pop(item.candidate_input_sha256, None)
            if selection is not None and item.outcome in (Outcome.MATCH, Outcome.MISMATCH):
                self._belief = bayes.update(self._belief, selection,
                                            c.BinaryOutcome(item.outcome.value))
        self._consumed = len(history)

    def choose(self, state: EpisodeState) -> str:
        if self._belief is None:
            raise RuntimeError("reset() must be called before choose()")
        self._absorb(state.history)
        selection = bayes.select(self._belief, list(_require(state)), c.SelectionPolicy(
            remaining_pairs=state.remaining_pairs,
            missing_coverage=list(state.missing_coverage)))
        self._pending = {selection.candidate.input_sha256: selection}
        return selection.candidate.input_sha256

    @property
    def belief(self) -> c.Belief | None:
        return self._belief


def make_selector(arm_id: str, *, seed: int) -> Selector:
    name = ARM_SELECTORS.get(arm_id, arm_id)
    if name == "fixed_order":
        return FixedOrder(arm_id)
    if name == "uniform_random":
        return UniformRandom(seed, arm_id)
    if name == "simple_heuristic":
        return SimpleHeuristic(arm_id)
    if name == "eig":
        return BuiltinEig(arm_id)
    raise ValueError(f"unknown baseline selector arm: {arm_id}")
