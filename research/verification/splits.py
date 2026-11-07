"""Split and leakage audit (Track A manifests, Track B task rows, training artifacts).

Splits must be disjoint by case, family, defect origin, near-duplicate cluster, template,
source hashes and near-duplicate code change. No test identity may be reachable from any
training/validation artifact. Derived from RESEARCH-SPEC §3.1/§6 and the frozen schemas only.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from research.verification.gates import GateResult

SPLITS = ("train", "validation", "test")
DISJOINT_KEYS = ("family_id", "defect_origin", "near_dup_cluster", "template_id",
                 "head_source_sha256")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf"}


def shingles(text: str, k: int = 5) -> frozenset[int]:
    tokens = _TOKEN.findall(text)
    if len(tokens) < k:
        return frozenset({hash(tuple(tokens))}) if tokens else frozenset()
    return frozenset(hash(tuple(tokens[i:i + k])) for i in range(len(tokens) - k + 1))


def change_text(base: bytes, head: bytes) -> str:
    """Only the changed lines of base -> head (context-free unified diff body)."""
    diff = difflib.unified_diff(base.decode("utf-8", "replace").splitlines(),
                                head.decode("utf-8", "replace").splitlines(), n=0, lineterm="")
    return "\n".join(line for line in diff
                     if line[:1] in "+-" and not line.startswith(("+++", "---")))


def near_duplicates(texts: Mapping[str, tuple[str, str]], threshold: float,
                    k: int = 5) -> list[tuple[str, str, float]]:
    """Cross-split pairs ``(id_a, id_b, jaccard)`` with similarity >= ``threshold``.

    ``texts`` maps case id -> (split, change text). Candidates come from an inverted
    shingle index; exact-duplicate texts are always reported.
    """
    sh = {cid: shingles(text, k) for cid, (_, text) in texts.items()}
    index: dict[int, list[str]] = defaultdict(list)
    for cid, s in sh.items():
        for h in s:
            index[h].append(cid)
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for ids in index.values():
        if len(ids) > 500:
            continue
        for a, b in combinations(sorted(ids), 2):
            if texts[a][0] != texts[b][0]:
                shared[(a, b)] += 1
    out: dict[tuple[str, str], float] = {}
    for (a, b), inter in shared.items():
        union = len(sh[a]) + len(sh[b]) - inter
        sim = inter / union if union else 0.0
        if sim >= threshold:
            out[(a, b)] = round(sim, 4)
    by_text: dict[str, list[str]] = defaultdict(list)
    for cid, (_, text) in texts.items():
        if text.strip():
            by_text[" ".join(_TOKEN.findall(text))].append(cid)
    for ids in by_text.values():
        for a, b in combinations(sorted(ids), 2):
            if texts[a][0] != texts[b][0]:
                out[(a, b)] = 1.0
    return sorted((a, b, s) for (a, b), s in out.items())


def audit_manifest_splits(cases: Sequence[Mapping[str, Any]], *,
                          protocol: Mapping[str, Any] | None = None,
                          labels: Mapping[str, Mapping[str, Any]] | None = None,
                          sources: Mapping[str, tuple[bytes, bytes]] | None = None,
                          bench_hashes: Iterable[str] = (),
                          near_dup_threshold: float = 0.9) -> GateResult:
    """Grouped-split audit over ``CaseRecord`` dicts.

    ``labels`` (case id -> ``CaseLabel`` dict) enables defect/clean counts per split;
    ``sources`` (case id -> (base, head) bytes of the reviewed template output) enables the
    near-duplicate change check; ``bench_hashes`` are frozen production-benchmark source
    hashes that research cases must not reuse.
    """
    gate = GateResult("split_leakage")
    split_of: dict[str, str] = {}
    by_split: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        cid, split = case.get("case_id"), case.get("split")
        if not isinstance(cid, str) or split not in SPLITS:
            gate.error("bad_case", f"case_id={cid!r} split={split!r}")
            continue
        if cid in split_of:
            gate.error("duplicate_case_id", f"in {split_of[cid]} and {split}", cid)
            continue
        split_of[cid] = split
        by_split[split].append(case)
    for key in DISJOINT_KEYS:
        owner: dict[Any, set[str]] = defaultdict(set)
        for split, rows in by_split.items():
            for row in rows:
                if row.get(key) in (None, ""):
                    gate.error("missing_group_key", f"missing {key}", str(row.get("case_id")))
                else:
                    owner[row[key]].add(split)
        overlaps = {k: sorted(v) for k, v in owner.items() if len(v) > 1}
        gate.stats[f"{key}_cross_split"] = len(overlaps)
        for value, splits in sorted(overlaps.items(), key=lambda kv: str(kv[0]))[:50]:
            gate.error(f"{key}_overlap", f"{value!r} in splits {splits}")
    owner_t: dict[tuple[Any, Any], set[str]] = defaultdict(set)
    pairs: dict[tuple[Any, Any], list[str]] = defaultdict(list)
    for split, rows in by_split.items():
        for row in rows:
            owner_t[(row.get("template_id"), row.get("params_sha256"))].add(split)
            pairs[(row.get("base_source_sha256"), row.get("head_source_sha256"))].append(
                str(row.get("case_id")))
    for tkey, tsplits in owner_t.items():
        if len(tsplits) > 1:
            gate.error("template_params_overlap", f"{tkey} in {sorted(tsplits)}")
    for ids in pairs.values():
        if len(ids) > 1:
            gate.error("duplicate_source_pair", f"{len(ids)} cases share base/head {ids[:5]}")
    bench = set(bench_hashes)
    if bench:
        hits = [str(c.get("case_id")) for c in cases
                if {c.get("base_source_sha256"), c.get("head_source_sha256")} & bench]
        gate.stats["bench_source_overlap"] = len(hits)
        for cid in hits[:50]:
            gate.error("bench_source_overlap", "source hash reused from frozen bench", cid)
    else:
        gate.warn("bench_not_checked", "no frozen bench hashes supplied")
    counts: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        rows = by_split.get(split, [])
        kinds: dict[str, int] = defaultdict(int)
        for row in rows:
            lab = (labels or {}).get(str(row.get("case_id")))
            kinds[str(lab.get("kind")) if lab else "unlabelled"] += 1
        counts[split] = {"cases": len(rows), "families": len({r.get("family_id") for r in rows}),
                         **kinds}
    gate.stats["counts"] = counts
    for split, target in ((protocol or {}).get("split_targets") or {}).items():
        got = counts.get(split, {})
        if got.get("families", 0) < target["min_families"]:
            gate.error("too_few_families", f"{split}: {got.get('families', 0)} families < "
                       f"{target['min_families']}")
        if labels is not None:
            defects = got.get("introduced_regression", 0) + got.get("existing_defect", 0)
            if defects < target["defect_cases"]:
                gate.warn("below_defect_target", f"{split}: {defects} < {target['defect_cases']}")
            if got.get("clean", 0) < target["clean_controls"]:
                gate.warn("below_clean_target",
                          f"{split}: {got.get('clean', 0)} < {target['clean_controls']}")
    if sources is None:
        gate.warn("near_dup_not_checked", "no case sources supplied; change similarity unchecked")
    else:
        texts = {cid: (split_of[cid], change_text(*src)) for cid, src in sources.items()
                 if cid in split_of}
        missing = len(split_of) - len(texts)
        if missing:
            gate.error("sources_missing", f"{missing} cases have no sources")
        dups = near_duplicates(texts, near_dup_threshold)
        gate.stats["near_duplicate_pairs"] = len(dups)
        for a, b, sim in dups[:50]:
            gate.error("near_duplicate_change",
                       f"{a} ({texts[a][0]}) ~ {b} ({texts[b][0]}) jaccard={sim}")
    return gate


def audit_rows_against_manifest(rows: Iterable[Mapping[str, Any]],
                                cases: Sequence[Mapping[str, Any]], *,
                                name: str, allowed: Sequence[str] = ("train", "validation"),
                                ) -> GateResult:
    """Training rows (observations, generator examples) only touch allowed-split cases."""
    gate = GateResult(f"row_splits:{name}")
    split_of = {str(c["case_id"]): str(c["split"]) for c in cases}
    n = 0
    seen: dict[str, int] = defaultdict(int)
    for row in rows:
        n += 1
        cid = str(row.get("case_id"))
        real = split_of.get(cid)
        if real is None:
            gate.error("unknown_case", "row references a case outside the manifest", cid)
        elif real not in allowed:
            gate.error("forbidden_split_case", f"row uses a {real} case", cid)
        elif row.get("split") != real:
            gate.error("split_mismatch", f"row split {row.get('split')} != manifest {real}", cid)
        seen[real or "unknown"] += 1
    gate.stats.update(rows=n, by_manifest_split=dict(seen))
    if n == 0:
        gate.error("empty", "no rows")
    return gate


def audit_swe_smith_splits(rows: Sequence[Mapping[str, Any]]) -> GateResult:
    """Track B: repository (split_group) and defect origin disjoint; never executed."""
    gate = GateResult("split_leakage_track_b")
    for key in ("split_group", "repo", "defect_origin", "instance_id"):
        owner: dict[Any, set[str]] = defaultdict(set)
        for row in rows:
            owner[row.get(key)].add(str(row.get("split")))
        for value, splits in owner.items():
            if len(splits) > 1:
                gate.error(f"{key}_overlap", f"{value!r} in {sorted(splits)}")
    for row in rows:
        if row.get("executed") is not False or row.get("label_status") != "unverified_design_only":
            gate.error("track_b_executed_or_labelled", "Track B is design-only",
                       str(row.get("instance_id")))
    gate.stats["tasks"] = len(rows)
    return gate


def collect_test_tokens(cases: Sequence[Mapping[str, Any]],
                     keys: Sequence[str] = ("case_id", "base_source_sha256",
                                            "head_source_sha256", "near_dup_cluster",
                                            "defect_origin")) -> dict[str, str]:
    """Identifiers that occur in test cases and in no train/validation case."""
    non_test = {str(c.get(k)) for c in cases if c.get("split") != "test" for k in keys}
    out: dict[str, str] = {}
    for c in cases:
        if c.get("split") != "test":
            continue
        for k in keys:
            v = c.get(k)
            if isinstance(v, str) and len(v) >= 8 and v not in non_test:
                out[v] = f"{c.get('case_id')}:{k}"
    return out


def audit_training_artifacts(cases: Sequence[Mapping[str, Any]], paths: Iterable[Path], *,
                             extra_tokens: Mapping[str, str] | None = None,
                             max_bytes: int = 1 << 30) -> GateResult:
    """Scan training/validation artifacts (data, checkpoints, logs) for test-only tokens."""
    gate = GateResult("test_label_unreachable")
    tokens = collect_test_tokens(cases) | dict(extra_tokens or {})
    gate.stats["test_tokens"] = len(tokens)
    if not tokens:
        gate.error("no_test_tokens", "no test-only identifiers; audit vacuous")
        return gate
    encoded = [(t.encode(), src) for t, src in tokens.items()]
    scanned = 0
    for root in paths:
        if not root.exists():
            gate.error("artifact_missing", "path does not exist", str(root))
            continue
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in files:
            if path.suffix.lower() in _SKIP_SUFFIXES:
                continue
            if path.stat().st_size > max_bytes:
                gate.error("artifact_too_large", "exceeds scan limit", str(path))
                continue
            data = path.read_bytes()
            scanned += 1
            hits = [src for tok, src in encoded if tok in data]
            for src in hits[:10]:
                gate.error("test_token_in_training_artifact", f"contains {src}", str(path))
    gate.stats["files_scanned"] = scanned
    if scanned == 0:
        gate.error("no_artifacts_scanned", "nothing scanned; audit vacuous")
    return gate
