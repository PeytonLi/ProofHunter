"""Teardown evidence: ledgered paid resources verified torn down, plus optional live listings.

Live listings are read-only GETs against provider APIs with keys from the environment
(RUNPOD_API_KEY, PROOFHUNTER_FIREWORKS_API_KEY, PROOFHUNTER_OPENAI_API_KEY). Key values are
never logged or stored; only resource ids, states and counts are recorded.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from research.verification.gates import GateResult

UA = {"User-Agent": "proofhunter-research-verification/1"}
PERSISTENT = {"runpod", "fireworks"}
FIREWORKS_ACCOUNT = "peli"
ACTIVE_RUNPOD = {"RUNNING", "CREATED", "RESTARTING", "STARTING"}


def audit_ledger_teardown(entries: Sequence[Mapping[str, Any]],
                          policy: Mapping[str, Any]) -> GateResult:
    gate = GateResult("teardown_ledger")
    guarded = {p["provider"] for p in policy.get("providers", [])
               if p.get("idle_shutdown_minutes") is not None} | PERSISTENT
    last_paid: dict[str, str] = {}
    last_verified: dict[str, str] = {}
    resources: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for e in entries:
        prov, at = str(e.get("provider")), str(e.get("recorded_at"))
        if e.get("resource_id"):
            resources[f"{prov}:{e['resource_id']}"].append(e)
        if e.get("paid"):
            last_paid[prov] = max(last_paid.get(prov, ""), at)
        if e.get("teardown_verified") is True:
            last_verified[prov] = max(last_verified.get(prov, ""), at)
    for res, es in resources.items():
        paid = any(x.get("paid") for x in es)
        if paid and not any(x.get("teardown_verified") is True for x in es):
            gate.error("resource_not_torn_down", "no entry with teardown_verified=true", res)
    for prov, at in last_paid.items():
        if prov in guarded and last_verified.get(prov, "") < at:
            gate.error("no_final_listing", f"no teardown verification after last paid entry "
                       f"{at}", prov)
    gate.stats.update(paid_providers=sorted(last_paid), resources=len(resources))
    return gate


def _get(url: str, headers: Mapping[str, str], timeout: float = 20.0) -> Any:
    resp = httpx.get(url, headers={**UA, **headers}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def list_runpod(key: str) -> dict[str, Any]:
    h = {"Authorization": f"Bearer {key}"}
    pods = _get("https://rest.runpod.io/v1/pods", h)
    vols = _get("https://rest.runpod.io/v1/networkvolumes", h)
    return {"pods": [{"id": p.get("id"), "name": p.get("name"),
                      "status": p.get("desiredStatus")} for p in pods],
            "network_volumes": [{"id": v.get("id"), "name": v.get("name")} for v in vols]}


def list_fireworks(key: str, account: str = FIREWORKS_ACCOUNT) -> dict[str, Any]:
    h = {"Authorization": f"Bearer {key}"}
    base = f"https://api.fireworks.ai/v1/accounts/{account}"
    deps = _get(f"{base}/deployments", h).get("deployments", [])
    jobs = _get(f"{base}/supervisedFineTuningJobs", h).get("supervisedFineTuningJobs", [])
    return {"deployments": [{"name": d.get("name"), "state": d.get("state")} for d in deps],
            "fine_tuning_jobs": [{"name": j.get("name"), "state": j.get("state")}
                                 for j in jobs]}


def list_openai(key: str) -> dict[str, Any]:
    batches = _get("https://api.openai.com/v1/batches?limit=100",
                   {"Authorization": f"Bearer {key}"}).get("data", [])
    return {"batches": [{"id": b.get("id"), "status": b.get("status")} for b in batches]}


def live_listing(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read-only provider listings; providers without a key are recorded as not checked."""
    env = os.environ if env is None else env
    out: dict[str, Any] = {"listed_at": datetime.now(UTC).isoformat()}
    for prov, var, fn in (("runpod", "RUNPOD_API_KEY", list_runpod),
                          ("fireworks", "PROOFHUNTER_FIREWORKS_API_KEY", list_fireworks),
                          ("openai", "PROOFHUNTER_OPENAI_API_KEY", list_openai)):
        key = env.get(var)
        if not key:
            out[prov] = {"checked": False, "reason": f"{var} not set"}
            continue
        try:
            out[prov] = {"checked": True, **fn(key)}
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            out[prov] = {"checked": False, "reason": f"{type(exc).__name__} status={status}"}
    return out


def audit_live_listing(listing: Mapping[str, Any],
                       allowed_ids: Sequence[str] = ()) -> GateResult:
    """Zero active paid resources except ids explicitly known to predate the research track."""
    gate = GateResult("teardown_live")
    allowed = set(allowed_ids)
    rp = listing.get("runpod", {})
    if rp.get("checked"):
        for p in rp.get("pods", []):
            if p.get("status") in ACTIVE_RUNPOD and p.get("id") not in allowed:
                gate.error("runpod_pod_active", f"{p.get('name')} {p.get('status')}",
                           str(p.get("id")))
            elif p.get("id") not in allowed:
                gate.warn("runpod_pod_exists", f"{p.get('name')} {p.get('status')}",
                          str(p.get("id")))
        for v in rp.get("network_volumes", []):
            if v.get("id") not in allowed:
                gate.error("runpod_volume", f"network volume {v.get('name')}", str(v.get("id")))
    fw = listing.get("fireworks", {})
    if fw.get("checked"):
        for d in fw.get("deployments", []):
            if d.get("state") not in ("DELETED", "DELETING") and d.get("name") not in allowed:
                gate.error("fireworks_deployment", str(d.get("state")), str(d.get("name")))
        for j in fw.get("fine_tuning_jobs", []):
            if j.get("state") in ("JOB_STATE_RUNNING", "JOB_STATE_CREATING",
                                  "JOB_STATE_PENDING", "JOB_STATE_VALIDATING"):
                gate.error("fireworks_job_running", str(j.get("state")), str(j.get("name")))
    oa = listing.get("openai", {})
    if oa.get("checked"):
        for b in oa.get("batches", []):
            if b.get("status") in ("validating", "in_progress", "finalizing"):
                gate.error("openai_batch_active", str(b.get("status")), str(b.get("id")))
    for prov in ("runpod", "fireworks", "openai"):
        if not listing.get(prov, {}).get("checked"):
            gate.warn("not_checked", str(listing.get(prov, {}).get("reason")), prov)
    gate.stats["listed_at"] = listing.get("listed_at")
    return gate
