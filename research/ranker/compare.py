"""Validation comparison of ranker checkpoints against the spec's built-in arms.

Metric unit: one decision point of dense (full guarded pool executed) validation collection that
contains at least one reproduced mismatch; the score is the rank of the first positive. Arms
(RESEARCH-SPEC section "arms"): ``fixed_order`` (anchors, boundary, constraint, seeded_fuzz,
ties by input_sha256), ``uniform_random`` (exact expectation), ``simple_heuristic``
(predictor disagreements desc, |fractional residue - 50| asc, basket size desc, input_sha256),
``builtin_eig`` (production EIG), and each checkpoint. Paired case-level bootstrap CIs compare
the primary checkpoint against every other arm. Run on train/validation only.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import numpy.typing as npt

from proofhunter import bayes
from proofhunter import contracts as c
from research.ranker import metrics
from research.ranker.features import FEATURE_SET_VERSION, feature_names
from research.ranker.train import SEED, sha256_file
from research.schemas import models as M

STRATEGY_ORDER = {c.Strategy.BOUNDARY: 0, c.Strategy.CONSTRAINT: 1, c.Strategy.SEEDED_FUZZ: 2}
BOOTSTRAP = 2000


def aligned_candidates(rows: Sequence[M.RankingObservation],
                       groups: metrics.Groups) -> list[M.CheckoutCandidate]:
    """Candidates in ``groups`` row order (mirrors ``metrics.build`` decision grouping)."""
    buckets: dict[metrics.GroupKey, list[M.RankingObservation]] = {}
    for r in rows:
        if r.relevance is not M.Relevance.UNKNOWN:
            key = (r.case_id, r.episode_seed, r.collection_policy, r.decision_step)
            buckets.setdefault(key, []).append(r)
    out = [r.candidate for k in groups.keys
           for r in sorted(buckets[k], key=lambda r: r.candidate.input_sha256)]
    if [x.input_sha256 for x in out] != groups.ids:
        raise ValueError("candidate alignment failed")
    return out


def _rank_scores(keys: list[tuple[Any, ...]], sizes: list[int]) -> npt.NDArray[np.float64]:
    out = np.zeros(len(keys))
    start = 0
    for size in sizes:
        order = sorted(range(start, start + size), key=lambda i: keys[i])
        for rank, i in enumerate(order):
            out[i] = -rank
        start += size
    return out


def fixed_order_key(x: M.CheckoutCandidate) -> tuple[Any, ...]:
    return (not x.is_anchor, STRATEGY_ORDER[c.Strategy(x.strategy)], x.input_sha256)


def heuristic_key(x: M.CheckoutCandidate) -> tuple[Any, ...]:
    prices, d = list(x.input.prices_cents), x.input.discount_percent
    preds = {bayes.predict_h0(prices, d), bayes.predict_per_item(prices, d),
             bayes.predict_half_even(prices, d)}
    residue = sum(prices) * (100 - d) % 100
    return (-len(preds), abs(residue - 50), -len(prices), x.input_sha256)


def reciprocal_ranks(groups: metrics.Groups,
                     scores: npt.NDArray[np.float64]) -> dict[str, float]:
    """Per-group reciprocal rank keyed by ``case|seed|step`` (groups with a positive)."""
    out: dict[str, float] = {}
    start = 0
    for key, size in zip(groups.keys, groups.sizes, strict=True):
        y = groups.y[start:start + size]
        if y.any():
            s, ids = scores[start:start + size], groups.ids[start:start + size]
            order = sorted(range(size), key=lambda i: (-round(float(s[i]), 12), ids[i]))
            out[f"{key[0]}|{key[1]}|{key[3]}"] = 1 / (1 + next(
                k for k, i in enumerate(order) if y[i]))
        start += size
    return out


def random_rr(groups: metrics.Groups) -> dict[str, float]:
    out: dict[str, float] = {}
    start = 0
    for key, size in zip(groups.keys, groups.sizes, strict=True):
        p = int(groups.y[start:start + size].sum())
        start += size
        if p:
            miss, rr = 1.0, 0.0
            for r in range(1, size - p + 2):
                hit = p / (size - r + 1)
                rr += miss * hit / r
                miss *= 1 - hit
            out[f"{key[0]}|{key[1]}|{key[3]}"] = rr
    return out


def paired_ci(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Mean MRR difference a-b with a case-clustered 95% bootstrap interval."""
    cases: dict[str, list[float]] = {}
    for k in a:
        cases.setdefault(k.split("|")[0], []).append(a[k] - b[k])
    names = sorted(cases)
    rng = random.Random(SEED)  # noqa: S311 - reproducible bootstrap
    sums = {n: (sum(v), len(v)) for n, v in cases.items()}
    stats = []
    for _ in range(BOOTSTRAP):
        pick = [names[rng.randrange(len(names))] for _ in names]
        tot, cnt = sum(sums[n][0] for n in pick), sum(sums[n][1] for n in pick)
        stats.append(tot / cnt)
    stats.sort()
    diff = sum(a[k] - b[k] for k in a) / len(a)
    return {"mrr_diff": diff, "ci95_low": stats[int(0.025 * BOOTSTRAP)],
            "ci95_high": stats[int(0.975 * BOOTSTRAP) - 1], "cases": float(len(names))}


