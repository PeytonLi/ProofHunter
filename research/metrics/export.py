"""Machine-readable ``metrics.json``: every metric as estimate, 95% grouped-bootstrap CI and n.

Layout: mode (offline/recorded/live, never pooled) -> split (validation pilot, test) -> arm.
Proportions and ratios use a percentile cluster bootstrap of sum(numerator)/sum(denominator)
over defect families (each clean control is its own cluster), with the frozen resamples and seed.
Latency estimates are linear-interpolated percentiles; their CIs resample clusters with an
inverted-CDF weighted quantile. Ledger component costs are totals, so they carry no CI.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from research.metrics.scoring import (
    CONFIRMED,
    SMALL_SAMPLE_CASES,
    UNRESOLVED,
    _check,
    _families,
    _paired_values,
    is_false_positive,
    percentile,
    success_within,
)
from research.schemas.models import (
    CaseLabel,
    CostStatus,
    LabelKind,
    LedgerCategory,
    LedgerEntry,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
    Split,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "reporting" / "metrics.schema.json"
REFERENCES = ("builtin_eig", "simple_heuristic")
REPORTED_SPLITS = (Split.VALIDATION, Split.TEST)
CHUNK = 1000


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Metric(_Model):
    estimate: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    n_runs: int
    n_clusters: int
    ci: Literal["grouped_bootstrap_95", "none"]


class ComponentCost(_Model):
    entries: int
    measured_usd: float
    measured_entries: int
    estimated_usd: float
    estimated_entries: int
    unknown_entries: int


class StatusCost(_Model):
    runs: int
    total_usd: float
    usd_per_case: Metric
    usd_per_confirmed_defect: Metric


class UnknownCost(_Model):
    runs: int
    rate: Metric


class CostMetrics(_Model):
    measured: StatusCost
    estimated: StatusCost
    unknown: UnknownCost


class PairedMetrics(_Model):
    shared_episodes: int
    recall: dict[str, Metric]
    clean_fp_rate: Metric
    cost_usd: Metric
    wall_ms: Metric


class ArmMetrics(_Model):
    runs: int
    cases: int
    recall: dict[str, Metric]
    precision: Metric
    clean_fp_rate: Metric
    inconclusive_rate: Metric
    inconclusive_by_reason: dict[str, Metric]
    censored_misses: Metric
    censored_miss_runs: int
    latency_ms: dict[Literal["p50", "p95"], Metric]
    cost: CostMetrics
    component_costs: dict[str, ComponentCost]
    degraded_rate: Metric
    degraded_by_reason: dict[str, Metric]
    paired: dict[str, PairedMetrics]


class SplitMetrics(_Model):
    available: bool
    defect_cases: int
    clean_cases: int
    descriptive_only: bool
    arms: dict[str, ArmMetrics]


class ModeMetrics(_Model):
    component_costs: dict[str, ComponentCost]
    splits: dict[str, SplitMetrics]


class Bootstrap(_Model):
    method: Literal["percentile"]
    confidence: float
    resamples: int
    seed: int
    group_by: Literal["family_id"]


class MetricsDocument(_Model):
    schema_version: Literal["research-metrics-v1"]
    protocol_version: str
    bootstrap: Bootstrap
    budgets: list[int]
    max_pairs: int
    references: list[str]
    degraded_records: int
    modes: dict[str, ModeMetrics]


def schema_json() -> str:
    return json.dumps(MetricsDocument.model_json_schema(), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Cluster bootstrap
# ---------------------------------------------------------------------------


class _Boot:
    def __init__(self, protocol: ProtocolV1, clusters: Mapping[str, str]) -> None:
        self.resamples = int(protocol.bootstrap["resamples"])
        self.seed = int(protocol.bootstrap["seed"])
        self.clusters = clusters

    def _draws(self, k: int) -> Any:
        import numpy as np

        return np.random.default_rng(self.seed).integers(0, k, size=(self.resamples, k))

    def ratio(self, per_case: Mapping[str, tuple[float, float]], n_runs: int) -> Metric:
        """Estimate sum(num)/sum(den); CI from resampling clusters of cases."""
        import numpy as np

        num_total = sum(v[0] for v in per_case.values())
        den_total = sum(v[1] for v in per_case.values())
        keyed: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for case, (num, den) in per_case.items():
            keyed[self.clusters[case]][0] += num
            keyed[self.clusters[case]][1] += den
        empty = Metric(
            estimate=None,
            ci_low=None,
            ci_high=None,
            n=len(per_case),
            n_runs=n_runs,
            n_clusters=len(keyed),
            ci="grouped_bootstrap_95",
        )
        if not den_total:
            return empty
        keys = sorted(keyed)
        nums = np.array([keyed[k][0] for k in keys])
        dens = np.array([keyed[k][1] for k in keys])
        draws = self._draws(len(keys))
        top, bottom = nums[draws].sum(axis=1), dens[draws].sum(axis=1)
        valid = bottom > 0
        low, high = np.percentile(top[valid] / bottom[valid], [2.5, 97.5])
        return empty.model_copy(
            update={"estimate": num_total / den_total, "ci_low": float(low), "ci_high": float(high)}
        )

    def mean(self, per_case: Mapping[str, float], n_runs: int) -> Metric:
        return self.ratio({c: (v, 1.0) for c, v in per_case.items()}, n_runs)

    def quantile(self, values: Sequence[tuple[str, float]], q: float) -> Metric:
        import numpy as np

        keys = sorted({self.clusters[c] for c, _ in values})
        out = Metric(
            estimate=percentile([v for _, v in values], q),
            ci_low=None,
            ci_high=None,
            n=len({c for c, _ in values}),
            n_runs=len(values),
            n_clusters=len(keys),
            ci="grouped_bootstrap_95",
        )
        if not values:
            return out
        index = {k: i for i, k in enumerate(keys)}
        ordered = sorted(values, key=lambda item: item[1])
        vals = np.array([v for _, v in ordered])
        members = np.array([index[self.clusters[c]] for c, _ in ordered])
        draws = self._draws(len(keys))
        picks = []
        for start in range(0, self.resamples, CHUNK):
            block = draws[start : start + CHUNK]
            rows = block.shape[0]
            flat = block + np.arange(rows)[:, None] * len(keys)
            weights = np.bincount(flat.ravel(), minlength=rows * len(keys)).reshape(rows, -1)
            cum = weights[:, members].cumsum(axis=1)
            picks.append(vals[np.argmax(cum >= q * cum[:, -1:], axis=1)])
        low, high = np.percentile(np.concatenate(picks), [2.5, 97.5])
        return out.model_copy(update={"ci_low": float(low), "ci_high": float(high)})


def _total(value: float, n: int) -> Metric:
    return Metric(estimate=value, ci_low=None, ci_high=None, n=n, n_runs=n, n_clusters=0, ci="none")


# ---------------------------------------------------------------------------
# Per-arm metrics
# ---------------------------------------------------------------------------


def _by_case(rows: Iterable[RawResultRow]) -> dict[str, list[RawResultRow]]:
    out: dict[str, list[RawResultRow]] = defaultdict(list)
    for r in rows:
        out[r.case_id].append(r)
    return out


def _components(entries: Iterable[LedgerEntry]) -> dict[str, ComponentCost]:
    grouped: dict[str, list[LedgerEntry]] = {c.value: [] for c in LedgerCategory}
    for e in entries:
        grouped[e.category.value].append(e)

    def usd(items: list[LedgerEntry], status: CostStatus) -> float:
        return sum(e.cost_usd or 0.0 for e in items if e.cost_status is status)

    return {
        category: ComponentCost(
            entries=len(items),
            measured_usd=usd(items, CostStatus.MEASURED),
            measured_entries=sum(e.cost_status is CostStatus.MEASURED for e in items),
            estimated_usd=usd(items, CostStatus.ESTIMATED),
            estimated_entries=sum(e.cost_status is CostStatus.ESTIMATED for e in items),
            unknown_entries=sum(e.cost_status is CostStatus.UNKNOWN for e in items),
        )
        for category, items in grouped.items()
    }


def _status_cost(
    rows: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    boot: _Boot,
    status: CostStatus,
) -> StatusCost:
    chosen = [r for r in rows if r.cost_status is status]
    cases = _by_case(chosen)
    per_case = {c: (sum(r.cost_usd or 0.0 for r in rs), float(len(rs))) for c, rs in cases.items()}
    per_defect = {
        c: (
            sum(r.cost_usd or 0.0 for r in rs),
            float(sum(success_within(r, labels[c], protocol.max_pairs) for r in rs)),
        )
        for c, rs in cases.items()
    }
    return StatusCost(
        runs=len(chosen),
        total_usd=sum(r.cost_usd or 0.0 for r in chosen),
        usd_per_case=boot.ratio(per_case, len(chosen)),
        usd_per_confirmed_defect=boot.ratio(per_defect, len(chosen)),
    )


def _paired(
    arm_rows: list[RawResultRow],
    ref_rows: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    boot: _Boot,
) -> PairedMetrics:
    defect = [r for r in arm_rows if labels[r.case_id].kind is not LabelKind.CLEAN]
    ref_defect = [r for r in ref_rows if labels[r.case_id].kind is not LabelKind.CLEAN]
    clean = [r for r in arm_rows if labels[r.case_id].kind is LabelKind.CLEAN]
    ref_clean = [r for r in ref_rows if labels[r.case_id].kind is LabelKind.CLEAN]

    def delta(a: list[RawResultRow], b: list[RawResultRow], metric: Any) -> Metric:
        values, shared = _paired_values(a, b, metric)
        return boot.mean(values, shared)

    recall = {
        str(k): delta(
            defect,
            ref_defect,
            lambda r, k=k: float(success_within(r, labels[r.case_id], k)),
        )
        for k in protocol.budgets_reported
    }
    shared = len(
        {(r.case_id, r.seed, r.repetition) for r in arm_rows}
        & {(r.case_id, r.seed, r.repetition) for r in ref_rows}
    )
    return PairedMetrics(
        shared_episodes=shared,
        recall=recall,
        clean_fp_rate=delta(
            clean, ref_clean, lambda r: float(is_false_positive(r, labels[r.case_id]))
        ),
        cost_usd=delta(arm_rows, ref_rows, lambda r: r.cost_usd),
        wall_ms=delta(arm_rows, ref_rows, lambda r: r.timings.wall_ms),
    )


def _arm(
    rows: list[RawResultRow],
    scoped: list[RawResultRow],
    labels: Mapping[str, CaseLabel],
    protocol: ProtocolV1,
    boot: _Boot,
    degraded: Mapping[str, str],
    ledger: Sequence[LedgerEntry],
) -> ArmMetrics:
    cases = _by_case(rows)
    defect = {c: rs for c, rs in cases.items() if labels[c].kind is not LabelKind.CLEAN}
    clean = {c: rs for c, rs in cases.items() if labels[c].kind is LabelKind.CLEAN}
    n_defect_runs = sum(len(rs) for rs in defect.values())

    def rate(group: Mapping[str, list[RawResultRow]], hit: Any) -> Metric:
        per_case = {
            c: (float(sum(bool(hit(r)) for r in rs)), float(len(rs))) for c, rs in group.items()
        }
        return boot.ratio(per_case, sum(len(rs) for rs in group.values()))

    recall = {
        str(k): boot.mean(
            {
                c: statistics.mean(float(success_within(r, labels[c], k)) for r in rs)
                for c, rs in defect.items()
            },
            n_defect_runs,
        )
        for k in protocol.budgets_reported
    }
    findings = {c: [r for r in rs if r.verdict in CONFIRMED] for c, rs in cases.items()}
    precision = boot.ratio(
        {
            c: (float(sum(r.verdict == labels[c].expected_verdict for r in rs)), float(len(rs)))
            for c, rs in findings.items()
            if rs
        },
        sum(len(rs) for rs in findings.values()),
    )
    reasons = sorted({f"{r.verdict}:{r.reason_code}" for r in rows if r.verdict in UNRESOLVED})
    degraded_reasons = sorted({degraded[r.run_id] for r in rows if r.run_id in degraded})

    def censored(r: RawResultRow) -> bool:
        return not success_within(r, labels[r.case_id], protocol.max_pairs)

    walls = [(r.case_id, r.timings.wall_ms) for r in rows]
    run_ids = {r.run_id for r in rows}
    paired = {
        ref: _paired(rows, [r for r in scoped if r.arm_id == ref], labels, protocol, boot)
        for ref in REFERENCES
        if rows[0].arm_id != ref and any(r.arm_id == ref for r in scoped)
    }
    unknown = [r for r in rows if r.cost_status is CostStatus.UNKNOWN]
    return ArmMetrics(
        runs=len(rows),
        cases=len(cases),
        recall=recall,
        precision=precision,
        clean_fp_rate=rate(clean, lambda r: is_false_positive(r, labels[r.case_id])),
        inconclusive_rate=rate(cases, lambda r: r.verdict in UNRESOLVED),
        inconclusive_by_reason={
            reason: rate(cases, lambda r, reason=reason: f"{r.verdict}:{r.reason_code}" == reason)
            for reason in reasons
        },
        censored_misses=rate(defect, censored),
        censored_miss_runs=sum(censored(r) for rs in defect.values() for r in rs),
        latency_ms={"p50": boot.quantile(walls, 0.5), "p95": boot.quantile(walls, 0.95)},
        cost=CostMetrics(
            measured=_status_cost(rows, labels, protocol, boot, CostStatus.MEASURED),
            estimated=_status_cost(rows, labels, protocol, boot, CostStatus.ESTIMATED),
            unknown=UnknownCost(
                runs=len(unknown),
                rate=rate(cases, lambda r: r.cost_status is CostStatus.UNKNOWN),
            ),
        ),
        component_costs=_components(e for e in ledger if e.run_id in run_ids),
        degraded_rate=rate(cases, lambda r: r.run_id in degraded),
        degraded_by_reason={
            reason: rate(cases, lambda r, reason=reason: degraded.get(r.run_id) == reason)
            for reason in degraded_reasons
        },
        paired=paired,
    )


def metrics_document(
    rows: Iterable[RawResultRow],
    labels: Iterable[CaseLabel],
    protocol: ProtocolV1,
    *,
    degraded: Mapping[str, str] | None = None,
    ledger: Iterable[LedgerEntry] = (),
) -> MetricsDocument:
    rows = list(rows)
    degraded = dict(degraded or {})
    ledger = list(ledger)
    by_case = {lab.case_id: lab for lab in labels}
    _check(rows, by_case, protocol)
    if stray := sorted(set(degraded) - {r.run_id for r in rows}):
        raise ValueError(f"degradation records for unknown run_ids: {stray[:5]}")
    boot = _Boot(protocol, _families(by_case))
    order = [a.arm_id for a in protocol.arms]
    modes: dict[str, ModeMetrics] = {}
    for mode in MeasurementMode:
        in_mode = [r for r in rows if r.measurement_mode is mode]
        splits = list(REPORTED_SPLITS) + sorted({r.split for r in in_mode} - set(REPORTED_SPLITS))
        mode_ledger = [e for e in ledger if e.measurement_mode is mode]
        out: dict[str, SplitMetrics] = {}
        for split in splits:
            scoped = [r for r in in_mode if r.split is split]
            kinds = Counter(by_case[c].kind is LabelKind.CLEAN for c in {r.case_id for r in scoped})
            out[split.value] = SplitMetrics(
                available=bool(scoped),
                defect_cases=kinds[False],
                clean_cases=kinds[True],
                descriptive_only=kinds[False] < SMALL_SAMPLE_CASES,
                arms={
                    a: _arm(
                        [r for r in scoped if r.arm_id == a],
                        scoped,
                        by_case,
                        protocol,
                        boot,
                        degraded,
                        mode_ledger,
                    )
                    for a in order
                    if any(r.arm_id == a for r in scoped)
                },
            )
        modes[mode.value] = ModeMetrics(component_costs=_components(mode_ledger), splits=out)
    return MetricsDocument(
        schema_version="research-metrics-v1",
        protocol_version=protocol.schema_version,
        bootstrap=Bootstrap(
            method="percentile",
            confidence=0.95,
            resamples=int(protocol.bootstrap["resamples"]),
            seed=int(protocol.bootstrap["seed"]),
            group_by="family_id",
        ),
        budgets=list(protocol.budgets_reported),
        max_pairs=protocol.max_pairs,
        references=list(REFERENCES),
        degraded_records=len(degraded),
        modes=modes,
    )


def write_metrics(path: Path, document: MetricsDocument) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check metrics.schema.json.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != schema_json():
            print(f"{SCHEMA_PATH} is stale; run python -m research.metrics.export", flush=True)
            return 1
        return 0
    SCHEMA_PATH.write_text(schema_json(), encoding="utf-8")
    print(SCHEMA_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
