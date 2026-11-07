"""Scoring joins labels only after execution. Bootstrap units are cases, not runs."""

from __future__ import annotations

import csv
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bench.arms import ARMS
from bench.corpus import Case

Row = dict[str, Any]


def score(row: Row, case: Case) -> Row:
    return {**row, "case_id": case.case_id, "label": case.label,
            "template_group": case.template_group, "expected_verdict": case.expected_verdict,
            "false_positive": case.label == "clean" and not row["censored"]}


def detected(row: Row, budget: int) -> float:
    first = row["pairs_to_first_confirmed_witness"]
    return float(first is not None and first <= budget)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap(differences: list[float], *, seed: int = 2000,
              resamples: int = 2000) -> tuple[float, float, float]:
    if not differences:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311
    samples = [statistics.mean(rng.choices(differences, k=len(differences)))
               for _ in range(resamples)]
    return statistics.mean(differences), percentile(samples, .025), percentile(samples, .975)


def paired(rows: list[Row], left: str, right: str, budget: int) -> tuple[float, float, float]:
    cases: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["label"] == "defective":
            cases[row["case_id"]][row["arm"]].append(detected(row, budget))
    differences = [statistics.mean(arms[left]) - statistics.mean(arms[right])
                   for arms in cases.values() if arms[left] and arms[right]]
    return bootstrap(differences)


def summary(rows: list[Row]) -> str:
    details = []
    lines = ["# Offline benchmark (descriptive only)", "",
             "Latency is local **offline_fixture**, not cloud latency. "
             "Provider mode: recorded/offline_fixture; no live calls or measured cloud cost.", "",
             "| arm | recall@1..6 | clean FP/runs | median detected pairs | censored defects | "
             "inconclusive/runs | base/head/confirm | p50/p95 ms (offline_fixture) |",
             "|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        runs = [r for r in rows if r["arm"] == arm]
        defects = [r for r in runs if r["label"] == "defective"]
        clean = [r for r in runs if r["label"] == "clean"]
        recall = [sum(detected(r, b) for r in defects) / max(1, len(defects))
                  for b in range(1, 7)]
        hits = [float(r["pairs_to_first_confirmed_witness"]) for r in defects if not r["censored"]]
        latency = [float(r["wall_ms"]) for r in runs]
        invocations = [sum(r[f"{side}_invocations"] for r in runs)
                       for side in ("base", "head", "confirm")]
        inconclusive = [r for r in runs if r["verdict"] == "inconclusive"]
        median = f"{statistics.median(hits):.1f}" if hits else "NA"
        lines.append(f"| {arm} | {', '.join(f'{x:.3f}' for x in recall)} | "
                     f"{sum(r['false_positive'] for r in clean)}/{len(clean)} | {median} | "
                     f"{sum(r['censored'] for r in defects)}/{len(defects)} | "
                     f"{len(inconclusive)}/{len(runs)} | {'/'.join(map(str, invocations))} | "
                     f"{percentile(latency, .5):.3f}/{percentile(latency, .95):.3f} |")
        reasons = Counter(r["reason_code"] for r in inconclusive)
        fallbacks = sum(r["provider_usage"].get("fallback", 0) for r in runs)
        decisions = sum(sum(r["provider_usage"].get(m, 0) for m in ("stub", "policy", "fallback"))
                        for r in runs)
        details.append(f"\n{arm}: inconclusive reasons {dict(reasons)}; "
                     f"recorded route fallbacks {fallbacks}/{decisions}; "
                     f"stub decisions {sum(r['provider_usage'].get('stub', 0) for r in runs)}.\n")
    lines.extend(details)
    lines.extend(["", "## Paired case-bootstrap differences", "",
                  "Case means include all preregistered seeds. 2000 resamples, seed 2000; "
                  "percentile 95% intervals; defective cases only.", "",
                  "| contrast | metric | mean difference | 95% CI |", "|---|---|---|---|"])
    for left, right in (("eig_deterministic", "random"), ("eig_deterministic", "fixed_boundary"),
                        ("meta_snap_eig", "eig_deterministic")):
        for budget in (3, 6):
            mean, low, high = paired(rows, left, right, budget)
            lines.append(f"| {left} - {right} | recall@{budget} | {mean:.3f} | "
                         f"[{low:.3f}, {high:.3f}] |")
    lines.extend(["", "Misses are right-censored at six pairs, not assigned a seventh pair. "
                  "Medians condition on detection; no superiority inference. "
                  "The fixed bank knows the two rounding predictors and may miss item-count "
                  "and other unmodeled families. No bank tuning was performed."])
    return "\n".join(lines) + "\n"


def write_outputs(path: Path, rows: list[Row], adversarial: list[Row]) -> str:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    with path.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()})
    report = summary(rows)
    report += "\n## Separate adversarial infrastructure suite (not detection metrics)\n\n"
    report += "| scenario | verdict reason | observation failures |\n|---|---|---|\n"
    for row in adversarial:
        report += (f"| {row['scenario']} | {row['reason_code']} | "
                   f"{row['failures_by_reason']} |\n")
    path.with_suffix(".adversarial.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in adversarial))
    path.with_suffix(".summary.md").write_text(report)
    return report
