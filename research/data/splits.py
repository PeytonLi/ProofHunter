"""Grouped split assignment (``grouped-family-origin-cluster-v1``) and the leakage audit."""

from __future__ import annotations

import builtins
import difflib
import hashlib
import json
import keyword
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from research.data import templates as T
from research.schemas import models as M

GROUP_KEYS = ("family_id", "defect_origin", "near_dup_cluster", "template_id")
DIFF_FINGERPRINT_VERSION = "normalized-diff-v1"
_TOKEN = re.compile(
    r'''[rbuRBU]{0,2}("""|\'\'\'|"|\')(?:\\.|(?!\1).)*\1'''
    r'''|\d[\d_]*(?:\.\d*)?|[A-Za-z_]\w*|\S''')

_SEMANTIC_NAMES = frozenset(dir(builtins)) | {
    "Decimal", "Fraction", "ROUND_HALF_UP", "ROUND_FLOOR", "ROUND_CEILING", "ROUND_HALF_EVEN",
    "ROUND_DOWN", "ROUND_HALF_DOWN", "decimal", "fractions", "functools", "math", "operator",
    "floor", "ceil", "reduce", "add", "quantize", "numerator", "denominator"}

_ROLES = ("ITEMS", "DISCOUNT", "SUBTOTAL", "TOTAL", "HELPER")
_ROLE_OF = {name: role for names in T.NAMES for name, role in zip(names, _ROLES, strict=True)}
_ROLE_OF[T.ALT_HELPER] = "HELPER"


