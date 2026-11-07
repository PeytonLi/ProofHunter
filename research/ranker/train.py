"""Deterministic LightGBM LambdaRank trainer; hyper-parameters chosen on validation only."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fit(groups: metrics.Groups, params: dict[str, Any]) -> lgb.Booster:
    p = {**FIXED, **{k: v for k, v in params.items() if k != "num_boost_round"}}
    data = lgb.Dataset(groups.x, label=groups.y, group=groups.sizes, free_raw_data=False,
                       params={"verbosity": -1})
    return lgb.train(p, data, num_boost_round=int(params["num_boost_round"]))


def _predict(booster: lgb.Booster, x: np.ndarray) -> np.ndarray:
    return np.asarray(booster.predict(x, num_threads=1), dtype=np.float64)


class LambdaRankTrainer:
    """``RankerTrainer``: writes model.txt and checkpoint.json; returns the checkpoint hash."""

    def __init__(self, ablation: str = "full") -> None:
        self.ablation = ablation
        self.names = feature_names(ablation)
        self.report: dict[str, Any] = {}

    def train(self, train: Iterable[M.RankingObservation],
              validation: Iterable[M.RankingObservation], out_dir: Path) -> str:
        tr = metrics.build(train, self.names, FEATURE_SET_VERSION, allowed=(M.Split.TRAIN,))
        va = metrics.build(validation, self.names, FEATURE_SET_VERSION,
                           allowed=(M.Split.VALIDATION,))
        if not tr.sizes or not va.sizes or not tr.y.any():
            raise ValueError("need labelled multi-candidate groups with positives in train")
        if set(k[0] for k in tr.keys) & set(k[0] for k in va.keys):
            raise ValueError("train and validation share case ids")
        trials = []
        for values in itertools.product(*GRID.values()):
            params = dict(zip(GRID, values, strict=True))
            booster = _fit(tr, params)
            trials.append({"params": params,
                           "train": metrics.evaluate(tr, _predict(booster, tr.x)),
                           "validation": metrics.evaluate(va, _predict(booster, va.x))})
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
            "lightgbm_version": lgb.__version__, "fixed_params": FIXED, "params": params,
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
