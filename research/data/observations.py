"""Ranking observations for train/validation cases (dataset-lane producer).

Policy ``eps0.3-heuristic-v1``: at each of the 6 pair steps pick the heuristic-best remaining
pool candidate with probability 0.7, else a uniform remaining candidate; the per-row
``selection_probability`` is the policy probability of the chosen candidate. Three episodes per
case (first three replicate seeds), at most 18 executed rows per case. Features are computed
before executing the candidate. Only reviewed template output runs.
"""

from __future__ import annotations

import difflib
import json
import random
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from proofhunter import bayes
from proofhunter import contracts as c
from research.data import execute as X
from research.schemas import interfaces as I
from research.schemas import models as M

POLICY: Final = "eps0.3-heuristic-v1"
FEATURE_SET: Final = "dataset-features-v1"
EPSILON: Final = 0.3
EPISODES_PER_CASE: Final = 3
COVERAGE: Final = ("empty", "singleton", "multi_item", "zero_discount", "full_discount",
                   "half_cent", "max_items", "max_price")


def diff_literals(base: bytes, head: bytes) -> set[int]:
    added = [ln[1:] for ln in difflib.unified_diff(base.decode().splitlines(),
                                                   head.decode().splitlines(), lineterm="")
             if ln.startswith("+") and not ln.startswith("+++")]
    return {int(x) for ln in added for x in re.findall(r"\b\d+\b", ln)}


def features(cand: c.Candidate, prior: dict[c.HypothesisId, float], literals: set[int],
             changed_lines: int, step: int, remaining: int) -> dict[str, float | int | bool]:
    prices, d = list(cand.prices_cents), cand.discount_percent
    preds = {bayes.predict_h0(prices, d), bayes.predict_per_item(prices, d),
             bayes.predict_half_even(prices, d)}
    subtotal = sum(prices)
    tags = {str(t) for t in cand.coverage_tags}
    out: dict[str, float | int | bool] = {
        "basket_size": len(prices), "discount_percent": d, "is_anchor": cand.is_anchor,
        "strategy_boundary": cand.strategy is c.Strategy.BOUNDARY,
        "strategy_constraint": cand.strategy is c.Strategy.CONSTRAINT,
        "strategy_seeded_fuzz": cand.strategy is c.Strategy.SEEDED_FUZZ,
        "fractional_residue": subtotal * (100 - d) % 100,
        "half_cent_tie": subtotal * (100 - d) % 100 == 50,
        "predictor_disagreements": len(preds) - 1,
        "eig_bits_prior": float(bayes.eig(prior, bayes.likelihoods(prior, prices, d))),
        "subtotal_cents": subtotal, "max_price": max(prices, default=0),
        "distinct_prices": len(set(prices)), "zero_price_lines": prices.count(0),
        "diff_changed_lines": changed_lines,
        "diff_literal_eq_discount": d in literals,
        "diff_literal_eq_basket_size": len(prices) in literals,
        "diff_literal_le_subtotal": any(0 < x <= subtotal for x in literals),
        "diff_literal_eq_price": any(p in literals for p in prices),
        "step_index": step, "remaining_pool": remaining,
    }
    for tag in COVERAGE:
        out[f"cov_{tag}"] = tag in tags
    return out


def heuristic_key(cand: c.Candidate) -> tuple[int, int, int, str]:
    prices, d = list(cand.prices_cents), cand.discount_percent
    preds = {bayes.predict_h0(prices, d), bayes.predict_per_item(prices, d),
             bayes.predict_half_even(prices, d)}
    residue = sum(prices) * (100 - d) % 100
    return (-len(preds), abs(residue - 50), -len(prices), cand.input_sha256)


def _candidate(cand: c.Candidate) -> M.CheckoutCandidate:
    data = {"discount_percent": cand.discount_percent, "prices_cents": list(cand.prices_cents)}
    return M.CheckoutCandidate(kind="checkout_input", input_sha256=cand.input_sha256,
                               input=M.CheckoutInput(**data), strategy=cand.strategy,
                               is_anchor=cand.is_anchor, coverage_tags=list(cand.coverage_tags))


