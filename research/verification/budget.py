"""Budget, fairness and verdict-consistency audit over ``RawResultRow`` episodes.

Re-derives the frozen episode rules (RESEARCH-SPEC §3.3/§3.4) from raw probes: at most six
distinct paired probes, one fresh-head confirmation only after a usable mismatch, at most two
retries, at most 15 invocations, identical pools across shared-pool arms, preregistered seeds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from research.verification.gates import GateResult

CONFIRMED = ("confirmed_regression", "confirmed_existing_defect")


def audit_raw_results(rows: Iterable[Mapping[str, Any]], protocol: Mapping[str, Any], *,
                      cases: Mapping[str, Mapping[str, Any]] | None = None,
                      require_complete: bool = False) -> GateResult:
    gate = GateResult("budget_fairness")
    max_pairs = int(protocol["max_pairs"])
    max_conf = int(protocol["max_confirmations"])
    max_retry = int(protocol["max_retries_per_episode"])
    max_inv = int(protocol["max_invocations_per_episode"])
    arms = {a["arm_id"]: a for a in protocol["arms"]}
    seeds = set(protocol["replicate_seeds"])
    keys: set[tuple[Any, ...]] = set()
    pools: dict[tuple[Any, ...], set[Any]] = defaultdict(set)
    model_refs: dict[tuple[str, str], set[str]] = defaultdict(set)
    impl: set[str] = set()
    coverage: dict[tuple[str, str], set[tuple[str, int, int]]] = defaultdict(set)
    totals: dict[str, int] = defaultdict(int)
    n = 0
    for row in rows:
        n += 1
        subj = f"{row.get('arm_id')}/{row.get('case_id')}/{row.get('seed')}/{row.get('repetition')}"
        key = (row.get("measurement_mode"), row.get("arm_id"), row.get("case_id"),
               row.get("seed"), row.get("repetition"))
        if key in keys:
            gate.error("duplicate_episode", "episode recorded twice", subj)
        keys.add(key)
        impl.add(str(row.get("implementation_sha")))
        arm = arms.get(str(row.get("arm_id")))
        if arm is None:
            gate.error("unknown_arm", "arm not in frozen protocol", subj)
        if row.get("seed") not in seeds:
            gate.error("unregistered_seed", f"seed {row.get('seed')}", subj)
        if row.get("snap_routing") != "disabled":
            gate.error("snap_enabled", "Snap routing must be disabled", subj)
        if cases is not None:
            case = cases.get(str(row.get("case_id")))
            if case is None:
                gate.error("unknown_case", "case not in manifest", subj)
            elif case.get("split") != row.get("split"):
                gate.error("split_mismatch", f"{row.get('split')} vs {case.get('split')}", subj)
        coverage[(str(row.get("measurement_mode")), str(row.get("split")))].add(
            (str(row.get("arm_id")), int(row.get("seed", -1)), int(row.get("repetition", -1))))
        probes = list(row.get("probes", []))
        inv = row.get("invocations", {})
        steps = [p.get("step") for p in probes]
        if steps != list(range(1, len(probes) + 1)):
            gate.error("probe_steps", f"steps {steps}", subj)
        if len(probes) > max_pairs:
            gate.error("too_many_pairs", f"{len(probes)} > {max_pairs}", subj)
        hashes = [p.get("candidate_input_sha256") for p in probes]
        if len(set(hashes)) != len(hashes):
            gate.error("repeated_candidate", "paired probes must be distinct inputs", subj)
        retries = sum(int(p.get("retries", 0)) for p in probes)
        if retries > max_retry:
            gate.error("too_many_retries", f"{retries} > {max_retry}", subj)
        confirmations = [p for p in probes if p.get("confirmation") != "not_run"]
        for p in confirmations:
            if p.get("outcome") != "mismatch":
                gate.error("confirmation_without_mismatch",
                           f"step {p.get('step')} confirmed after {p.get('outcome')}", subj)
        if len(confirmations) > max_conf:
            gate.error("too_many_confirmations", f"{len(confirmations)} > {max_conf}", subj)
        if inv.get("confirm") != len(confirmations):
            gate.error("confirm_count", f"invocations.confirm={inv.get('confirm')} but "
                       f"{len(confirmations)} confirmations in probes", subj)
        base, head = int(inv.get("base", 0)), int(inv.get("head", 0))
        if not (len(probes) <= base <= len(probes) + retries
                and len(probes) <= head <= len(probes) + retries):
            gate.error("pair_invocations", f"base={base} head={head} for {len(probes)} pairs "
                       f"and {retries} retries", subj)
        total = base + head + int(inv.get("confirm", 0))
        if total > max_inv:
            gate.error("invocation_ceiling", f"{total} > {max_inv}", subj)
        unusable = sum(p.get("outcome") == "unusable" for p in probes) + sum(
            p.get("confirmation") == "unusable" for p in probes)
        if int(inv.get("failed", 0)) < unusable:
            gate.error("failed_undercount", f"failed={inv.get('failed')} < {unusable} unusable",
                       subj)
        totals["invocations"] += total
        totals["failed"] += int(inv.get("failed", 0))
        first = next((p["step"] for p in probes if p.get("outcome") == "mismatch"
                      and p.get("confirmation") == "reproduced"), None)
        if row.get("pairs_to_first_confirmed_witness") != first:
            gate.error("first_witness", f"recorded {row.get('pairs_to_first_confirmed_witness')}"
                       f" vs derived {first}", subj)
        if row.get("censored") is not (first is None):
            gate.error("censoring", "censored must be true exactly without a witness", subj)
        verdict = row.get("verdict")
        if verdict == "confirmed_regression" and first is None:
            gate.error("verdict_unsupported", "confirmed_regression without reproduced mismatch",
                       subj)
        if first is not None and verdict not in CONFIRMED:
            gate.warn("witness_not_reported", f"reproduced witness but verdict {verdict}", subj)
        if verdict == "no_defect_found":
            matches = {p["candidate_input_sha256"] for p in probes if p.get("outcome") == "match"}
            if len(matches) < 3 or any(p.get("outcome") != "match" for p in probes):
                gate.error("no_defect_unsupported",
                           f"no_defect_found with {len(matches)} distinct matches and "
                           f"outcomes {[p.get('outcome') for p in probes]}", subj)
        if first is not None and len(probes) > first:
            gate.warn("probes_after_witness", f"{len(probes) - first} probes after witness", subj)
        if arm and arm.get("candidate_freedom") == "shared_pool":
            pools[(row.get("measurement_mode"), row.get("case_id"), row.get("seed"))].add(
                row.get("pool_sha256"))
            if row.get("pool_sha256") is None:
                gate.error("pool_missing", "shared-pool arm without pool_sha256", subj)
        models = row.get("models") or {}
        for ref in ("ranker_checkpoint_sha256", "adapter_sha256", "generator_revision",
                    "frontier_model_id", "prompt_sha256", "generator_model_id"):
            if models.get(ref) is not None:
                model_refs[(str(row.get("arm_id")), ref)].add(str(models[ref]))
        if row.get("cost_status") == "unknown" and row.get("cost_usd") is not None:
            gate.error("unknown_cost_valued", "unknown cost must be null", subj)
    for key, ps in pools.items():
        if len(ps - {None}) > 1:
            gate.error("pool_differs_across_arms", f"{len(ps)} pools for {key}")
    for (arm_id, ref), vals in model_refs.items():
        if len(vals) > 1:
            gate.error("unfrozen_model_ref", f"{arm_id} uses {len(vals)} distinct {ref}")
    if len(impl) > 1:
        gate.error("multiple_implementation_shas", f"{sorted(impl)}")
    if require_complete and cases is not None:
        for (mode, split), got in coverage.items():
            arm_ids = {a for a, _, _ in got}
            split_cases = [c for c, v in cases.items() if v.get("split") == split]
            expected = len(arm_ids) * len(seeds) * len(split_cases)
            have = sum(1 for k in keys if k[0] == mode and cases.get(str(k[2]), {}).get(
                "split") == split)
            if have < expected:
                gate.error("incomplete_grid", f"{mode}/{split}: {have} of {expected} "
                           "arm x seed x case episodes")
    gate.stats.update(episodes=n, implementation_shas=sorted(impl), **totals)
    if n == 0:
        gate.error("empty", "no raw results")
    return gate
