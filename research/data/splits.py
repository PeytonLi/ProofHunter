"""Grouped split assignment (``grouped-family-origin-cluster-v1``) and the leakage audit."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from research.data import templates as T
from research.schemas import models as M

GROUP_KEYS = ("family_id", "defect_origin", "near_dup_cluster", "template_id")


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
                  bench_hashes: set[str]) -> dict[str, object]:
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
