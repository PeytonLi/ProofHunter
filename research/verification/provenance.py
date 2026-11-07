"""Label provenance: every label and observation must follow from the trusted oracle.

The expected value is recomputed with an independent implementation of
``checkout-rounding@1`` and cross-checked against the production T1 oracle. Kinds,
outcomes and relevance are re-derived from recorded integers (RESEARCH-SPEC §3.4, §6).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from proofhunter.contracts import (
    CONTRACT_ID,
    MAX_DISCOUNT_PERCENT,
    MAX_ITEMS,
    MAX_PRICE_CENTS,
    ORACLE_VERSION,
    canonical_sha256,
    input_sha256,
)
from proofhunter.oracle.rounding import TRUSTED_ORACLES
from research.verification.gates import GateResult

FORBIDDEN_PREFIXES = ("label", "relevance", "outcome", "observed", "witness", "expected",
                      "head_value", "base_value", "confirm", "defect", "family", "template",
                      "split", "verdict")
KIND_VERDICT = {"introduced_regression": "confirmed_regression",
                "existing_defect": "confirmed_existing_defect", "clean": "no_defect_found"}


def _is_int(v: object) -> bool:
    return type(v) is int


def independent_oracle(prices_cents: object, discount_percent: object) -> int:
    """Round half up once on the exact discounted total; raises outside the domain."""
    if not (isinstance(prices_cents, list) and len(prices_cents) <= MAX_ITEMS
            and all(_is_int(p) and 0 <= p <= MAX_PRICE_CENTS for p in prices_cents)
            and isinstance(discount_percent, int) and _is_int(discount_percent)
            and 0 <= discount_percent <= MAX_DISCOUNT_PERCENT):
        raise ValueError("input outside checkout-rounding@1 domain")
    numerator = sum(prices_cents) * (100 - discount_percent)
    quotient, remainder = divmod(numerator, 100)
    return quotient + (1 if remainder >= 50 else 0)


def trusted_expected(inp: Mapping[str, Any]) -> int:
    prices, discount = inp.get("prices_cents"), inp.get("discount_percent")
    value = independent_oracle(prices, discount)
    prod = TRUSTED_ORACLES[(CONTRACT_ID, ORACLE_VERSION)]
    assert isinstance(prices, list)
    assert isinstance(discount, int)
    if prod(list(prices), discount) != value:
        raise AssertionError("production oracle disagrees with independent oracle")
    return value


def derive_outcome(expected: int, base: int, head: int) -> str:
    if base != expected:
        return "base_mismatch"
    return "match" if head == expected else "mismatch"


def derive_kind(witness: Mapping[str, Any] | None) -> str | None:
    if witness is None:
        return "clean"
    exp, base, head = witness["expected_cents"], witness["base_cents"], witness["head_cents"]
    if head == exp:
        return None
    if base == exp:
        return "introduced_regression"
    return "existing_defect" if base == head else None


def audit_label_files(cases: Sequence[Mapping[str, Any]],
                      label_files: Mapping[str, Mapping[str, Any]],
                      manifest_cases_sha256: str) -> GateResult:
    """Every manifest case has exactly one oracle-consistent label in its split's file."""
    gate = GateResult("label_provenance")
    split_of = {str(c["case_id"]): str(c["split"]) for c in cases}
    family_of = {str(c["case_id"]): c.get("family_id") for c in cases}
    computed_cases_sha = canonical_sha256([dict(c) for c in cases])
    if computed_cases_sha != manifest_cases_sha256:
        gate.error("manifest_cases_sha256", "cases hash does not match manifest")
    counts: dict[str, dict[str, int]] = {}
    labelled: set[str] = set()
    for split, lf in label_files.items():
        kinds: dict[str, int] = defaultdict(int)
        if lf.get("split") != split:
            gate.error("label_file_split", f"file for {split} says {lf.get('split')}")
        if lf.get("sealed") is not (split == "test"):
            gate.error("seal_state", f"{split} sealed={lf.get('sealed')}")
        if lf.get("manifest_cases_sha256") != manifest_cases_sha256:
            gate.error("label_manifest_binding", f"{split} labels bound to another manifest")
        for lab in lf.get("labels", []):
            cid = str(lab.get("case_id"))
            kinds[str(lab.get("kind"))] += 1
            if cid in labelled:
                gate.error("duplicate_label", "labelled twice", cid)
            labelled.add(cid)
            if split_of.get(cid) != split:
                gate.error("label_split_mismatch", f"label in {split}, case in "
                           f"{split_of.get(cid)}", cid)
            kind, w = lab.get("kind"), lab.get("witness")
            if KIND_VERDICT.get(str(kind)) != lab.get("expected_verdict"):
                gate.error("verdict_kind", f"{kind} -> {lab.get('expected_verdict')}", cid)
            if w is not None:
                try:
                    exp = trusted_expected(w.get("input", {}))
                except (ValueError, TypeError):
                    gate.error("witness_illegal_input", "witness input outside domain", cid)
                    continue
                if not (_is_int(w.get("expected_cents")) and w["expected_cents"] == exp):
                    gate.error("witness_expected", f"{w.get('expected_cents')} != oracle {exp}",
                               cid)
                if w.get("input_sha256") != input_sha256(w["input"]):
                    gate.error("witness_input_hash", "input_sha256 mismatch", cid)
                if w.get("reproduced_by") != "reviewed_template_execution":
                    gate.error("witness_not_reproduced", str(w.get("reproduced_by")), cid)
                if lab.get("defect_family") is None:
                    gate.error("defect_family_missing", "defective label lacks family", cid)
            elif kind != "clean":
                gate.error("defect_without_witness", "defective label lacks witness", cid)
            elif not lab.get("review_rationale"):
                gate.error("clean_without_review", "clean control lacks review rationale", cid)
            derived = derive_kind(w)
            if derived != kind:
                gate.error("kind_mismatch", f"label {kind} but witness implies {derived}", cid)
            if family_of.get(cid) and lab.get("defect_family") not in (None, family_of[cid]):
                gate.warn("family_differs", f"label family {lab.get('defect_family')} vs case "
                          f"family {family_of[cid]}", cid)
        counts[split] = dict(kinds)
    missing = sorted(set(split_of) - labelled)
    gate.stats.update(counts=counts, unlabelled=len(missing))
    if missing and "test" not in counts and all(split_of[c] == "test" for c in missing):
        gate.warn("sealed_labels_unavailable",
                  f"{len(missing)} test cases unlabelled: sealed test labels not supplied")
        return gate
    for cid in missing[:50]:
        gate.error("unlabelled_case", "manifest case has no label", cid)
    return gate