def _normalize_line(line: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN.finditer(line):
        tok = match.group(0)
        if tok == "#":
            break
        if match.group(1):
            tokens.append("STR")
        elif tok[0].isalpha() or tok[0] == "_":
            semantic = keyword.iskeyword(tok) or tok in _SEMANTIC_NAMES
            tokens.append(tok if semantic else _ROLE_OF.get(tok, "ID"))
        else:
            tokens.append(tok)
    return tokens


def _tokens(line: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN.finditer(line):
        if match.group(0) == "#":
            break
        tokens.append(match.group(0))
    return tokens


def _code_lines(source: bytes) -> list[str]:
    lines = (" ".join(_tokens(line)) for line in source.decode().splitlines())
    return [line for line in lines if line]


def diff_fingerprint(base: bytes, head: bytes) -> str:
    """Hash of the base->head changed lines. Lines are compared with whitespace and comments
    removed, so formatting-only edits vanish; the changed lines are then hashed with string
    contents as ``STR``, template renames collapsed to their role and other names to ``ID``.
    Renamed or reformatted copies of the same change collide across splits."""
    old, new = _code_lines(base), _code_lines(head)
    removed: list[list[str]] = []
    added: list[list[str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            removed.extend(_normalize_line(line) for line in old[i1:i2])
            added.extend(_normalize_line(line) for line in new[j1:j2])
    payload = json.dumps([DIFF_FINGERPRINT_VERSION, removed, added], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def template_split(template_id: str) -> M.Split:
    shape_id = template_id.partition("__")[2]
    return T.SHAPE_BY_ID[shape_id].split


def assign_splits(cases: Sequence[M.CaseRecord], protocol: M.ProtocolV1, *,
                  dataset_commit: str = "0" * 40) -> M.CaseManifest:
    """Assign each case the split owned by its template (family and shape are split-owned).

    Grouping keys are then checked by the frozen ``CaseManifest`` validator.
    """
    del protocol
    records = []
    for case in sorted(cases, key=lambda c: c.case_id):
        split = template_split(case.template_id)
        records.append(case if case.split is split else case.model_copy(update={"split": split}))
    dumped = [c.model_dump(mode="json") for c in records]
    return M.CaseManifest(schema_version="research-case-manifest-v1", track=M.Track.A,
                          manifest_version="track-a-cases-v1",
                          split_policy="grouped-family-origin-cluster-v1",
                          dataset_commit=dataset_commit, cases=records,
                          cases_sha256=M.canonical_sha256(dumped))


def leakage_audit(manifest: M.CaseManifest, labels: Mapping[M.Split, M.LabelFile],
                  clusters: Mapping[str, str], fingerprints: Mapping[str, str],
                  bench_hashes: set[str],
                  diff_fingerprints: Mapping[str, str] | None = None) -> dict[str, object]:
    by_split: dict[str, list[M.CaseRecord]] = defaultdict(list)
    for case in manifest.cases:
        by_split[case.split.value].append(case)
    kinds = {lab.case_id: lab.kind.value for lf in labels.values() for lab in lf.labels}
    checks: dict[str, object] = {}
    for key in GROUP_KEYS:
        owners: dict[str, set[str]] = defaultdict(set)
        for case in manifest.cases:
            owners[str(getattr(case, key))].add(case.split.value)
        checks[f"{key}_cross_split"] = sorted(k for k, v in owners.items() if len(v) > 1)
    diff_owner: dict[str, set[str]] = defaultdict(set)
    diff_cases: dict[str, list[str]] = defaultdict(list)
    for case in manifest.cases:
        fp = (diff_fingerprints or {}).get(case.case_id)
        if fp:
            diff_owner[fp].add(case.split.value)
            diff_cases[fp].append(f"{case.split.value}:{case.case_id}")
    shared_diffs = sorted(k for k, v in diff_owner.items() if len(v) > 1)
    checks["diff_fingerprint_cross_split"] = shared_diffs
    src_owner: dict[str, set[str]] = defaultdict(set)
    for case in manifest.cases:
        src_owner[case.head_source_sha256].add(case.split.value)
        src_owner[case.base_source_sha256].add(case.split.value)
    shared_sources = sorted(k for k, v in src_owner.items() if len(v) > 1)
    fp_owner: dict[str, set[str]] = defaultdict(set)
    for case in manifest.cases:
        fp = fingerprints.get(case.case_id)
        if fp:
            fp_owner[fp].add(case.split.value)
    shape_owner: dict[str, set[str]] = defaultdict(set)
    for case in manifest.cases:
        shape_owner[case.template_id.partition("__")[2]].add(case.split.value)
    summary = {
        split: {
            "cases": len(rows),
            "defect_cases": sum(kinds[c.case_id] != "clean" for c in rows),
            "clean_controls": sum(kinds[c.case_id] == "clean" for c in rows),
            "existing_defects": sum(kinds[c.case_id] == "existing_defect" for c in rows),
            "defect_families": len({c.family_id for c in rows if kinds[c.case_id] != "clean"}),
            "cases_per_family": dict(sorted(Counter(c.family_id for c in rows).items())),
            "templates": len({c.template_id for c in rows}),
            "near_dup_clusters": len({c.near_dup_cluster for c in rows}),
            "max_cases_per_cluster": max(Counter(c.near_dup_cluster for c in rows).values()),
        }
        for split, rows in sorted(by_split.items())
    }
    return {
        "schema": "track-a-split-audit-v1",
        "split_policy": manifest.split_policy,
        "manifest_cases_sha256": manifest.cases_sha256,
        "summary": summary,
        "grouping_key_violations": checks,
        "diff_fingerprint_version": DIFF_FINGERPRINT_VERSION,
        "diff_fingerprints_recorded": len(diff_owner),
        "diff_fingerprint_shared_across_splits": len(shared_diffs),
        "diff_fingerprint_shared_examples": {k: sorted(diff_cases[k]) for k in shared_diffs[:5]},
        "source_sha256_shared_across_splits": len(shared_sources),
        "source_sha256_shared_examples": shared_sources[:5],
        "behavior_fingerprint_shared_across_splits": sum(len(v) > 1 for v in fp_owner.values()),
        "code_shape_shared_across_splits": sorted(k for k, v in shape_owner.items() if len(v) > 1),
        "production_benchmark_source_overlap": sorted(
            {c.base_source_sha256 for c in manifest.cases} & bench_hashes
            | {c.head_source_sha256 for c in manifest.cases} & bench_hashes),
        "clusters_recorded": len(set(clusters.values())),
        "test_labels_sealed": labels[M.Split.TEST].sealed,
        "test_label_file": "labels/test.sealed.json (store.labels('test', unseal=...) only)",
        "known_shared_structure": (
            "Rounding helper styles (addfloor/divmod/float/fraction/decimal) recur across splits "
            "in different shape combinations; shapes, template ids, families, defect origins and "
            "behavior clusters do not. CaseRecord.params/family_id are public by schema design; "
            "arms must not read them (EpisodeContext excludes them)."),
    }
