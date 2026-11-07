"""Simple feature baselines: one pre-execution feature (with sign) chosen on validation only."""

from __future__ import annotations

from typing import Final

import numpy as np

from research.ranker import metrics

CANDIDATES: Final = ("eig_bits", "p_mismatch", "n_predictors_differ", "fractional_cent",
                     "half_cent", "lines_with_fraction", "n_items", "covers_missing_count",
                     "discount_percent", "total_cents")


def score(groups: metrics.Groups, names: tuple[str, ...], feature: str,
          sign: float) -> np.ndarray:
    return sign * groups.x[:, names.index(feature)]


def evaluate_all(train: metrics.Groups, validation: metrics.Groups,
                 names: tuple[str, ...]) -> dict[str, object]:
    table: dict[str, dict[str, float]] = {}
    for feature in CANDIDATES:
        if feature not in names:
            continue
        for sign, tag in ((1.0, "high"), (-1.0, "low")):
            table[f"{feature}:{tag}"] = {
                "train_mrr": metrics.evaluate(train, score(train, names, feature, sign))["mrr"],
                **metrics.evaluate(validation, score(validation, names, feature, sign))}
    best = max(sorted(table), key=lambda k: table[k]["mrr"])
    return {"per_feature": table, "selected_on_validation": best,
            "builtin_eig": table.get("eig_bits:high"),
            "uniform_random": metrics.random_expected(validation)}
