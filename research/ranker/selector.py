"""Inference adapter: a frozen LambdaRank checkpoint as a research ``Selector``."""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np

from research.ranker.features import SUPPORTED_VERSIONS, CheckoutFeatures, feature_names
from research.ranker.train import CHECKPOINT_VERSION, sha256_file
from research.schemas import interfaces as I


class RankerSelector:
    """Scores ``state.available`` (already coverage-guarded); ties break on input hash."""

    def __init__(self, booster: lgb.Booster, names: tuple[str, ...], *, arm_id: str,
                 checkpoint_sha256: str | None = None) -> None:
        self.arm_id = arm_id
        self.checkpoint_sha256 = checkpoint_sha256
        self._booster = booster
        self._names = names
        self._features = CheckoutFeatures()
        self._context: I.EpisodeContext | None = None

    def reset(self, context: I.EpisodeContext) -> None:
        self._context = context

    def scores(self, state: I.EpisodeState) -> dict[str, float]:
        if self._context is None:
            raise RuntimeError("reset() must be called before choose()")
        rows = [self._features.extract(self._context, state, x) for x in state.available]
        x = np.asarray([[float(r[n]) for n in self._names] for r in rows], dtype=np.float64)
        pred = np.asarray(self._booster.predict(x, num_threads=1), dtype=np.float64)
        return {c.input_sha256: float(p) for c, p in zip(state.available, pred, strict=True)}

    def choose(self, state: I.EpisodeState) -> str:
        if not state.available:
            raise ValueError("no available candidates")
        s = self.scores(state)
        return min(s, key=lambda h: (-round(s[h], 12), h))


def load_ranker_selector(checkpoint: Path, *, arm_id: str) -> I.Selector:
    """``checkpoint``: the trainer's checkpoint.json (sha256 returned by train) or its dir."""
    directory = checkpoint if checkpoint.is_dir() else checkpoint.parent
    meta = json.loads((directory / "checkpoint.json").read_text())
    if meta["checkpoint_version"] != CHECKPOINT_VERSION:
        raise ValueError("unsupported checkpoint version")
    if meta["feature_set_version"] not in SUPPORTED_VERSIONS:
        raise ValueError("checkpoint feature set does not match extractor")
    names = feature_names(meta["ablation"], meta["feature_set_version"])
    if list(names) != meta["feature_names"]:
        raise ValueError("checkpoint feature names do not match extractor")
    model = directory / "model.txt"
    if sha256_file(model) != meta["model_sha256"]:
        raise ValueError("model.txt hash does not match checkpoint")
    return RankerSelector(lgb.Booster(model_str=model.read_text()), names, arm_id=arm_id,
                          checkpoint_sha256=sha256_file(directory / "checkpoint.json"))
