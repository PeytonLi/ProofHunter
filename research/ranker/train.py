"""Deterministic LightGBM LambdaRank trainer; hyper-parameters chosen on validation only."""

from __future__ import annotations

import hashlib
import itertools
import json
import multiprocessing
import tempfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Final

import lightgbm as lgb
import numpy as np

from research.ranker import baselines, metrics
from research.ranker.features import FEATURE_SET_VERSION, feature_names
from research.schemas import models as M

SEED: Final = 20261107
CHECKPOINT_VERSION: Final = "ranker-checkpoint-v1"
FIXED: Final[dict[str, Any]] = {
    "objective": "lambdarank", "metric": "ndcg", "eval_at": [1, 3, 6],
    "seed": SEED, "bagging_seed": SEED, "feature_fraction_seed": SEED, "data_random_seed": SEED,
    "deterministic": True, "force_row_wise": True, "num_threads": 1, "verbosity": -1,
    "feature_fraction": 1.0, "bagging_fraction": 1.0, "bagging_freq": 0,
}
GRID: Final[dict[str, tuple[Any, ...]]] = {
    "num_leaves": (7, 15, 31), "learning_rate": (0.05, 0.1), "min_data_in_leaf": (5, 20),
    "num_boost_round": (50, 150),
}
GRID_V2: Final[dict[str, tuple[Any, ...]]] = {
    "num_leaves": (7, 15, 31, 63), "min_data_in_leaf": (20, 100),
    "learning_rate": (0.03, 0.1), "num_boost_round": (100, 300),
    "label_gain": ((0.0, 1.0), (0.0, 3.0), (0.0, 10.0)),
}
GRIDS: Final = {"v1": GRID, "v2": GRID_V2}
_DATA: dict[str, metrics.Groups] = {}


def _load(path: str) -> None:
    for split in ("train", "validation"):
        z = np.load(f"{path}/{split}.npz")
        _DATA[split] = metrics.Groups([], z["sizes"].tolist(), z["x"], z["y"],
                                      z["ids"].tolist(), z["w"], "", 0, 0)


def _trial(params: dict[str, Any]) -> dict[str, Any]:
    tr, va = _DATA["train"], _DATA["validation"]
    booster = _fit(tr, params)
    return {"params": params, "train": metrics.evaluate(tr, _predict(booster, tr.x)),
            "validation": metrics.evaluate(va, _predict(booster, va.x))}


def run_trials(tr: metrics.Groups, va: metrics.Groups, grid: dict[str, tuple[Any, ...]],
               jobs: int) -> list[dict[str, Any]]:
    """Every grid point, in grid order; parallel workers each fit single-threaded."""
    configs = [dict(zip(grid, (list(v) if isinstance(v, tuple) else v for v in values),
                        strict=True)) for values in itertools.product(*grid.values())]
    if jobs <= 1:
        _DATA.update(train=tr, validation=va)
        return [_trial(p) for p in configs]
    with tempfile.TemporaryDirectory() as tmp:
        for split, g in (("train", tr), ("validation", va)):
            np.savez(f"{tmp}/{split}.npz", x=g.x, y=g.y, w=g.w,
                     sizes=np.asarray(g.sizes), ids=np.asarray(g.ids))
        ctx = multiprocessing.get_context("forkserver")
        with ProcessPoolExecutor(jobs, mp_context=ctx, initializer=_load,
                                 initargs=(tmp,)) as pool:
            return list(pool.map(_trial, configs))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fit(groups: metrics.Groups, params: dict[str, Any]) -> lgb.Booster:
    p = {**FIXED, **{k: v for k, v in params.items() if k != "num_boost_round"}}
    data = lgb.Dataset(groups.x, label=groups.y, group=groups.sizes, weight=groups.w,
                       free_raw_data=False,
                       params={"verbosity": -1})
    return lgb.train(p, data, num_boost_round=int(params["num_boost_round"]))


def _predict(booster: lgb.Booster, x: np.ndarray) -> np.ndarray:
    return np.asarray(booster.predict(x, num_threads=1), dtype=np.float64)


class LambdaRankTrainer:
    """``RankerTrainer``: writes model.txt and checkpoint.json; returns the checkpoint hash."""

    def __init__(self, ablation: str = "full", *, group_by: metrics.GroupBy = "decision",
                 weighting: metrics.Weighting = "none",
                 grid: dict[str, tuple[Any, ...]] = GRID, jobs: int = 1) -> None:
        self.grid = grid
        self.jobs = jobs
        self.ablation = ablation
        self.group_by: metrics.GroupBy = group_by
        self.weighting: metrics.Weighting = weighting
        self.names = feature_names(ablation)
        self.report: dict[str, Any] = {}

    def train(self, train: Iterable[M.RankingObservation],
              validation: Iterable[M.RankingObservation], out_dir: Path) -> str:
        tr = metrics.build(train, self.names, FEATURE_SET_VERSION, allowed=(M.Split.TRAIN,),
                           group_by=self.group_by, weighting=self.weighting)
        va = metrics.build(validation, self.names, FEATURE_SET_VERSION,
                           allowed=(M.Split.VALIDATION,))
        if not tr.sizes or not va.sizes or not tr.y.any():
            raise ValueError("need labelled multi-candidate groups with positives in train")
        if set(k[0] for k in tr.keys) & set(k[0] for k in va.keys):
            raise ValueError("train and validation share case ids")
        trials = run_trials(tr, va, self.grid, self.jobs)
        best = max(range(len(trials)), key=lambda i: (
            round(trials[i]["validation"]["mrr"], 12),
            round(trials[i]["validation"]["hit_at_1"], 12), -i))
        params = trials[best]["params"]
        booster = _fit(tr, params)
        out_dir.mkdir(parents=True, exist_ok=True)
        model = out_dir / "model.txt"
        model.write_text(booster.model_to_string(num_iteration=-1))
        meta = {
            "checkpoint_version": CHECKPOINT_VERSION, "feature_set_version": FEATURE_SET_VERSION,
            "ablation": self.ablation, "feature_names": list(self.names),
            "train_group_by": self.group_by, "train_weighting": self.weighting,
            "ips_clip": metrics.IPS_CLIP if self.weighting == "ips" else None,
            "lightgbm_version": lgb.__version__, "fixed_params": FIXED, "params": params,
            "grid": {k: [list(v) if isinstance(v, tuple) else v for v in vs]
                     for k, vs in self.grid.items()},
            "configs_tried": len(trials),
            "model_sha256": sha256_file(model), "train_sha256": tr.sha256,
            "validation_sha256": va.sha256,
            "train_rows": tr.rows_in, "train_unknown_rows": tr.rows_unknown,
            "train_groups": len(tr.sizes), "validation_rows": va.rows_in,
            "validation_unknown_rows": va.rows_unknown, "validation_groups": len(va.sizes),
            "validation_metrics": trials[best]["validation"],
        }
        text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
        (out_dir / "checkpoint.json").write_text(text)
        importance = booster.feature_importance(importance_type="gain")
        self.report = {**meta, "trials": trials,
                       "baselines": baselines.evaluate_all(tr, va, self.names),
                       "top_gain": sorted(((self.names[i], float(g)) for i, g in
                                           enumerate(importance) if g > 0),
                                          key=lambda t: -t[1])[:15]}
        (out_dir / "report.json").write_text(json.dumps(self.report, indent=2, sort_keys=True)
                                             + "\n")
        return sha256_file(out_dir / "checkpoint.json")
