"""Independent recomputation of headline metrics from raw rows and labels (RESEARCH-SPEC §7-8).

Success on a defect case: an introduced regression needs ``confirmed_regression`` with a
reproduced witness within k pairs; an existing defect needs ``confirmed_existing_defect`` using
at most k pairs. Any ``confirmed_*`` on a clean control is a false positive. Episode successes
are averaged per case, then over cases. Misses stay censored (no invented times).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from statistics import mean
from typing import Any

from research.verification.gates import GateResult

CONFIRMED = ("confirmed_regression", "confirmed_existing_defect")
DEFECT_KINDS = ("introduced_regression", "existing_defect")


def success_within(row: Mapping[str, Any], kind: str, k: int) -> bool:
    if kind == "introduced_regression":
        first = row.get("pairs_to_first_confirmed_witness")
        return row.get("verdict") == "confirmed_regression" and first is not None and first <= k
    if kind == "existing_defect":
        return (row.get("verdict") == "confirmed_existing_defect"
                and len(row.get("probes", [])) <= k)
    return False


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def group_bootstrap(per_case: Mapping[str, float], groups: Mapping[str, str], *,
                    resamples: int, seed: int) -> tuple[float, float, float] | None:
    """Percentile-95 bootstrap of the case mean, resampling whole groups."""
    if not per_case:
        return None
    by_group: dict[str, list[float]] = defaultdict(list)
    for cid, v in per_case.items():
        by_group[groups.get(cid, cid)].append(v)
    keys = sorted(by_group)
    rng = random.Random(seed)  # noqa: S311 - seeded resampling, not crypto
    stats = []
    for _ in range(resamples):
        vals = [v for g in (rng.choice(keys) for _ in keys) for v in by_group[g]]
        stats.append(mean(vals))
    stats.sort()
    lo = stats[int(0.025 * (resamples - 1))]
    hi = stats[int(0.975 * (resamples - 1))]
    return (mean(per_case.values()), lo, hi)


def recompute(rows: Iterable[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]],
              cases: Mapping[str, Mapping[str, Any]], protocol: Mapping[str, Any], *,
              reference_arm: str = "builtin_eig", bootstrap: bool = True) -> dict[str, Any]:
    """Flat metrics keyed ``split/mode/arm/metric``."""
    budgets = list(protocol.get("budgets_reported", [1, 3, 6]))
    boot = protocol.get("bootstrap", {})
    resamples, seed = int(boot.get("resamples", 10000)), int(boot.get("seed", 20261107))
    cells: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        cells[(str(r.get("split")), str(r.get("measurement_mode")), str(r.get("arm_id")))].append(r)
    out: dict[str, Any] = {}
    family = {cid: str(c.get("family_id")) for cid, c in cases.items()}
    per_case_store: dict[tuple[str, str, str, int], dict[str, float]] = {}
    for (split, mode, arm), rs in sorted(cells.items()):
        pre = f"{split}/{mode}/{arm}/"
        by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for r in rs:
            by_case[str(r.get("case_id"))].append(r)
        kind_of = {cid: str(labels.get(cid, {}).get("kind")) for cid in by_case}
        defect = [c for c in by_case if kind_of[c] in DEFECT_KINDS]
        clean = [c for c in by_case if kind_of[c] == "clean"]
        out[pre + "episodes"] = len(rs)
        out[pre + "defect_cases"] = len(defect)
        out[pre + "clean_cases"] = len(clean)
        for k in budgets:
            pc = {c: mean(float(success_within(r, kind_of[c], k)) for r in by_case[c])
                  for c in defect}
            per_case_store[(split, mode, arm, k)] = pc
            out[pre + f"recall@{k}"] = mean(pc.values()) if pc else None
            if bootstrap and k == max(budgets) and pc:
                ci = group_bootstrap(pc, family, resamples=resamples, seed=seed)
                out[pre + f"recall@{k}_ci95"] = list(ci[1:]) if ci else None
        confirmed = [r for r in rs if r.get("verdict") in CONFIRMED]
        true_conf = [r for r in confirmed
                     if success_within(r, kind_of[str(r["case_id"])], 10**9)]
        out[pre + "precision"] = len(true_conf) / len(confirmed) if confirmed else None
        clean_rows = [r for c in clean for r in by_case[c]]
        fp = [r for r in clean_rows if r.get("verdict") in CONFIRMED]
        out[pre + "false_positives"] = len(fp)
        out[pre + "fp_rate_clean"] = len(fp) / len(clean_rows) if clean_rows else None
        inc = [r for r in rs if r.get("verdict") in ("inconclusive", "failed")]
        out[pre + "inconclusive_rate"] = len(inc) / len(rs)
        reasons: dict[str, int] = defaultdict(int)
        for r in inc:
            reasons[str(r.get("reason_code"))] += 1
        out[pre + "inconclusive_reasons"] = dict(sorted(reasons.items()))
        mism = [p for r in rs for p in r.get("probes", []) if p.get("outcome") == "mismatch"]
        repro = [p for p in mism if p.get("confirmation") == "reproduced"]
        out[pre + "confirmation_rate"] = len(repro) / len(mism) if mism else None
        firsts = [r["pairs_to_first_confirmed_witness"] for r in rs
                  if r.get("pairs_to_first_confirmed_witness") is not None]
        out[pre + "censored"] = sum(1 for r in rs if r.get("censored"))
        out[pre + "pairs_to_first_witness_median_uncensored"] = percentile(firsts, 50)
        walls = [float(r["timings"]["wall_ms"]) for r in rs]
        out[pre + "wall_ms_p50"] = percentile(walls, 50)
        out[pre + "wall_ms_p95"] = percentile(walls, 95)
        known = [r for r in rs if r.get("cost_status") != "unknown"]
        out[pre + "cost_unknown_episodes"] = len(rs) - len(known)
        total = sum(float(r["cost_usd"]) for r in known)
        all_known = len(known) == len(rs)
        out[pre + "cost_usd_total_known"] = total
        out[pre + "usd_per_case"] = total / len(by_case) if all_known else None
        n_true = sum(1 for c in defect if any(success_within(r, kind_of[c], 10**9)
                                              for r in by_case[c]))
        out[pre + "usd_per_confirmed_defect"] = (total / n_true if all_known and n_true
                                                 else None)
        fam_hits: dict[str, list[float]] = defaultdict(list)
        for c in defect:
            fam_hits[family.get(c, "?")].append(per_case_store[(split, mode, arm,
                                                                max(budgets))][c])
        out[pre + f"family_recall@{max(budgets)}"] = {f: mean(v) for f, v in
                                                     sorted(fam_hits.items())}
    if bootstrap:
        kmax = max(budgets)
        for (split, mode, arm, k), pc in per_case_store.items():
            ref = per_case_store.get((split, mode, reference_arm, k))
            if k != kmax or arm == reference_arm or not ref:
                continue
            common = sorted(set(pc) & set(ref))
            diff = {c: pc[c] - ref[c] for c in common}
            ci = group_bootstrap(diff, family, resamples=resamples, seed=seed)
            if ci:
                out[f"{split}/{mode}/{arm}/paired_diff_vs_{reference_arm}@{k}"] = list(ci)
    return out


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out |= flatten(v, f"{prefix}/{k}" if prefix else str(k))
        return out
    return {prefix: obj}


def _close(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return bool(a == b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def compare_reported(computed: Mapping[str, Any], reported: Any, *, tol: float = 1e-6,
                     key_map: Mapping[str, str] | None = None) -> GateResult:
    """Every reported number must equal its independent recomputation."""
    gate = GateResult("metric_recomputation")
    comp = flatten(computed)
    rep = flatten(reported)
    matched = 0
    for key, value in rep.items():
        ck = (key_map or {}).get(key, key)
        if ck not in comp:
            if isinstance(value, int | float) and not isinstance(value, bool):
                gate.warn("unverified_metric", "reported metric not recomputed", key)
            continue
        matched += 1
        if not _close(comp[ck], value, tol):
            gate.error("metric_mismatch", f"reported {value!r} vs recomputed {comp[ck]!r}", key)
    headline = [k for k in comp if "/recall@" in k and not k.endswith("_ci95")]
    missing = [k for k in headline if k not in {(key_map or {}).get(r, r) for r in rep}]
    for k in missing[:50]:
        gate.warn("headline_not_reported", "recomputed recall missing from report", k)
    gate.stats.update(reported=len(rep), matched=matched, recomputed=len(comp))
    if matched == 0:
        gate.error("nothing_matched", "no reported metric could be matched; audit vacuous")
    return gate
