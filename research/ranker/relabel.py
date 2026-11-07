"""Re-featurize logged dataset observations with ``ranker-features-v1`` (no execution).

The dataset lane logs one executed candidate per decision step under ``eps0.3-heuristic-v1``
with a per-episode pool ``bayes.generate(builtin plan, pool_seed(candidate_seed, seed))`` and no
coverage guard. Each episode is replayed in step order: the state at step k holds the pool minus
the k-1 earlier picks and their recorded pair feedback, exactly what the policy saw. Features are
recomputed by ``CheckoutFeatures``; outcomes, labels and propensities are copied unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator

from proofhunter import bayes
from proofhunter import contracts as c
from research.ranker.collect import context_for
from research.ranker.features import FEATURE_SET_VERSION, CheckoutFeatures, builtin_plan
from research.schemas import interfaces as I
from research.schemas import models as M


def dataset_pool_seed(candidate_seed: int, replicate_seed: int) -> int:
    """Dataset-lane pool seed convention (research/data/execute.py ``pool_seed``)."""
    digest = hashlib.sha256(f"pool-seed-v1:{candidate_seed}:{replicate_seed}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def refeaturize(store: I.CaseStore,
                rows: Iterable[M.RankingObservation]) -> Iterator[M.RankingObservation]:
    episodes: dict[tuple[str, int], list[M.RankingObservation]] = {}
    for row in rows:
        if row.split not in (M.Split.TRAIN, M.Split.VALIDATION):
            raise PermissionError(f"row {row.observation_id} is from split {row.split}")
        episodes.setdefault((row.case_id, row.episode_seed), []).append(row)
    extractor = CheckoutFeatures()
    for (case_id, seed), logged in sorted(episodes.items()):
        logged.sort(key=lambda r: r.decision_step)
        sources = store.sources(case_id)
        bank = bayes.generate(builtin_plan(),
                              dataset_pool_seed(sources.case.candidate_seed, seed))
        pool = list(bank.candidates)
        if c.pool_sha256(pool) != logged[0].pool_sha256:
            raise ValueError(f"pool mismatch for {case_id} seed {seed}")
        ctx = context_for(sources, seed)
        ctx = I.EpisodeContext(case_id=ctx.case_id, seed=ctx.seed, base_source=ctx.base_source,
                               head_source=ctx.head_source, unified_diff=ctx.unified_diff,
                               bank=bank, plan=ctx.plan)
        by_id = {x.input_sha256: x for x in pool}
        history: list[I.PairFeedback] = []
        for row in logged:
            seen = {t for h in history if h.outcome in (M.Outcome.MATCH, M.Outcome.MISMATCH)
                    for t in by_id[h.candidate_input_sha256].coverage_tags}
            picked = {h.candidate_input_sha256 for h in history}
            state = I.EpisodeState(
                step=row.decision_step, remaining_pairs=row.remaining_pairs_before,
                available=[x for x in pool if x.input_sha256 not in picked],
                missing_coverage=[t for t in c.REQUIRED_COVERAGE_TAGS if t not in seen],
                history=tuple(history))
            cand = by_id[row.candidate.input_sha256]
            feats = dict(extractor.extract(ctx, state, cand))
            yield row.model_copy(update={"feature_set_version": FEATURE_SET_VERSION,
                                         "features": feats})
            history.append(I.PairFeedback(
                step=row.decision_step, candidate_input_sha256=cand.input_sha256,
                outcome=row.outcome, expected_cents=row.expected_cents,
                base_cents=row.base_cents, head_cents=row.head_cents))