def collect(store: I.CaseStore, split: M.Split, seeds: Sequence[int]) -> list[M.RankingObservation]:
    if split not in (M.Split.TRAIN, M.Split.VALIDATION):
        raise ValueError("observations cover train and validation only")
    prior = bayes.initialize(X.builtin_plan()).belief.probabilities
    rows: list[M.RankingObservation] = []
    for case in store.manifest().cases:
        if case.split is not split:
            continue
        src = store.sources(case.case_id)
        base_fn = X.load_entry(src.base_source)
        head_fn = X.load_entry(src.head_source)
        literals = diff_literals(src.base_source, src.head_source)
        changed = sum(1 for ln in difflib.unified_diff(
            src.base_source.decode().splitlines(), src.head_source.decode().splitlines(),
            lineterm="") if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
        for seed in seeds[:EPISODES_PER_CASE]:
            pool = X.pool(X.pool_seed(case.candidate_seed, seed))
            pool_sha = c.pool_sha256(pool)
            remaining = list(pool)
            rng = random.Random(f"{POLICY}:{case.case_id}:{seed}")  # noqa: S311
            for step in range(1, M.MAX_PAIRS + 1):
                if not remaining:
                    break
                greedy = min(remaining, key=heuristic_key)
                explore = rng.random() < EPSILON
                chosen = rng.choice(remaining) if explore else greedy
                prob = EPSILON / len(remaining) + (1 - EPSILON) * (chosen is greedy)
                feats = features(chosen, prior, literals, changed, step, len(remaining))
                remaining.remove(chosen)
                item = (tuple(chosen.prices_cents), chosen.discount_percent)
                t0 = time.perf_counter()
                exp = X.expected(item)
                b, h = X.call(base_fn, item), X.call(head_fn, item)
                outcome = (M.Outcome.UNUSABLE if b is None or h is None else
                           M.Outcome.BASE_MISMATCH if b != exp else
                           M.Outcome.MATCH if h == exp else M.Outcome.MISMATCH)
                confirmation = M.Confirmation.NOT_RUN
                if outcome is M.Outcome.MISMATCH:
                    fresh = X.call(X.load_entry(src.head_source), item)
                    confirmation = (M.Confirmation.UNUSABLE if fresh is None else
                                    M.Confirmation.REPRODUCED if fresh == h else
                                    M.Confirmation.NOT_REPRODUCED)
                wall = (time.perf_counter() - t0) * 1000
                relevance = (M.Relevance.POSITIVE if confirmation is M.Confirmation.REPRODUCED
                             else M.Relevance.ZERO if outcome is M.Outcome.MATCH
                             else M.Relevance.UNKNOWN)
                usable = outcome is not M.Outcome.UNUSABLE
                rows.append(M.RankingObservation(
                    schema_version="research-ranking-observation-v1", track=M.Track.A,
                    observation_id=f"obs-{case.case_id}-s{seed}-{step}", case_id=case.case_id,
                    split=split, collection_policy=POLICY, episode_seed=seed,
                    decision_step=step, remaining_pairs_before=M.MAX_PAIRS + 1 - step,
                    pool_sha256=pool_sha, pool_size=len(pool), candidate=_candidate(chosen),
                    feature_set_version=FEATURE_SET, features=feats, exploration=explore,
                    selection_probability=prob, outcome=outcome,
                    unusable_reason=None if usable else "exception_or_invalid_output",
                    expected_cents=exp if usable else None, base_cents=b if usable else None,
                    head_cents=h if usable else None, confirmation=confirmation,
                    relevance=relevance,
                    cost=M.ExecutionCost(base_invocations=1, head_invocations=1,
                                         confirm_invocations=int(
                                             confirmation is not M.Confirmation.NOT_RUN),
                                         retries=0, wall_ms=wall),
                    measurement_mode=M.MeasurementMode.OFFLINE))
                if relevance is M.Relevance.POSITIVE:
                    break
    return rows


def write(rows: Sequence[M.RankingObservation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n"
                            for r in rows))
