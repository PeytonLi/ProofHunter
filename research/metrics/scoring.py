"""Scoring of frozen ``RawResultRow`` episodes against ``CaseLabel``s (RESEARCH-SPEC sections 7-8).

Everything is keyed by measurement mode first: offline, recorded and live rows are never pooled.
Success on a defect case is a ``confirmed_*`` verdict whose kind matches the label within k pairs.
Misses are right-censored; no detection time is invented for them.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from proofhunter.contracts import Verdict
from research.schemas.models import (
    CaseLabel,
    Confirmation,
    CostStatus,
    LabelKind,
    Outcome,
    ProtocolV1,
    RawResultRow,
)

CONFIRMED = frozenset({Verdict.CONFIRMED_REGRESSION, Verdict.CONFIRMED_EXISTING_DEFECT})
UNRESOLVED = frozenset({Verdict.INCONCLUSIVE, "failed"})
FACTORIAL = {
    ("untuned", "eig"): "qwen_untuned_eig",
    ("untuned", "ranker"): "qwen_untuned_ranker",
    ("tuned", "eig"): "qwen_tuned_eig",
    ("tuned", "ranker"): "qwen_tuned_ranker",
}
TIMING_FIELDS = (
    "queue_ms",
    "generation_ms",
    "feature_ms",
    "ranking_ms",
    "sandbox_startup_ms",
    "execution_ms",
    "confirmation_ms",
    "cleanup_ms",
)
SMALL_SAMPLE_CASES = 30
CURVE_POINTS = 24

Scores = dict[str, Any]
RowMetric = Callable[[RawResultRow], float | None]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def bootstrap_ci(
    values: Mapping[str, Sequence[float]], *, resamples: int, seed: int
) -> tuple[float, float, float]:
    """Percentile 95% cluster bootstrap of the pooled mean.

    Keys are the resampling unit (``case_id`` for case-level, ``family_id`` for grouped
    intervals); each key's values are resampled together.
    """
    import numpy as np  # research lock only; keeps the CLI importable in the production env

    keys = sorted(k for k, v in values.items() if len(v))
    if not keys:
        raise ValueError("bootstrap_ci needs at least one non-empty key")
    sums = np.array([float(sum(values[k])) for k in keys])
    counts = np.array([float(len(values[k])) for k in keys])
    estimate = float(sums.sum() / counts.sum())
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(keys), size=(resamples, len(keys)))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return estimate, float(low), float(high)


def _ci(
    case_values: Mapping[str, float],
    groups: Mapping[str, str] | None,
    protocol: ProtocolV1,
    *,
    salt: int = 0,
) -> dict[str, Any]:
    if not case_values:
        return {
            "estimate": None,
            "low": None,
            "high": None,
            "n_cases": 0,
            "n_clusters": 0,
            "unit": "family_id" if groups else "case",
        }
    keyed: dict[str, list[float]] = defaultdict(list)
    for case_id, value in case_values.items():
        keyed[groups[case_id] if groups else case_id].append(value)
    resamples = int(protocol.bootstrap["resamples"])
    estimate, low, high = bootstrap_ci(
        keyed, resamples=resamples, seed=int(protocol.bootstrap["seed"]) + salt
    )
    return {
        "estimate": estimate,
        "low": low,
        "high": high,
        "n_cases": len(case_values),
        "n_clusters": len(keyed),
        "unit": "family_id" if groups else "case",
    }


def _intervals(
    case_values: Mapping[str, float],
    families: Mapping[str, str],
    protocol: ProtocolV1,
    *,
    salt: int = 0,
) -> dict[str, Any]:
    """Headline grouped interval plus the case-level interval (spec section 8)."""
    return {
        "grouped": _ci(case_values, families, protocol, salt=salt),
        "case": _ci(case_values, None, protocol, salt=salt),
    }


# ---------------------------------------------------------------------------
# Per-row predicates
# ---------------------------------------------------------------------------


def witness_step(row: RawResultRow) -> int | None:
    """Pair index of the confirmed witness backing a ``confirmed_*`` verdict, else ``None``."""
    if row.verdict == Verdict.CONFIRMED_REGRESSION:
        return row.pairs_to_first_confirmed_witness
    if row.verdict == Verdict.CONFIRMED_EXISTING_DEFECT:
        return next((p.step for p in row.probes if p.outcome is Outcome.BASE_MISMATCH), None)
    return None


def success_within(row: RawResultRow, label: CaseLabel, budget: int) -> bool:
    if label.kind is LabelKind.CLEAN or row.verdict != label.expected_verdict:
        return False
    step = witness_step(row)
    return step is not None and step <= budget


def is_false_positive(row: RawResultRow, label: CaseLabel) -> bool:
    return label.kind is LabelKind.CLEAN and row.verdict in CONFIRMED


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (numpy's default method)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _check(
    rows: Sequence[RawResultRow], labels: Mapping[str, CaseLabel], protocol: ProtocolV1
) -> None:
    arms = {a.arm_id for a in protocol.arms}
    for row in rows:
        if row.protocol_version != protocol.schema_version:
            raise ValueError(f"{row.run_id}: protocol {row.protocol_version} != frozen protocol")
        if row.case_id not in labels:
            raise ValueError(f"{row.run_id}: case {row.case_id} has no label")
        if row.arm_id not in arms:
            raise ValueError(f"{row.run_id}: arm {row.arm_id} is not in the frozen protocol")
    keys = Counter(
        (r.measurement_mode, r.split, r.arm_id, r.case_id, r.seed, r.repetition) for r in rows
    )
    if dupes := [k for k, n in keys.items() if n > 1]:
        raise ValueError(f"duplicate episodes {dupes[:3]}")


def _families(labels: Mapping[str, CaseLabel]) -> dict[str, str]:
    """Bootstrap clusters: defect family; each clean control is its own cluster."""
    return {c: (lab.defect_family or f"clean:{c}") for c, lab in labels.items()}


def _case_means(rows: Iterable[RawResultRow], metric: RowMetric) -> dict[str, float]:
    per_case: dict[str, list[float]] = defaultdict(list)
    undefined: set[str] = set()
    for row in rows:
        value = metric(row)
        if value is None:
            undefined.add(row.case_id)
        else:
            per_case[row.case_id].append(value)
    return {c: statistics.mean(v) for c, v in per_case.items() if c not in undefined}


def _success_metric(labels: Mapping[str, CaseLabel], budget: int) -> RowMetric:
    def metric(row: RawResultRow) -> float:
        return float(success_within(row, labels[row.case_id], budget))

    return metric


def _within_resource(
    labels: Mapping[str, CaseLabel],
    budget: int,
    resource: Callable[[RawResultRow], float | None],
    threshold: float,
) -> RowMetric:
    def metric(row: RawResultRow) -> float:
        hit = success_within(row, labels[row.case_id], budget)
        return float(hit and (resource(row) or 0.0) <= threshold)

    return metric


def _fraction(n: int, d: int) -> float | None:
    return n / d if d else None


def _summarize_arm(
    rows: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    families: Mapping[str, str],
    degraded: Mapping[str, str],
) -> dict[str, Any]:
    budgets = list(protocol.budgets_reported)
    defect_rows = [r for r in rows if labels[r.case_id].kind is not LabelKind.CLEAN]
    clean_rows = [r for r in rows if labels[r.case_id].kind is LabelKind.CLEAN]
    introduced = [
        r for r in defect_rows if labels[r.case_id].kind is LabelKind.INTRODUCED_REGRESSION
    ]
    recall = {
        str(b): _intervals(
            _case_means(defect_rows, _success_metric(labels, b)), families, protocol, salt=b
        )
        for b in budgets
    }
    per_family: dict[str, dict[str, Any]] = {}
    for family in sorted({labels[r.case_id].defect_family or "" for r in defect_rows}):
        fam_rows = [r for r in defect_rows if labels[r.case_id].defect_family == family]
        values = _case_means(
            fam_rows, lambda r: float(success_within(r, labels[r.case_id], protocol.max_pairs))
        )
        per_family[family] = {
            "cases": len(values),
            f"recall_at_{protocol.max_pairs}": statistics.mean(values.values()),
        }
    findings = [r for r in rows if r.verdict in CONFIRMED]
    correct = [r for r in findings if r.verdict == labels[r.case_id].expected_verdict]
    fp_case = _case_means(clean_rows, lambda r: float(is_false_positive(r, labels[r.case_id])))
    unresolved = [r for r in rows if r.verdict in UNRESOLVED]
    mismatches = [p for r in rows for p in r.probes if p.outcome is Outcome.MISMATCH]
    reproduced = [p for p in mismatches if p.confirmation is Confirmation.REPRODUCED]
    detected_steps = [
        float(s)
        for r in defect_rows
        if success_within(r, labels[r.case_id], protocol.max_pairs)
        and (s := witness_step(r)) is not None
    ]
    walls = [r.timings.wall_ms for r in rows]
    detected_walls = [
        r.timings.wall_ms
        for r in defect_rows
        if success_within(r, labels[r.case_id], protocol.max_pairs)
    ]
    cold = [r.timings.wall_ms for r in rows if r.timings.cold_start is True]
    warm = [r.timings.wall_ms for r in rows if r.timings.cold_start is False]
    cost_rows = Counter(r.cost_status.value for r in rows)
    known_cost = sum(r.cost_usd or 0.0 for r in rows)
    unknown_cost = cost_rows.get(CostStatus.UNKNOWN.value, 0)
    estimated = [r for r in rows if r.cost_status is CostStatus.ESTIMATED]
    degraded_rows = [r for r in rows if r.run_id in degraded]
    degraded_reasons = Counter(degraded[r.run_id] for r in degraded_rows)
    cases = {r.case_id for r in rows}
    true_defects = sum(
        success_within(r, labels[r.case_id], protocol.max_pairs) for r in defect_rows
    )
    seeds = sorted({r.seed for r in rows})
    replicate = {}
    for seed in seeds:
        seed_rows = [r for r in defect_rows if r.seed == seed]
        values = _case_means(
            seed_rows, lambda r: float(success_within(r, labels[r.case_id], protocol.max_pairs))
        )
        replicate[str(seed)] = statistics.mean(values.values()) if values else None
    spread = [v for v in replicate.values() if v is not None]
    return {
        "runs": len(rows),
        "cases": len(cases),
        "defect_cases": len({r.case_id for r in defect_rows}),
        "introduced_regression_cases": len({r.case_id for r in introduced}),
        "existing_defect_cases": len(
            {r.case_id for r in defect_rows} - {r.case_id for r in introduced}
        ),
        "clean_cases": len({r.case_id for r in clean_rows}),
        "seeds": len(seeds),
        "recall": recall,
        "recall_by_family": per_family,
        "replicate_recall_at_max": replicate,
        "replicate_spread": (max(spread) - min(spread)) if spread else None,
        "findings": len(findings),
        "correct_findings": len(correct),
        "precision": _fraction(len(correct), len(findings)),
        "clean_runs": len(clean_rows),
        "clean_false_positive_runs": sum(
            is_false_positive(r, labels[r.case_id]) for r in clean_rows
        ),
        "clean_fp_rate": _ci(fp_case, None, protocol),
        "inconclusive_runs": len(unresolved),
        "inconclusive_rate": _fraction(len(unresolved), len(rows)),
        "inconclusive_by_reason": dict(
            sorted(Counter(f"{r.verdict}:{r.reason_code}" for r in unresolved).items())
        ),
        "generator_fallback_runs": sum("fallback" in r.reason_code for r in rows),
        "head_mismatches": len(mismatches),
        "confirmations_reproduced": len(reproduced),
        "confirmation_rate": _fraction(len(reproduced), len(mismatches)),
        "unusable_probes": sum(p.outcome is Outcome.UNUSABLE for r in rows for p in r.probes),
        "retries": sum(p.retries for r in rows for p in r.probes),
        "defect_runs": len(defect_rows),
        "censored_defect_runs": len(defect_rows) - len(detected_steps),
        "censored_at_pairs": protocol.max_pairs,
        "median_pairs_to_witness_detected_only": (
            statistics.median(detected_steps) if detected_steps else None
        ),
        "invocations": {
            k: sum(getattr(r.invocations, k) for r in rows)
            for k in ("base", "head", "confirm", "failed")
        },
        "probes_executed": sum(len(r.probes) for r in rows),
        "wall_ms": {"p50": percentile(walls, 0.5), "p95": percentile(walls, 0.95)},
        "time_to_first_witness_ms_detected_only": {
            "p50": percentile(detected_walls, 0.5),
            "p95": percentile(detected_walls, 0.95),
        },
        "cold_start_wall_ms_p50": percentile(cold, 0.5),
        "warm_wall_ms_p50": percentile(warm, 0.5),
        "timing_component_ms_mean": {
            f: (
                statistics.mean(v)
                if (v := [getattr(r.timings, f) for r in rows if getattr(r.timings, f) is not None])
                else None
            )
            for f in TIMING_FIELDS
        },
        "tokens": {
            k: sum(getattr(r.tokens, k) or 0 for r in rows)
            for k in ("input_tokens", "output_tokens", "reasoning_tokens")
        },
        "cost_status_runs": dict(sorted(cost_rows.items())),
        "known_cost_usd": known_cost,
        "estimated_cost_usd": sum(r.cost_usd or 0.0 for r in estimated),
        "estimated_cost_runs": len(estimated),
        "unknown_cost_runs": unknown_cost,
        "usd_per_case": (known_cost / len(cases)) if cases and not unknown_cost else None,
        "usd_per_confirmed_defect": (
            (known_cost / true_defects) if true_defects and not unknown_cost else None
        ),
        "confirmed_true_defect_runs": true_defects,
        "degraded_runs": len(degraded_rows),
        "degraded_rate": _fraction(len(degraded_rows), len(rows)),
        "degraded_by_reason": {
            reason: {"runs": n, "rate": _fraction(n, len(rows))}
            for reason, n in sorted(degraded_reasons.items())
        },
        "degraded_known_cost_usd": sum(r.cost_usd or 0.0 for r in degraded_rows),
        "degraded_confirmed_true_defect_runs": sum(
            success_within(r, labels[r.case_id], protocol.max_pairs)
            for r in degraded_rows
            if labels[r.case_id].kind is not LabelKind.CLEAN
        ),
    }


def _curve(
    rows: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    resource: Callable[[RawResultRow], float | None],
) -> list[list[float]] | None:
    """Recall vs resource threshold; ``None`` when any detected run lacks the resource value."""
    defects = [r for r in rows if labels[r.case_id].kind is not LabelKind.CLEAN]
    if not defects:
        return None
    hits = [r for r in defects if success_within(r, labels[r.case_id], protocol.max_pairs)]
    values = [resource(r) for r in hits]
    if any(v is None for v in values):
        return None
    known = sorted(v for v in values if v is not None)
    top = known[-1] if known else 0.0
    grid = sorted({0.0, *(top * i / (CURVE_POINTS - 1) for i in range(CURVE_POINTS))})
    n_cases = len({r.case_id for r in defects})
    points = []
    for t in grid:
        means = _case_means(defects, _within_resource(labels, protocol.max_pairs, resource, t))
        points.append([t, sum(means.values()) / n_cases])
    return points


def _execution_curve(
    rows: list[RawResultRow], labels: Mapping[str, CaseLabel], protocol: ProtocolV1
) -> list[list[float]] | None:
    defects = [r for r in rows if labels[r.case_id].kind is not LabelKind.CLEAN]
    if not defects:
        return None
    n_cases = len({r.case_id for r in defects})
    return [
        [float(k), sum(_case_means(defects, _success_metric(labels, k)).values()) / n_cases]
        for k in range(0, protocol.max_pairs + 1)
    ]


def _paired_values(
    a: Iterable[RawResultRow], b: Iterable[RawResultRow], metric: RowMetric
) -> tuple[dict[str, float], int]:
    """Per-case mean of (a - b) over identical (case, seed, repetition) episodes."""
    left = {(r.case_id, r.seed, r.repetition): r for r in a}
    right = {(r.case_id, r.seed, r.repetition): r for r in b}
    per_case: dict[str, list[float]] = defaultdict(list)
    shared = sorted(left.keys() & right.keys())
    for key in shared:
        x, y = metric(left[key]), metric(right[key])
        if x is not None and y is not None:
            per_case[key[0]].append(x - y)
    return {c: statistics.mean(v) for c, v in per_case.items()}, len(shared)


def paired_difference(
    a: Iterable[RawResultRow],
    b: Iterable[RawResultRow],
    protocol: ProtocolV1,
    *,
    labels: Mapping[str, CaseLabel] | None = None,
) -> Mapping[str, object]:
    """Paired a - b differences on identical (case, seed, repetition) episodes.

    Without labels, "detection" is any ``confirmed_*`` verdict within k pairs on every shared
    case. With labels, recall uses label-matched success on defect cases only, intervals are
    grouped by family, and the clean-control false-positive difference is added.
    """
    a_rows, b_rows = list(a), list(b)
    modes = {r.measurement_mode for r in a_rows + b_rows}
    if len(modes) > 1:
        raise ValueError(f"refusing to pair across measurement modes {sorted(modes)}")
    arms = ({r.arm_id for r in a_rows}, {r.arm_id for r in b_rows})
    if len(arms[0]) > 1 or len(arms[1]) > 1:
        raise ValueError("each side of a paired difference must be a single arm")
    families = _families(labels) if labels else None

    def scoped(rows: list[RawResultRow], clean: bool) -> list[RawResultRow]:
        if labels is None:
            return rows
        return [r for r in rows if (labels[r.case_id].kind is LabelKind.CLEAN) == clean]

    def detect(budget: int) -> RowMetric:
        if labels is not None:
            return lambda r: float(success_within(r, labels[r.case_id], budget))
        return lambda r: float((s := witness_step(r)) is not None and s <= budget)

    out: dict[str, object] = {
        "a": next(iter(arms[0]), None),
        "b": next(iter(arms[1]), None),
        "measurement_mode": next(iter(modes)).value if modes else None,
        "labelled": labels is not None,
    }
    for budget in protocol.budgets_reported:
        values, shared = _paired_values(
            scoped(a_rows, False), scoped(b_rows, False), detect(budget)
        )
        intervals = (
            _intervals(values, families, protocol, salt=budget)
            if families
            else {"case": _ci(values, None, protocol, salt=budget)}
        )
        out[f"recall_at_{budget}"] = {**intervals, "shared_episodes": shared}
    if labels is not None:
        values, shared = _paired_values(
            scoped(a_rows, True),
            scoped(b_rows, True),
            lambda r: float(is_false_positive(r, labels[r.case_id])),
        )
        out["clean_fp_rate"] = {"case": _ci(values, None, protocol), "shared_episodes": shared}
    values, shared = _paired_values(a_rows, b_rows, lambda r: r.cost_usd)
    out["cost_usd"] = {"case": _ci(values, None, protocol), "shared_episodes": shared}
    values, shared = _paired_values(a_rows, b_rows, lambda r: r.timings.wall_ms)
    out["wall_ms"] = {"case": _ci(values, None, protocol), "shared_episodes": shared}
    return out


def _factorial(
    rows: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    families: Mapping[str, str],
) -> dict[str, Any] | None:
    cells = {k: [r for r in rows if r.arm_id == arm] for k, arm in FACTORIAL.items()}
    if not all(cells.values()):
        return None
    out: dict[str, Any] = {}
    for budget in protocol.budgets_reported:
        per: dict[tuple[str, str], dict[tuple[str, int, int], float]] = {}
        for key, cell in cells.items():
            per[key] = {
                (r.case_id, r.seed, r.repetition): float(
                    success_within(r, labels[r.case_id], budget)
                )
                for r in cell
                if labels[r.case_id].kind is not LabelKind.CLEAN
            }
        shared = set.intersection(*(set(v) for v in per.values()))
        effects: dict[str, dict[str, list[float]]] = {
            "generator_tuning": defaultdict(list),
            "ranker_vs_eig": defaultdict(list),
            "interaction": defaultdict(list),
        }
        for ep in shared:
            ue, ur = per[("untuned", "eig")][ep], per[("untuned", "ranker")][ep]
            te, tr = per[("tuned", "eig")][ep], per[("tuned", "ranker")][ep]
            effects["generator_tuning"][ep[0]].append(((te + tr) - (ue + ur)) / 2)
            effects["ranker_vs_eig"][ep[0]].append(((ur + tr) - (ue + te)) / 2)
            effects["interaction"][ep[0]].append((tr - te) - (ur - ue))
        out[f"recall_at_{budget}"] = {
            name: _intervals(
                {c: statistics.mean(v) for c, v in vals.items()}, families, protocol, salt=budget
            )
            for name, vals in effects.items()
        } | {"shared_episodes": len(shared)}
    return out


CONTRASTS = (
    ("builtin_eig", "uniform_random"),
    ("builtin_eig", "fixed_order"),
    ("builtin_ranker", "builtin_eig"),
    ("qwen_tuned_ranker", "builtin_eig"),
    ("frontier_agent", "builtin_eig"),
    ("frontier_agent", "qwen_tuned_ranker"),
    ("frontier_reviewer", "builtin_eig"),
)


def score(
    rows: Iterable[RawResultRow],
    labels: Iterable[CaseLabel],
    protocol: ProtocolV1,
    degraded: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Per measurement mode x split x arm summaries, curves, paired contrasts and factorial.

    ``degraded`` maps run_id to the reason an arm fell back to fixed order. Degraded episodes
    stay in their arm's results (outcome and cost); they are only additionally counted.
    """
    rows = list(rows)
    degraded = dict(degraded or {})
    by_case = {lab.case_id: lab for lab in labels}
    _check(rows, by_case, protocol)
    stray = sorted(set(degraded) - {r.run_id for r in rows})
    if stray:
        raise ValueError(f"degradation records for unknown run_ids: {stray[:5]}")
    families = _families(by_case)
    order = [a.arm_id for a in protocol.arms]
    modes: dict[str, Any] = {}
    for mode in sorted({r.measurement_mode for r in rows}):
        splits: dict[str, Any] = {}
        for split in sorted({r.split for r in rows if r.measurement_mode is mode}):
            scoped = [r for r in rows if r.measurement_mode is mode and r.split is split]
            present = [a for a in order if any(r.arm_id == a for r in scoped)]
            arms = {
                a: _summarize_arm(
                    [r for r in scoped if r.arm_id == a], by_case, protocol, families, degraded
                )
                for a in present
            }
            curves = {
                a: {
                    "executions": _execution_curve(
                        [r for r in scoped if r.arm_id == a], by_case, protocol
                    ),
                    "usd": _curve(
                        [r for r in scoped if r.arm_id == a],
                        by_case,
                        protocol,
                        lambda r: r.cost_usd,
                    ),
                    "wall_ms": _curve(
                        [r for r in scoped if r.arm_id == a],
                        by_case,
                        protocol,
                        lambda r: r.timings.wall_ms,
                    ),
                }
                for a in present
            }
            paired = [
                dict(
                    paired_difference(
                        [r for r in scoped if r.arm_id == x],
                        [r for r in scoped if r.arm_id == y],
                        protocol,
                        labels=by_case,
                    )
                )
                for x, y in CONTRASTS
                if x in present and y in present
            ]
            defect_cases = len(
                {r.case_id for r in scoped if by_case[r.case_id].kind is not LabelKind.CLEAN}
            )
            splits[split.value] = {
                "arms": arms,
                "curves": curves,
                "paired": paired,
                "factorial": _factorial(scoped, by_case, protocol, families),
                "defect_cases": defect_cases,
                "descriptive_only": defect_cases < SMALL_SAMPLE_CASES,
            }
        modes[mode.value] = splits
    return {
        "schema": "research-scores-v1",
        "protocol_version": protocol.schema_version,
        "bootstrap": dict(protocol.bootstrap),
        "budgets": list(protocol.budgets_reported),
        "max_pairs": protocol.max_pairs,
        "modes": modes,
        "implementation_shas": sorted({r.implementation_sha for r in rows}),
        "degraded_records": len(degraded),
    }
