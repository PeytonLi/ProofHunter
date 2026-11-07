"""Grouped ranking data and decision-point metrics (labels: positive=1, zero=0, unknown dropped)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
import numpy.typing as npt

from research.schemas import models as M

GroupKey = tuple[str, int, str, int]
GroupBy = Literal["decision", "episode"]
Weighting = Literal["none", "ips"]
IPS_CLIP: Final = 50.0


def stable_json(row: M.RankingObservation) -> str:
    """Row JSON without the measured wall time, so data hashes are reproducible."""
    return row.model_dump_json(exclude={"cost": {"wall_ms"}})


def ips_weight(row: M.RankingObservation) -> float:
    if not row.selection_probability:
        raise ValueError(f"row {row.observation_id} has no selection probability")
    return min(1.0 / row.selection_probability, IPS_CLIP)


@dataclass(frozen=True)
class Groups:
    keys: list[GroupKey]
    sizes: list[int]
    x: npt.NDArray[np.float64]
    y: npt.NDArray[np.int32]
    ids: list[str]  # candidate input hashes (tie-break order)
    w: npt.NDArray[np.float64]  # per-row training weight (1.0 unless IPS)
    sha256: str
    rows_in: int
    rows_unknown: int


def build(rows: Iterable[M.RankingObservation], names: Sequence[str],
          feature_set_version: str, *, allowed: Sequence[M.Split],
          group_by: GroupBy = "decision", weighting: Weighting = "none") -> Groups:
    """Group labelled rows and hash the input stream.

    ``decision`` groups by (case, seed, policy, step): every candidate executed at one decision
    point (dense full-pool collection). ``episode`` groups the logged picks of one episode
    (one row per step, as in the dataset-lane log). ``ips`` weights each row by
    ``min(1/selection_probability, IPS_CLIP)`` normalised to mean 1, correcting the logging
    policy's bias toward its greedy pick.
    """
    digest = hashlib.sha256()
    buckets: dict[GroupKey, list[M.RankingObservation]] = {}
    total = unknown = 0
    for row in rows:
        if row.split not in allowed:
            raise PermissionError(f"row {row.observation_id} is from split {row.split}")
        if row.feature_set_version != feature_set_version:
            raise ValueError(f"feature set {row.feature_set_version} != {feature_set_version}")
        digest.update(stable_json(row).encode() + b"\n")
        total += 1
        if row.relevance is M.Relevance.UNKNOWN:
            unknown += 1
            continue
        step = row.decision_step if group_by == "decision" else 0
        key = (row.case_id, row.episode_seed, row.collection_policy, step)
        buckets.setdefault(key, []).append(row)
    keys, sizes, xs, ys, ids, ws = [], [], [], [], [], []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda r: r.candidate.input_sha256)
        if len(group) < 2:
            continue
        keys.append(key)
        sizes.append(len(group))
        for r in group:
            xs.append([float(r.features[n]) for n in names])
            ys.append(int(r.relevance is M.Relevance.POSITIVE))
            ids.append(r.candidate.input_sha256)
            ws.append(1.0 if weighting == "none" else ips_weight(r))
    w = np.asarray(ws, dtype=np.float64)
    if w.size:
        w = w / w.mean()
    return Groups(keys, sizes, np.asarray(xs, dtype=np.float64).reshape(-1, len(names)),
                  np.asarray(ys, dtype=np.int32), ids, w, digest.hexdigest(), total, unknown)


def evaluate(groups: Groups, scores: npt.NDArray[np.float64]) -> dict[str, float]:
    """MRR and hit@k of the first positive over groups that contain a positive."""
    ranks: list[int] = []
    start = 0
    for size in groups.sizes:
        y = groups.y[start:start + size]
        if y.any():
            s = scores[start:start + size]
            ids = groups.ids[start:start + size]
            order = sorted(range(size), key=lambda i: (-round(float(s[i]), 12), ids[i]))
            ranks.append(1 + next(k for k, i in enumerate(order) if y[i]))
        start += size
    n = len(ranks)
    if n == 0:
        return {"groups": 0.0, "mrr": 0.0, "hit_at_1": 0.0, "hit_at_3": 0.0, "hit_at_6": 0.0}
    return {"groups": float(n), "mrr": sum(1 / r for r in ranks) / n,
            **{f"hit_at_{k}": sum(r <= k for r in ranks) / n for k in (1, 3, 6)}}


def random_expected(groups: Groups) -> dict[str, float]:
    """Exact expectation of uniform-random ordering (first-positive rank distribution)."""
    vals: list[dict[str, float]] = []
    start = 0
    for size in groups.sizes:
        p = int(groups.y[start:start + size].sum())
        start += size
        if not p:
            continue
        # P(first positive at rank r) for p positives among n, drawn without replacement.
        probs, miss = [], 1.0
        for r in range(1, size - p + 2):
            hit = p / (size - r + 1)
            probs.append(miss * hit)
            miss *= 1 - hit
        vals.append({"mrr": sum(q / (r + 1) for r, q in enumerate(probs)),
                     **{f"hit_at_{k}": sum(probs[:k]) for k in (1, 3, 6)}})
    n = len(vals)
    out = {"groups": float(n)}
    for key in ("mrr", "hit_at_1", "hit_at_3", "hit_at_6"):
        out[key] = sum(v[key] for v in vals) / n if n else 0.0
    return out