def audit_observations(rows: Iterable[Mapping[str, Any]],
                       labels: Mapping[str, Mapping[str, Any]]) -> GateResult:
    """Re-derive every ``RankingObservation`` label from the oracle and recorded values."""
    gate = GateResult("observation_provenance")
    counts: dict[str, int] = defaultdict(int)
    positives: dict[str, int] = defaultdict(int)
    ids: set[str] = set()
    for row in rows:
        oid = str(row.get("observation_id"))
        cid = str(row.get("case_id"))
        if oid in ids:
            gate.error("duplicate_observation", "observation_id repeated", oid)
        ids.add(oid)
        counts[str(row.get("relevance"))] += 1
        cand = row.get("candidate", {})
        inp = cand.get("input", {})
        try:
            exp = trusted_expected(inp)
        except (ValueError, TypeError):
            gate.error("illegal_input", "candidate outside domain", oid)
            continue
        if cand.get("input_sha256") != input_sha256(inp):
            gate.error("input_hash", "candidate input_sha256 mismatch", oid)
        bad_feats = [f for f in row.get("features", {}) if f.startswith(FORBIDDEN_PREFIXES)]
        if bad_feats:
            gate.error("leaky_feature", f"post-execution/label features {bad_feats}", oid)
        outcome = row.get("outcome")
        if outcome != "unusable":
            if row.get("expected_cents") != exp:
                gate.error("expected_mismatch", f"{row.get('expected_cents')} != oracle {exp}",
                           oid)
            base, head = row.get("base_cents"), row.get("head_cents")
            if not (_is_int(base) and _is_int(head)):
                gate.error("usable_without_values", "usable outcome lacks integers", oid)
                continue
            assert isinstance(base, int)
            assert isinstance(head, int)
            if derive_outcome(exp, base, head) != outcome:
                gate.error("outcome_mismatch", f"{outcome} vs derived "
                           f"{derive_outcome(exp, base, head)}", oid)
        truth = ("positive" if outcome == "mismatch" and row.get("confirmation") == "reproduced"
                 else "zero" if outcome == "match" else "unknown")
        if row.get("relevance") != truth:
            gate.error("relevance_mismatch", f"{row.get('relevance')} vs derived {truth}", oid)
        if truth == "positive":
            positives[cid] += 1
        lab = labels.get(cid)
        if lab is None:
            gate.error("unlabelled_case", "observation case has no label", oid)
        elif lab.get("kind") == "clean" and truth == "positive":
            gate.error("clean_case_positive", "clean control produced a confirmed mismatch", oid)
        if row.get("exploration") and not row.get("selection_probability"):
            gate.error("exploration_probability", "exploration row lacks probability", oid)
    gate.stats.update(relevance=dict(counts), rows=len(ids),
                      cases_with_positive=len(positives))
    if not ids:
        gate.error("empty", "no observations")
    return gate


def audit_generator_examples(rows: Iterable[Mapping[str, Any]],
                             cases: Sequence[Mapping[str, Any]],
                             labels: Mapping[str, Mapping[str, Any]],
                             positive_inputs: Mapping[str, set[str]] | None = None
                             ) -> GateResult:
    """Inputs carry no labels/template identity; targets are grounded in real witnesses."""
    gate = GateResult("generator_example_provenance")
    case_of = {str(c["case_id"]): c for c in cases}
    n = 0
    for row in rows:
        n += 1
        eid, cid = str(row.get("example_id")), str(row.get("case_id"))
        case = case_of.get(cid)
        if case is None:
            gate.error("unknown_case", "example case not in manifest", eid)
            continue
        text = str(row.get("input"))
        leaks = [k for k in ("template_id", "family_id", "defect_origin", "near_dup_cluster",
                             "params_sha256", "case_id")
                 if isinstance(case.get(k), str) and len(case[k]) >= 6 and case[k] in text]
        if leaks:
            gate.error("identity_in_input", f"input contains case {leaks}", eid)
        if row.get("contains_template_params") is not False or row.get(
                "contains_fix_patch") is not False:
            gate.error("forbidden_content", "template params or fix patch flagged", eid)
        lab = labels.get(cid, {})
        allowed = set((positive_inputs or {}).get(cid, set()))
        if lab.get("witness"):
            allowed.add(lab["witness"]["input_sha256"])
        for hyp in row.get("target", []):
            g = hyp.get("grounding_witness_sha256")
            if g is not None and g not in allowed:
                gate.error("ungrounded_target", "grounding witness not a reproduced input", eid)
            if lab.get("kind") == "clean" and g is not None:
                gate.error("clean_grounded_target", "clean case target claims a witness", eid)
    gate.stats["examples"] = n
    if n == 0:
        gate.error("empty", "no generator examples")
    return gate