def summarize(rr: dict[str, float]) -> dict[str, float]:
    v = list(rr.values())
    return {"groups": float(len(v)), "mrr": sum(v) / len(v),
            "hit_at_1": sum(x == 1.0 for x in v) / len(v)}


def compare(validation: Sequence[M.RankingObservation], checkpoints: dict[str, Path],
            primary: str) -> dict[str, Any]:
    full = feature_names("full")
    groups = metrics.build(validation, full, FEATURE_SET_VERSION,
                           allowed=(M.Split.VALIDATION,))
    cands = aligned_candidates(validation, groups)
    rrs: dict[str, dict[str, float]] = {
        "fixed_order": reciprocal_ranks(groups, _rank_scores(
            [fixed_order_key(x) for x in cands], groups.sizes)),
        "uniform_random": random_rr(groups),
        "simple_heuristic": reciprocal_ranks(groups, _rank_scores(
            [heuristic_key(x) for x in cands], groups.sizes)),
        "builtin_eig": reciprocal_ranks(groups, groups.x[:, full.index("eig_bits")]),
    }
    meta: dict[str, Any] = {}
    for name, ckpt in checkpoints.items():
        info = json.loads((ckpt / "checkpoint.json").read_text())
        booster = lgb.Booster(model_file=str(ckpt / "model.txt"))
        cols = [full.index(n) for n in info["feature_names"]]
        scores = np.asarray(booster.predict(groups.x[:, cols], num_threads=1), dtype=np.float64)
        rrs[name] = reciprocal_ranks(groups, scores)
        meta[name] = {"checkpoint_sha256": sha256_file(ckpt / "checkpoint.json"),
                      "ablation": info["ablation"], "train_group_by": info.get("train_group_by"),
                      "train_weighting": info.get("train_weighting"), "params": info["params"]}
    return {"validation_sha256": groups.sha256, "validation_rows": groups.rows_in,
            "validation_unknown_rows": groups.rows_unknown,
            "arms": {**{k: summarize(v) for k, v in rrs.items()},
                     "uniform_random": metrics.random_expected(groups)},
            "checkpoints": meta,
            "primary": primary,
            "paired_vs_primary": {k: paired_ci(rrs[primary], v) for k, v in rrs.items()
                                  if k != primary}}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m research.ranker.compare")
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--checkpoint", action="append", default=[], help="name=dir")
    ap.add_argument("--primary", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    from research.ranker.commands import read_rows
    from research.ranker.relabel import upgrade

    ckpts = dict(item.split("=", 1) for item in a.checkpoint)
    rows = upgrade(read_rows(a.validation))
    report = compare(rows, {k: Path(v) for k, v in ckpts.items()}, a.primary)
    a.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["arms"], indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
