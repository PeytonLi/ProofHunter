"""Grouped split assignment (``grouped-family-origin-cluster-v1``) and the leakage audit."""

from __future__ import annotations

import builtins
import difflib
import hashlib
import io
import json
import keyword
import tokenize
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from research.data import templates as T
from research.schemas import models as M

GROUP_KEYS = ("family_id", "defect_origin", "near_dup_cluster", "template_id")
DIFF_FINGERPRINT_VERSION = "normalized-diff-v2"
DIFF_SHINGLE_TOKENS = 5
DIFF_JACCARD_THRESHOLD = 0.9
_KEEP_NAMES = frozenset(dir(builtins)) | frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)
_SKIP_TOKENS = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
                          tokenize.ENCODING, tokenize.ENDMARKER})


def _logical_lines(source: bytes) -> list[list[tokenize.TokenInfo]]:
    """Logical lines without comments, whitespace or docstrings (bare string statements)."""
    lines: list[list[tokenize.TokenInfo]] = []
    current: list[tokenize.TokenInfo] = []
    for tok in tokenize.tokenize(io.BytesIO(source).readline):
        if tok.type in _SKIP_TOKENS:
            continue
        if tok.type == tokenize.NEWLINE:
            if current and not (len(current) == 1 and current[0].type == tokenize.STRING):
                lines.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        lines.append(current)
    return lines


def _stripped_lines(source: bytes) -> list[tuple[str, ...]]:
    """Logical lines as tokens with strings collapsed to ``STR`` (identifiers still exact)."""
    return [tuple("STR" if tok.type == tokenize.STRING else tok.string for tok in line)
            for line in _logical_lines(source)]


def _changed_lines(base: bytes, head: bytes) -> tuple[list[str], list[str]]:
    """Removed and added lines of the base->head change, after stripping comments, docstrings
    and whitespace and collapsing strings. Every non-builtin identifier in the changed lines is
    then renamed ``v<n>`` in order of first appearance, one renaming shared by base and head."""
    old, new = _stripped_lines(base), _stripped_lines(head)
    removed: list[tuple[str, ...]] = []
    added: list[tuple[str, ...]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            removed.extend(old[i1:i2])
            added.extend(new[j1:j2])
    names: dict[str, str] = {}

    def rename(line: tuple[str, ...]) -> str:
        return " ".join(
            names.setdefault(t, f"v{len(names)}")
            if (t[0].isalpha() or t[0] == "_") and t != "STR" and t not in _KEEP_NAMES else t
            for t in line)

    return [rename(line) for line in removed], [rename(line) for line in added]


@dataclass(frozen=True)
class DiffSignature:
    fingerprint: str
    shingles: frozenset[str]


def diff_signature(base: bytes, head: bytes) -> DiffSignature:
    """``normalized-diff-v2`` signature of the base->head change: the sha256 of the changed
    normalised lines plus their 5-token shingles (one shingle if the change is shorter)."""
    removed, added = _changed_lines(base, head)
    payload = json.dumps([DIFF_FINGERPRINT_VERSION, removed, added], separators=(",", ":"))
    seq = [t for line in removed for t in ("-", *line.split())]
    seq += [t for line in added for t in ("+", *line.split())]
    k = DIFF_SHINGLE_TOKENS
    shingles = (frozenset({" ".join(seq)}) if len(seq) < k
                else frozenset(" ".join(seq[i:i + k]) for i in range(len(seq) - k + 1)))
    return DiffSignature(hashlib.sha256(payload.encode()).hexdigest(), shingles)


def diff_fingerprint(base: bytes, head: bytes) -> str:
    return diff_signature(base, head).fingerprint


class DiffIndex:
    """Exact-fingerprint and shingle-Jaccard lookup over a set of case diffs."""

    def __init__(self) -> None:
        self.signatures: dict[str, DiffSignature] = {}
        self._by_fp: dict[str, set[str]] = defaultdict(set)
        self._by_shingle: dict[str, set[str]] = defaultdict(set)

    def add(self, key: str, sig: DiffSignature) -> None:
        self.signatures[key] = sig
        self._by_fp[sig.fingerprint].add(key)
        for sh in sig.shingles:
            self._by_shingle[sh].add(key)

    def matches(self, sig: DiffSignature) -> list[tuple[str, float]]:
        """Keys whose diff equals ``sig`` exactly or has Jaccard >= the threshold."""
        hits = {key: 1.0 for key in self._by_fp.get(sig.fingerprint, ())}
        inter: Counter[str] = Counter()
        for sh in sig.shingles:
            inter.update(self._by_shingle.get(sh, ()))
        for key, n in inter.items():
            other = self.signatures[key].shingles
            jaccard = n / (len(sig.shingles) + len(other) - n)
            if jaccard >= DIFF_JACCARD_THRESHOLD:
                hits[key] = max(hits.get(key, 0.0), jaccard)
        return sorted(hits.items())


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
                  diff_signatures: Mapping[str, DiffSignature] | None = None) -> dict[str, object]:
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
    split_of = {case.case_id: case.split.value for case in manifest.cases}
    diff_index = DiffIndex()
    for case_id, sig in sorted((diff_signatures or {}).items()):
        diff_index.add(case_id, sig)
    diff_pairs: list[dict[str, object]] = []
    for case_id, sig in sorted(diff_index.signatures.items()):
        for other, jaccard in diff_index.matches(sig):
            if other > case_id and split_of[other] != split_of[case_id]:
                diff_pairs.append({
                    "cases": [f"{split_of[case_id]}:{case_id}", f"{split_of[other]}:{other}"],
                    "exact": sig.fingerprint == diff_index.signatures[other].fingerprint,
                    "jaccard": round(jaccard, 4)})
    fp_split: dict[str, set[str]] = defaultdict(set)
    for case_id, sig in diff_index.signatures.items():
        fp_split[sig.fingerprint].add(split_of[case_id])
    checks["diff_fingerprint_cross_split"] = sorted(k for k, v in fp_split.items() if len(v) > 1)
    checks["diff_jaccard_cross_split"] = [p["cases"] for p in diff_pairs]
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
        "diff_fingerprints_recorded": len(fp_split),
        "diff_fingerprint_shared_across_splits": len(checks["diff_fingerprint_cross_split"]),
        "diff_shingle_tokens": DIFF_SHINGLE_TOKENS,
        "diff_jaccard_threshold": DIFF_JACCARD_THRESHOLD,
        "diff_similar_pairs_across_splits": len(diff_pairs),
        "diff_similar_pair_examples": diff_pairs[:10],
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
