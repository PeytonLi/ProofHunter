"""Cost ledger audit: paid-call rules, estimate overruns, totals by status, row reconciliation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from research.verification.gates import GateResult

OVERRUN = 1.25


def audit_ledger(entries: Sequence[Mapping[str, Any]], policy: Mapping[str, Any], *,
                 raw_rows: Iterable[Mapping[str, Any]] = (),
                 tolerance_usd: float = 0.01) -> GateResult:
    gate = GateResult("cost_ledger")
    providers = {p["provider"] for p in policy.get("providers", [])}
    checkin = float(policy.get("single_job_checkin_usd", 50.0))
    ids: set[str] = set()
    by_status: dict[str, float] = defaultdict(float)
    unknown_entries = 0
    jobs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    run_cost: dict[str, float] = defaultdict(float)
    for e in entries:
        eid = str(e.get("entry_id"))
        if eid in ids:
            gate.error("duplicate_entry", "entry_id repeated", eid)
        ids.add(eid)
        status, cost = e.get("cost_status"), e.get("cost_usd")
        if (cost is None) != (status == "unknown"):
            gate.error("cost_status", "cost_usd null exactly when unknown", eid)
        if status == "unknown":
            unknown_entries += 1
        elif isinstance(cost, int | float):
            by_status[str(status)] += float(cost)
            by_cat[str(e.get("category"))][str(status)] += float(cost)
            if e.get("run_id"):
                run_cost[str(e["run_id"])] += float(cost)
        if e.get("unit_price_usd") is not None and status == "measured" and cost is not None:
            implied = float(e["unit_price_usd"]) * float(e.get("quantity", 0))
            if abs(implied - float(cost)) > max(tolerance_usd, 0.01 * implied):
                gate.warn("unit_price_mismatch", f"quantity*unit_price={implied:.4f} vs "
                          f"cost={cost}", eid)
        if e.get("paid"):
            if e.get("provider") not in providers:
                gate.error("unapproved_provider", f"{e.get('provider')} not in spend policy", eid)
            if not e.get("pricing_reference") or not e.get("pricing_date"):
                gate.error("pricing_missing", "paid entry lacks pricing reference/date", eid)
            est = e.get("estimated_job_usd")
            if est is None:
                gate.error("estimate_missing", "paid entry lacks pre-launch estimate", eid)
            elif float(est) > checkin and not e.get("coordinator_checkin_ref"):
                gate.error("checkin_missing", f"estimate {est} > {checkin} without check-in",
                           eid)
            job = e.get("resource_id") or e.get("run_id") or eid
            jobs[f"{e.get('provider')}:{job}"].append(e)
    for job, es in jobs.items():
        est = max((float(x["estimated_job_usd"]) for x in es
                   if x.get("estimated_job_usd") is not None), default=None)
        spent = sum(float(x["cost_usd"]) for x in es if x.get("cost_usd") is not None)
        if est is not None and est > 0 and spent > OVERRUN * est:
            gate.error("estimate_overrun", f"spent {spent:.4f} > 125% of estimate {est}", job)
        if any(x.get("cost_status") == "unknown" for x in es):
            gate.warn("job_cost_unknown", "some paid entries have unknown cost", job)
    row_cost: dict[str, float] = defaultdict(float)
    row_unknown = 0
    rows = 0
    for r in raw_rows:
        rows += 1
        if r.get("cost_status") == "unknown":
            row_unknown += 1
        elif r.get("cost_usd") is not None:
            row_cost[str(r.get("run_id"))] += float(r["cost_usd"])
    for run, cost in row_cost.items():
        if run in run_cost and abs(run_cost[run] - cost) > max(tolerance_usd, 0.01 * cost):
            gate.error("row_ledger_mismatch", f"rows {cost:.4f} vs ledger {run_cost[run]:.4f}",
                       run)
        elif cost > tolerance_usd and run not in run_cost:
            gate.warn("row_cost_unledgered", f"rows report {cost:.4f} USD with no ledger entry",
                      run)
    gate.stats.update(entries=len(ids), usd_by_status=dict(by_status),
                      unknown_cost_entries=unknown_entries,
                      usd_by_category={k: dict(v) for k, v in by_cat.items()},
                      paid_jobs=len(jobs), raw_rows=rows, raw_rows_unknown_cost=row_unknown)
    return gate
