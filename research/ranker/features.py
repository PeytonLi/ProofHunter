"""Pre-execution feature extractor for Track A candidates (spec: FEATURES.md).

Inputs are only what every arm already receives before a pair runs: the episode context
(base/head source, diff, shared pool, plan), the episode state (step, budget, guarded pool,
missing coverage, prior pair feedback) and the candidate itself. Belief features replay the
production Bayesian model read-only over the usable history.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Final

from proofhunter import bayes
from proofhunter import contracts as c
from research.schemas.interfaces import EpisodeContext, EpisodeState, FeatureVector, PairFeedback
from research.schemas.models import FORBIDDEN_FEATURE_PREFIXES, MAX_PAIRS, Outcome

FEATURE_SET_VERSION: Final = "ranker-features-v2"
V1: Final = "ranker-features-v1"
SUPPORTED_VERSIONS: Final = (V1, FEATURE_SET_VERSION)

_STRATEGIES = tuple(c.Strategy)
_TAGS = tuple(c.CoverageTag)
_HYPOTHESES = tuple(c.HypothesisId)
_DIFF_TOKENS: Final = {
    "loop": re.compile(r"\bfor\b|\bwhile\b"),
    "round_call": re.compile(r"\bround\("),
    "floor_div": re.compile(r"//"),
    "modulo": re.compile(r"%"),
    "decimal": re.compile(r"\bDecimal\b|\bquantize\b"),
    "conditional": re.compile(r"\bif\b"),
    "comparison": re.compile(r"[<>]=?|=="),
    "int_literal": re.compile(r"\b\d+\b"),
}

GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "candidate": (*(f"strategy_{s.value}" for s in _STRATEGIES), "is_anchor",
                  *(f"tag_{t.value}" for t in _TAGS), "n_coverage_tags"),
    "shape": ("n_items", "total_cents", "log1p_total_cents", "max_price", "min_price",
              "distinct_prices", "all_equal", "discount_percent", "kept_percent",
              "total_residue", "fractional_cent", "half_cent", "line_residue_sum",
              "lines_with_fraction", "lines_at_or_above_half"),
    "predictor": ("per_item_differs", "half_even_differs", "n_predictors_differ",
                  "abs_per_item_delta", "abs_half_even_delta"),
    "diff": ("diff_added_lines", "diff_removed_lines",
             *(f"diff_added_{k}" for k in _DIFF_TOKENS),
             *(f"diff_removed_{k}" for k in _DIFF_TOKENS)),
    "context": ("step", "remaining_pairs", "budget_fraction_used", "pool_available",
                "history_matches", "history_mismatches", "history_base_mismatches",
                "history_unusable", "missing_coverage_count", "covers_missing_count"),
    "belief": (*(f"p_{h.value.lower()}" for h in _HYPOTHESES), "belief_entropy", "eig_bits",
               "p_mismatch"),
    # v2: the spec's simple_heuristic signals, its lexicographic score and its rank in the pool.
    "heuristic": ("n_distinct_predictions", "residue_dist_half", "heuristic_score",
                  "heuristic_rank", "heuristic_rank_frac", "heuristic_is_top"),
}
FEATURE_NAMES: Final[tuple[str, ...]] = tuple(n for names in GROUPS.values() for n in names)
ABLATIONS: Final[dict[str, tuple[str, ...]]] = {
    "full": (),
    "no_belief": ("belief",),
    "no_belief_no_predictor": ("belief", "predictor"),
    "no_heuristic": ("heuristic",),
    "no_diff": ("diff",),
}
assert not any(n.startswith(FORBIDDEN_FEATURE_PREFIXES) for n in FEATURE_NAMES)


def feature_names(ablation: str = "full", version: str = FEATURE_SET_VERSION) -> tuple[str, ...]:
    """Model input names; ``ranker-features-v1`` is v2 without the heuristic group."""
    if ablation not in ABLATIONS:
        raise ValueError(f"unknown ablation {ablation!r}")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported feature set {version!r}")
    dropped = {n for g in ABLATIONS[ablation] for n in GROUPS[g]}
    if version == V1:
        dropped |= set(GROUPS["heuristic"])
    return tuple(n for n in FEATURE_NAMES if n not in dropped)


def builtin_plan(payload: c.HypothesisPlanPayload | None = None) -> c.HypothesisPlan:
    hypotheses = (payload or c.builtin_hypothesis_payload()).hypotheses
    return c.HypothesisPlan(hypotheses=hypotheses, provenance=c.ProviderProvenance.model_validate(
        {"attempts": 0, "latency_ms": 0, "mode": "offline_fixture", "model_id": None,
         "provider": "meta", "reason_code": None, "request_id": None}))


def replay_belief(plan: c.HypothesisPlanPayload, history: Sequence[PairFeedback],
                  pool: Sequence[c.Candidate]) -> dict[c.HypothesisId, float]:
    """Production prior from the plan, updated on usable match/mismatch history only."""
    belief = dict(bayes.initialize(builtin_plan(plan)).belief.probabilities)
    by_hash = {x.input_sha256: x for x in pool}
    for item in history:
        candidate = by_hash.get(item.candidate_input_sha256)
        if candidate is None or item.outcome not in (Outcome.MATCH, Outcome.MISMATCH):
            continue
        q = bayes.likelihoods(belief, candidate.prices_cents, candidate.discount_percent)
        observed = (c.BinaryOutcome.MATCH if item.outcome is Outcome.MATCH
                    else c.BinaryOutcome.MISMATCH)
        belief = {h: round(v, 12) for h, v in bayes.posterior(belief, q, observed).items()}
    return belief


def diff_features(unified_diff: str) -> dict[str, float]:
    added = [ln[1:] for ln in unified_diff.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:] for ln in unified_diff.splitlines()
               if ln.startswith("-") and not ln.startswith("---")]
    out: dict[str, float] = {"diff_added_lines": len(added), "diff_removed_lines": len(removed)}
    for side, lines in (("added", added), ("removed", removed)):
        for key, pattern in _DIFF_TOKENS.items():
            out[f"diff_{side}_{key}"] = sum(len(pattern.findall(ln)) for ln in lines)
    return out


def _shape(prices: Sequence[int], discount: int) -> dict[str, float]:
    kept = 100 - discount
    total = sum(prices)
    residue = total * kept % 100
    lines = [p * kept % 100 for p in prices]
    return {
        "n_items": len(prices), "total_cents": total, "log1p_total_cents": math.log1p(total),
        "max_price": max(prices, default=0), "min_price": min(prices, default=0),
        "distinct_prices": len(set(prices)), "all_equal": float(len(set(prices)) <= 1),
        "discount_percent": discount, "kept_percent": kept, "total_residue": residue,
        "fractional_cent": float(residue != 0), "half_cent": float(residue == 50),
        "line_residue_sum": sum(lines), "lines_with_fraction": sum(r != 0 for r in lines),
        "lines_at_or_above_half": sum(r >= 50 for r in lines),
    }


def _predictor(prices: Sequence[int], discount: int) -> dict[str, float]:
    h0 = bayes.predict_h0(prices, discount)
    per_item = bayes.predict_per_item(prices, discount)
    half_even = bayes.predict_half_even(prices, discount)
    return {
        "per_item_differs": float(per_item != h0), "half_even_differs": float(half_even != h0),
        "n_predictors_differ": float((per_item != h0) + (half_even != h0)),
        "abs_per_item_delta": abs(per_item - h0), "abs_half_even_delta": abs(half_even - h0),
    }


def heuristic_key(prices: Sequence[int], discount: int,
                  input_sha256: str) -> tuple[int, int, int, str]:
    """RESEARCH-SPEC ``simple_heuristic`` order: predictor disagreement count (distinct
    predictions) desc, |fractional residue - 50| asc, basket size desc, input hash."""
    preds = {bayes.predict_h0(prices, discount), bayes.predict_per_item(prices, discount),
             bayes.predict_half_even(prices, discount)}
    residue = sum(prices) * (100 - discount) % 100
    return (-len(preds), abs(residue - 50), -len(prices), input_sha256)


def heuristic_group(items: Sequence[tuple[Sequence[int], int, str]]) -> dict[str, dict[str, float]]:
    """Heuristic features of every candidate ``(prices, discount, sha)`` in one available set."""
    keys = {sha: heuristic_key(p, d, sha) for p, d, sha in items}
    order = sorted(keys, key=lambda sha: keys[sha])
    n = len(order)
    out: dict[str, dict[str, float]] = {}
    for rank, sha in enumerate(order):
        distinct, dist, neg_size, _ = keys[sha]
        out[sha] = {
            "n_distinct_predictions": -distinct, "residue_dist_half": dist,
            "heuristic_score": -distinct * 10_000 + (50 - dist) * 100 + min(-neg_size, 99),
            "heuristic_rank": rank, "heuristic_rank_frac": rank / max(n - 1, 1),
            "heuristic_is_top": float(rank == 0)}
    return out


def compute(candidate: c.Candidate, *, state: EpisodeState,
            belief: Mapping[c.HypothesisId, float], diff: Mapping[str, float],
            heuristic: Mapping[str, float]) -> dict[str, float]:
    prices, discount = candidate.prices_cents, candidate.discount_percent
    tags = set(candidate.coverage_tags)
    missing = set(state.missing_coverage)
    outcomes = [h.outcome for h in state.history]
    values: dict[str, float] = {f"strategy_{s.value}": float(candidate.strategy is s)
                                for s in _STRATEGIES}
    values["is_anchor"] = float(candidate.is_anchor)
    values.update({f"tag_{t.value}": float(t in tags) for t in _TAGS})
    values["n_coverage_tags"] = len(tags)
    values.update(_shape(prices, discount))
    values.update(_predictor(prices, discount))
    values.update(diff)
    values.update({
        "step": state.step, "remaining_pairs": state.remaining_pairs,
        "budget_fraction_used": 1 - state.remaining_pairs / MAX_PAIRS,
        "pool_available": len(state.available),
        "history_matches": outcomes.count(Outcome.MATCH),
        "history_mismatches": outcomes.count(Outcome.MISMATCH),
        "history_base_mismatches": outcomes.count(Outcome.BASE_MISMATCH),
        "history_unusable": outcomes.count(Outcome.UNUSABLE),
        "missing_coverage_count": len(missing), "covers_missing_count": len(missing & tags),
    })
    q = bayes.likelihoods(belief, prices, discount)
    values.update({f"p_{h.value.lower()}": float(belief.get(h, 0.0)) for h in _HYPOTHESES})
    values["belief_entropy"] = bayes.entropy(belief)
    values["eig_bits"] = bayes.eig(belief, q)
    values["p_mismatch"] = bayes.p_mismatch(belief, q)
    values.update(heuristic)
    return {name: float(values[name]) for name in FEATURE_NAMES}


class CheckoutFeatures:
    """``FeatureExtractor`` implementation. Caches per-episode diff features and belief."""

    feature_set_version: str = FEATURE_SET_VERSION

    def __init__(self) -> None:
        self._diff: tuple[str, dict[str, float]] | None = None
        self._belief: tuple[tuple[object, ...], dict[c.HypothesisId, float]] | None = None
        self._heuristic: tuple[tuple[str, ...], dict[str, dict[str, float]]] | None = None

    def heuristic_for(self, state: EpisodeState) -> dict[str, dict[str, float]]:
        key = tuple(x.input_sha256 for x in state.available)
        if self._heuristic is None or self._heuristic[0] != key:
            self._heuristic = (key, heuristic_group(
                [(x.prices_cents, x.discount_percent, x.input_sha256) for x in state.available]))
        return self._heuristic[1]

    def _diff_for(self, context: EpisodeContext) -> dict[str, float]:
        if self._diff is None or self._diff[0] != context.unified_diff:
            self._diff = (context.unified_diff, diff_features(context.unified_diff))
        return self._diff[1]

    def belief_for(self, context: EpisodeContext,
                   state: EpisodeState) -> dict[c.HypothesisId, float]:
        key = (context.case_id, context.seed, context.bank.bank_sha256,
               tuple((h.candidate_input_sha256, h.outcome) for h in state.history))
        if self._belief is None or self._belief[0] != key:
            self._belief = (key, replay_belief(context.plan, state.history,
                                               context.bank.candidates))
        return self._belief[1]

    def extract(self, context: EpisodeContext, state: EpisodeState,
                candidate: c.Candidate) -> FeatureVector:
        group = self.heuristic_for(state)
        heuristic = group.get(candidate.input_sha256) or heuristic_group(
            [(candidate.prices_cents, candidate.discount_percent, candidate.input_sha256)]
        )[candidate.input_sha256]
        return compute(candidate, state=state, belief=self.belief_for(context, state),
                       diff=self._diff_for(context), heuristic=heuristic)
