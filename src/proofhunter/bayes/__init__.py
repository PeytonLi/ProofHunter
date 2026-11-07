"""Deterministic integer models and bounded Bayesian experiment selection."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence

from proofhunter import contracts as c

Prices = Sequence[int]
Vector = Mapping[c.HypothesisId, float]


def predict_h0(prices: Prices, discount: int) -> int:
    return (sum(prices) * (100 - discount) + 50) // 100


def predict_per_item(prices: Prices, discount: int) -> int:
    return sum((price * (100 - discount) + 50) // 100 for price in prices)


def predict_half_even(prices: Prices, discount: int) -> int:
    whole, residue = divmod(sum(prices) * (100 - discount), 100)
    return whole + int(residue > 50 or (residue == 50 and whole % 2 == 1))


PREDICTORS = {
    c.HypothesisId.H0: predict_h0,
    c.HypothesisId.PER_ITEM_ROUNDING: predict_per_item,
    c.HypothesisId.HALF_EVEN_ROUNDING: predict_half_even,
}


def predictions(ids: Iterable[c.HypothesisId], prices: Prices,
                discount: int) -> dict[c.HypothesisId, int | None]:
    return {hid: None if hid is c.HypothesisId.OTHER else PREDICTORS[hid](prices, discount)
            for hid in ids}


def likelihoods(ids: Iterable[c.HypothesisId], prices: Prices,
                discount: int) -> dict[c.HypothesisId, float]:
    expected = predict_h0(prices, discount)
    return {hid: (c.Q_H0 if hid is c.HypothesisId.H0 else
                  c.Q_OTHER if hid is c.HypothesisId.OTHER else
                  c.Q_PREDICTOR_DIFFERS if PREDICTORS[hid](prices, discount) != expected
                  else c.Q_PREDICTOR_AGREES) for hid in ids}


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ArithmeticError("non-finite Bayesian calculation")
    return value


def entropy(probabilities: Vector) -> float:
    for value in probabilities.values():
        if _finite(value) < 0:
            raise ArithmeticError("negative probability")
    return _finite(-math.fsum(p * math.log2(p) for p in probabilities.values() if p > 0))


def p_mismatch(prior: Vector, q: Vector) -> float:
    return _finite(math.fsum(prior[hid] * q[hid] for hid in prior))


def posterior(prior: Vector, q: Vector,
              outcome: c.BinaryOutcome) -> dict[c.HypothesisId, float]:
    if not isinstance(outcome, c.BinaryOutcome):
        raise ValueError("outcome must be BinaryOutcome")
    if set(prior) != set(q):
        raise ValueError("likelihood keys must match prior")
    for value in q.values():
        if not 0 < _finite(value) < 1:
            raise ArithmeticError("likelihood must be strictly interior")
    weights = {hid: _finite(p * (q[hid] if outcome is c.BinaryOutcome.MISMATCH
                               else 1 - q[hid])) for hid, p in prior.items()}
    evidence = _finite(math.fsum(weights.values()))
    if evidence <= 0 or any(w < 0 for w in weights.values()):
        raise ArithmeticError("non-positive evidence or negative mass")
    return {hid: _finite(w / evidence) for hid, w in weights.items()}


def eig(prior: Vector, q: Vector, negative_eig_tolerance: float = 1e-12) -> float:
    pm = p_mismatch(prior, q)
    value = _finite(entropy(prior) - pm * entropy(posterior(prior, q, c.BinaryOutcome.MISMATCH))
                    - (1 - pm) * entropy(posterior(prior, q, c.BinaryOutcome.MATCH)))
    if value < -negative_eig_tolerance:
        raise ArithmeticError("negative EIG beyond tolerance")
    return max(0.0, value)


def _normalize(values: Vector) -> dict[c.HypothesisId, float]:
    total = _finite(math.fsum(values.values()))
    if total <= 0:
        raise ArithmeticError("non-positive mass")
    return {hid: round(_finite(value / total), 12) for hid, value in values.items()}


def initialize(plan: c.HypothesisPlan) -> c.InitialBelief:
    source = {c.ProviderMode.OFFLINE_FIXTURE: c.PlanSource.OFFLINE_FIXTURE,
              c.ProviderMode.FALLBACK: c.PlanSource.BUILTIN_FALLBACK}.get(
                  plan.provenance.mode, c.PlanSource.META)
    proposals: dict[str, c.HypothesisProposal] = {}
    for proposed in plan.hypotheses:
        proposals.setdefault(proposed.mode.value, proposed)
    rejected = len(plan.hypotheses) - len(proposals)
    if set(proposals) <= {c.HypothesisId.OTHER.value}:
        proposals = {p.mode.value: p for p in c.builtin_hypothesis_payload().hypotheses}
        if source is not c.PlanSource.BUILTIN_FALLBACK:
            source = c.PlanSource.BUILTIN_RESTORED
    retained = []
    for hid in c.HypothesisId:
        proposal = proposals.get(hid.value)
        if proposal is None and hid not in (c.HypothesisId.H0, c.HypothesisId.OTHER):
            continue
        origin = c.HypothesisSource.MANDATORY
        if proposal is not None:
            origin = (c.HypothesisSource.META if source in
                      (c.PlanSource.META, c.PlanSource.OFFLINE_FIXTURE)
                      else c.HypothesisSource.BUILTIN)
        retained.append(c.RetainedHypothesis(
            id=hid, source=origin, rationale=proposal.rationale if proposal else None,
            suggested_strategy=proposal.suggested_strategy if proposal else None,
            constraints=proposal.constraints if proposal else None,
            raw_prior_weight=c.PRIOR_WEIGHTS_V1[hid.value]))
    prior = _normalize({h.id: h.raw_prior_weight for h in retained})
    return c.InitialBelief(
        belief=c.Belief(probabilities=prior, prior_policy=c.PRIOR_POLICY,
                        likelihood_policy=c.LIKELIHOOD_POLICY, updates=0),
        hypotheses=c.HypothesisSummary(plan_source=source, prior_policy=c.PRIOR_POLICY,
                                       retained=retained, initial_prior=prior,
                                       rejected_proposals=rejected))


def coverage_tags(prices: Prices, discount: int) -> list[c.CoverageTag]:
    tags = {c.CoverageTag.EMPTY if not prices else
            c.CoverageTag.SINGLETON if len(prices) == 1 else c.CoverageTag.MULTI_ITEM}
    for condition, tag in (
        (discount == 0, c.CoverageTag.ZERO_DISCOUNT),
        (discount == 100, c.CoverageTag.FULL_DISCOUNT),
        (sum(prices) * (100 - discount) % 100 == 50, c.CoverageTag.HALF_CENT),
        (len(prices) == c.MAX_ITEMS, c.CoverageTag.MAX_ITEMS),
        (c.MAX_PRICE_CENTS in prices, c.CoverageTag.MAX_PRICE),
    ):
        if condition:
            tags.add(tag)
    return sorted(tags)


def make_candidate(prices: Prices, discount: int, strategy: c.Strategy, *,
                   is_anchor: bool = False) -> c.Candidate:
    data = c.CandidateInput(prices_cents=list(prices), discount_percent=discount)
    return c.Candidate(**data.model_dump(), input_sha256=c.input_sha256(data),
                       strategy=strategy, is_anchor=is_anchor,
                       coverage_tags=coverage_tags(prices, discount),
                       generator_version=c.GENERATOR_VERSION)


def generate(plan: c.HypothesisPlan, seed: int) -> c.CandidateBank:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= c.MAX_SEED:
        raise ValueError("seed must be an integer in the frozen seed domain")
    chosen: dict[str, c.Candidate] = {}

    def add(prices: Prices, discount: int, strategy: c.Strategy, anchor: bool = False) -> None:
        candidate = make_candidate(prices, discount, strategy, is_anchor=anchor)
        count = sum(x.strategy is strategy and not x.is_anchor for x in chosen.values())
        if (len(chosen) < c.MAX_CANDIDATES and
                (anchor or count < c.MAX_CANDIDATES_PER_STRATEGY)):
            chosen.setdefault(candidate.input_sha256, candidate)

    for anchor_prices, discount in c.COMMON_ANCHORS:
        add(anchor_prices, discount,
            c.Strategy.CONSTRAINT if (anchor_prices, discount) == c.DEMO_SENTINEL
            else c.Strategy.BOUNDARY, True)
    boundaries = [([], 0), ([], 100), ([0], 0), ([c.MAX_PRICE_CENTS], 0),
                  ([c.MAX_PRICE_CENTS] * c.MAX_ITEMS, 0),
                  ([c.MAX_PRICE_CENTS] * c.MAX_ITEMS, 100), ([1], 50), ([1], 1),
                  ([1], 99), ([5], 10), ([50], 1), ([c.MAX_PRICE_CENTS], 99),
                  ([0] * c.MAX_ITEMS, 50), ([1, 1], 50), ([3], 50), ([7], 50),
                  ([c.MAX_PRICE_CENTS, 1], 50), ([25], 2), ([1], 100), ([9999], 50)]
    for prices, discount in boundaries:
        add(prices, discount, c.Strategy.BOUNDARY)
    constraints = [h.constraints for h in initialize(plan).hypotheses.retained
                   if h.constraints is not None]

    def rank(item: tuple[tuple[int, ...], int]) -> tuple[int, int, tuple[int, ...], int]:
        prices, discount = item
        matches = sum(
            (rule.min_items is None or len(prices) >= rule.min_items)
            and (rule.max_items is None or len(prices) <= rule.max_items)
            and (rule.discount_percent is None or discount == rule.discount_percent)
            and (rule.require_fractional_cent is None or
                 bool(sum(prices) * (100 - discount) % 100) == rule.require_fractional_cent)
            for rule in constraints)
        differs = sum(fn(prices, discount) != predict_h0(prices, discount)
                      for fn in (predict_per_item, predict_half_even))
        return -matches, -differs, prices, discount

    discounts = sorted({0, 1, 10, 25, 50, 99, 100} |
                       {r.discount_percent for r in constraints if r.discount_percent is not None})
    grid = {(tuple([price] * count), discount)
            for count in range(c.MAX_ITEMS + 1)
            for price in (0, 1, 2, 3, 5, 7, 15, 25, 50, 99, 9999, c.MAX_PRICE_CENTS)
            for discount in discounts}
    for grid_prices, discount in sorted(grid, key=rank):
        add(grid_prices, discount, c.Strategy.CONSTRAINT)
    rng = random.Random(f"{c.GENERATOR_VERSION}:seeded_fuzz:{seed}")  # noqa: S311
    for _ in range(200):
        count = rng.choice((0, 1, 1, 2, 2, 3, 4, c.MAX_ITEMS))
        prices = [rng.choice((rng.randrange(c.MAX_PRICE_CENTS + 1),
                              rng.randrange(100))) for _ in range(count)]
        add(prices, rng.randrange(c.MAX_DISCOUNT_PERCENT + 1), c.Strategy.SEEDED_FUZZ)
    candidates = list(chosen.values())
    return c.CandidateBank(generator_version=c.GENERATOR_VERSION, seed=seed,
                           candidates=candidates, bank_sha256=c.pool_sha256(candidates))


def select(belief: c.Belief, candidates: Sequence[c.Candidate],
           policy: c.SelectionPolicy) -> c.ProbeSelection:
    if not candidates or len(candidates) > c.MAX_CANDIDATES:
        raise ValueError("candidate pool size outside limits")
    if len({x.input_sha256 for x in candidates}) != len(candidates):
        raise ValueError("duplicate input hashes")
    if any(x.generator_version != c.GENERATOR_VERSION for x in candidates):
        raise ValueError("unsupported generator version")
    if (policy.likelihood_policy != c.LIKELIHOOD_POLICY or
            belief.likelihood_policy != c.LIKELIHOOD_POLICY or
            policy.selection_policy != c.SELECTION_POLICY or belief.prior_policy != c.PRIOR_POLICY):
        raise ValueError("unsupported policy version")
    if policy.remaining_pairs == 0:
        raise ValueError("pair budget exhausted")
    missing = set(policy.missing_coverage)
    pool = list(candidates)
    reason = c.SelectionReason.MAX_EIG
    if missing and policy.remaining_pairs <= len(missing):
        covering = [x for x in pool if missing.intersection(x.coverage_tags)]
        if covering:
            pool, reason = covering, c.SelectionReason.COVERAGE_GUARD
    scores = {x.input_sha256: eig(belief.probabilities,
              likelihoods(belief.probabilities, x.prices_cents, x.discount_percent),
              policy.negative_eig_tolerance) for x in pool}
    best = min(pool, key=lambda x: (
        -len(missing.intersection(x.coverage_tags))
        if reason is c.SelectionReason.COVERAGE_GUARD else 0,
        -round(scores[x.input_sha256], policy.eig_round_decimals), x.input_sha256))
    q = likelihoods(belief.probabilities, best.prices_cents, best.discount_percent)
    return c.ProbeSelection(
        candidate=best, candidate_pool_sha256=c.pool_sha256(candidates), pool_size=len(candidates),
        eig_bits=scores[best.input_sha256], p_mismatch=p_mismatch(belief.probabilities, q),
        prior=belief.probabilities, likelihood_mismatch=q,
        predictions=predictions(belief.probabilities, best.prices_cents, best.discount_percent),
        reason=reason)


def update(belief: c.Belief, selection: c.ProbeSelection, observed: c.BinaryOutcome) -> c.Belief:
    if (selection.prior != belief.probabilities or
            set(selection.likelihood_mismatch) != set(belief.probabilities) or
            set(selection.predictions) != set(belief.probabilities)):
        raise ValueError("selection does not match belief")
    if not isinstance(observed, c.BinaryOutcome):
        raise ValueError("observed must be BinaryOutcome")
    if belief.updates + 1 > c.MAX_PROBES:
        raise ValueError("update limit exceeded")
    probabilities = _normalize(posterior(belief.probabilities,
                                         selection.likelihood_mismatch, observed))
    return c.Belief(probabilities=probabilities, prior_policy=belief.prior_policy,
                    likelihood_policy=belief.likelihood_policy, updates=belief.updates + 1)
