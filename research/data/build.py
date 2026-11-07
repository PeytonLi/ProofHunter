"""Track A case builder: enumerate reviewed templates, validate against the trusted oracle,
remove duplicates, select grouped splits, write the manifest, sealed labels and audits."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from research.data import execute as X
from research.data import splits as S
from research.data import templates as T
from research.data.splits import assign_splits, leakage_audit
from research.schemas import models as M

MANIFEST_VERSION: Final = "track-a-cases-v1"
EXISTING_DEFECT_SHARE: Final = 10  # percent of defect behaviors rendered as existing defects
SPLIT_PRIORITY: Final = {M.Split.TEST: 0, M.Split.VALIDATION: 1, M.Split.TRAIN: 2}

_PROBES: list[X.Input] = []
_EXPECTED: list[int] = []


def _h(*parts: object) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def _pick(seq: Sequence[object], *key: object) -> object:
    return seq[int(_h(*key), 16) % len(seq)]


@dataclass
class Raw:
    template_id: str
    family_id: str
    split: M.Split
    kind: M.LabelKind
    params: dict[str, T.ParamValue]
    base: bytes = b""
    head: bytes = b""
    fingerprint: str = ""
    cluster: str = ""
    witness_index: int | None = None
    witness: dict[str, int] = field(default_factory=dict)
    reason: str | None = None

    @property
    def params_sha256(self) -> str:
        return M.canonical_sha256(self.params)

    @property
    def key(self) -> str:
        return _h(self.template_id, self.params_sha256)


def enumerate_defects(master_seed: int) -> list[Raw]:
    out: list[Raw] = []
    for fam in T.FAMILIES:
        shapes = [s.shape_id for s in T.SHAPES if s.split is fam.split]
        for params in T.iter_defect_params(fam):
            pkey = (master_seed, fam.family_id, params["value"], params["cond"],
                    params["cond_value"])
            shape = str(_pick(shapes, "shape", *pkey))
            params = dict(params, names=int(_pick(range(3), "names", *pkey)),
                          doc=int(_pick(range(3), "doc", *pkey)),
                          guard=int(_pick(range(2), "guard", *pkey)))
            kind = M.LabelKind.INTRODUCED_REGRESSION
            if int(_h("kind", *pkey), 16) % 100 < EXISTING_DEFECT_SHARE:
                kind = M.LabelKind.EXISTING_DEFECT
                params.update(kind="existing", head_names=(params["names"] + 1) % 3,
                              head_doc=int(_pick(range(3), "hdoc", *pkey)),
                              head_guard=params["guard"])
            out.append(Raw(f"{fam.family_id}__{shape}", fam.family_id, fam.split, kind, params))
    return out


def enumerate_clean(master_seed: int) -> list[Raw]:
    out: list[Raw] = []
    for shape in T.SHAPES:
        for guard in (0, 1):
            for head_guard in (0, 1):
                for sub in T.SUBTOTALS:
                    for num in T.NUMERATORS:
                        if any(o.split is not shape.split and
                               (o.rounding, o.subtotal, o.numerator) == (shape.rounding, sub, num)
                               for o in T.SHAPES):
                            continue
                        pkey = (master_seed, shape.shape_id, guard, head_guard, sub, num)
                        names = int(_pick(range(3), "names", *pkey))
                        params: dict[str, T.ParamValue] = {
                            "names": names, "doc": int(_pick(range(3), "doc", *pkey)),
                            "guard": guard, "head_names": (names + 1) % 3,
                            "head_doc": int(_pick(range(3), "hdoc", *pkey)),
                            "head_guard": head_guard, "head_subtotal": sub,
                            "head_numerator": num}
                        out.append(Raw(f"clean__{shape.shape_id}",
                                       T.clean_family_id(shape.shape_id), shape.split,
                                       M.LabelKind.CLEAN, params,
                                       cluster="c-" + _h("clean", shape.shape_id, guard,
                                                         head_guard, sub, num)[:20]))
    return out


def _init(probes: list[X.Input], expected: list[int]) -> None:
    global _PROBES, _EXPECTED
    _PROBES, _EXPECTED = probes, expected


def _vector(source: bytes) -> list[int | None]:
    entry = X.load_entry(source)
    return [X.call(entry, item) for item in _PROBES]


def validate(raw: Raw) -> Raw:
    """Render, execute and label one raw case (reviewed template code only)."""
    try:
        raw.base, raw.head = T.render(raw.template_id, raw.params)
    except ValueError:
        raw.reason = "render_rejected"
        return raw
    if raw.base == raw.head:
        raw.reason = "head_identical_to_base"
        return raw
    try:
        base, head = _vector(raw.base), _vector(raw.head)
    except Exception:
        raw.reason = "source_load_failed"
        return raw
    if None in base or None in head:
        raw.reason = "unusable_output_on_probe"
        return raw
    if raw.kind is not M.LabelKind.EXISTING_DEFECT and base != _EXPECTED:
        raw.reason = "base_violates_oracle"
        return raw
    if raw.kind is M.LabelKind.CLEAN:
        if head != _EXPECTED:
            raw.reason = "clean_head_violates_oracle"
        return raw
    if raw.kind is M.LabelKind.EXISTING_DEFECT and base != head:
        raw.reason = "existing_defect_base_head_differ"
        return raw
    diffs = [k for k, (h, e) in enumerate(zip(head, _EXPECTED, strict=True)) if h != e]
    if not diffs:
        raw.reason = "equivalent_on_probe_set"
        return raw
    index = diffs[0]
    item = _PROBES[index]
    fresh = X.call(X.load_entry(raw.head), item)
    if fresh != head[index] or X.expected(item) != _EXPECTED[index]:
        raw.reason = "witness_not_reproduced"
        return raw
    raw.witness_index = index
    raw.witness = {"expected": _EXPECTED[index], "base": base[index] or 0, "head": fresh or 0}
    raw.fingerprint = hashlib.sha256(json.dumps(head).encode()).hexdigest()
    raw.cluster = "b-" + raw.fingerprint[:20]
    return raw


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,  # noqa: S607
                              text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40


def _bench_source_hashes() -> set[str]:
    try:
        from bench import corpus
    except ImportError:
        return set()
    manifest = corpus.load()
    return {h for case in manifest.cases for h in (case.base_sha256, case.head_sha256)}


def enumerate_test_clean_backfill(master_seed: int) -> list[Raw]:
    """Test-only clean variants that also refactor the base's subtotal/numerator form.

    Used only to backfill test clean controls dropped for cross-split diff duplicates; base and
    head code shapes never equal a train/validation shape.
    """
    other = {(o.rounding, o.subtotal, o.numerator) for o in T.SHAPES if o.split is not M.Split.TEST}
    out: list[Raw] = []
    for shape in T.SHAPES:
        if shape.split is not M.Split.TEST:
            continue
        for guard in (0, 1):
            for head_guard in (0, 1):
                for bsub in T.SUBTOTALS:
                    for bnum in T.NUMERATORS:
                        if (shape.rounding, bsub, bnum) in other or (bsub, bnum) == (
                                shape.subtotal, shape.numerator):
                            continue
                        for sub in T.SUBTOTALS:
                            for num in T.NUMERATORS:
                                if ((shape.rounding, sub, num) in other
                                        or (sub, num) == (bsub, bnum)):
                                    continue
                                pkey = (master_seed, "backfill", shape.shape_id, guard, head_guard,
                                        bsub, bnum, sub, num)
                                names = int(_pick(range(3), "names", *pkey))
                                params: dict[str, T.ParamValue] = {
                                    "names": names, "doc": int(_pick(range(3), "doc", *pkey)),
                                    "guard": guard, "subtotal": bsub, "numerator": bnum,
                                    "head_names": (names + 1) % 3,
                                    "head_doc": int(_pick(range(3), "hdoc", *pkey)),
                                    "head_guard": head_guard, "head_subtotal": sub,
                                    "head_numerator": num}
                                out.append(Raw(
                                    f"clean__{shape.shape_id}", T.clean_family_id(shape.shape_id),
                                    shape.split, M.LabelKind.CLEAN, params,
                                    cluster="c-" + _h("clean-backfill", shape.shape_id, guard,
                                                      head_guard, bsub, bnum, sub, num)[:20]))
    return out


def repair_test_split(chosen: list[Raw], validated: Sequence[Raw], backfill: Sequence[Raw],
                      protocol: M.ProtocolV1,
                      bench_hashes: set[str]) -> tuple[list[Raw], list[Raw]]:
    """Drop test cases whose normalised diff matches a train/validation diff (exact
    ``normalized-diff-v2`` fingerprint or shingle Jaccard >= 0.9) and backfill them with
    test-only cases. Train and validation selections are left untouched."""
    blocked = S.DiffIndex()
    for r in chosen:
        if r.split is not M.Split.TEST:
            blocked.add(case_id(r), S.diff_signature(r.base, r.head))
    blocked_src = {_sha(x) for r in validated if r.split is not M.Split.TEST and r.base
                   for x in (r.base, r.head)} | bench_hashes
    kept: list[Raw] = []
    dropped: list[Raw] = []
    for raw in chosen:
        if raw.split is M.Split.TEST and blocked.matches(S.diff_signature(raw.base, raw.head)):
            raw.reason = "cross_split_diff_duplicate"
            dropped.append(raw)
        else:
            kept.append(raw)
    used_fp = {S.diff_fingerprint(r.base, r.head) for r in kept if r.split is M.Split.TEST}
    used_clusters = {r.cluster for r in kept}
    used_pairs = {(r.base, r.head) for r in kept}
    target = protocol.split_targets[M.Split.TEST.value]
    excluded: list[Raw] = list(dropped)
    pools = {
        False: [r for r in validated if r.split is M.Split.TEST and r.kind is not M.LabelKind.CLEAN
                and r.reason == "over_split_target"],
        True: list(backfill),
    }
    for clean, want in ((False, target.defect_cases), (True, target.clean_controls)):
        have = sum(r.split is M.Split.TEST and (r.kind is M.LabelKind.CLEAN) == clean for r in kept)
        if have >= want:
            continue
        by_family: dict[str, list[Raw]] = defaultdict(list)
        for raw in sorted(pools[clean], key=lambda r: _h(protocol.master_seed, "backfill", r.key)):
            if raw.reason not in (None, "over_split_target"):
                excluded.append(raw)
                continue
            sig = S.diff_signature(raw.base, raw.head)
            fp = sig.fingerprint
            leaks = bool(blocked.matches(sig))
            if leaks or {_sha(raw.base), _sha(raw.head)} & blocked_src:
                raw.reason = "cross_split_diff_duplicate" if leaks else (
                    "cross_split_source_duplicate")
            elif (fp in used_fp or raw.cluster in used_clusters
                  or (raw.base, raw.head) in used_pairs):
                raw.reason = "near_duplicate_behavior"
            else:
                raw.reason = None
                used_fp.add(fp)
                used_clusters.add(raw.cluster)
                used_pairs.add((raw.base, raw.head))
                by_family[raw.family_id].append(raw)
                continue
            if clean:
                excluded.append(raw)
        picked, rest = _round_robin(by_family, max(0, want - have))
        for raw in rest:
            raw.reason = "over_split_target"
        kept += picked
        if clean:
            excluded += rest
    return kept, excluded


def _round_robin(by_family: Mapping[str, list[Raw]], target: int) -> tuple[list[Raw], list[Raw]]:
    queues = {f: list(v) for f, v in sorted(by_family.items())}
    chosen: list[Raw] = []
    while len(chosen) < target and any(queues.values()):
        for fam in sorted(queues):
            if queues[fam] and len(chosen) < target:
                chosen.append(queues[fam].pop(0))
    rest = [r for q in queues.values() for r in q]
    return chosen, rest


def select(raws: Iterable[Raw], protocol: M.ProtocolV1,
           bench_hashes: set[str]) -> tuple[list[Raw], list[Raw]]:
    excluded: list[Raw] = []
    valid: list[Raw] = []
    seen_pairs: set[tuple[bytes, bytes]] = set()
    for raw in sorted(raws, key=lambda r: r.key):
        if raw.reason is None:
            pair = (raw.base, raw.head)
            if pair in seen_pairs:
                raw.reason = "duplicate_source_pair"
            elif {_sha(raw.base), _sha(raw.head)} & bench_hashes:
                raw.reason = "shares_source_hash_with_production_benchmark"
            else:
                seen_pairs.add(pair)
        (excluded if raw.reason else valid).append(raw)
    owners: dict[str, set[M.Split]] = defaultdict(set)
    for raw in valid:
        for data in (raw.base, raw.head):
            owners[_sha(data)].add(raw.split)
    shared = [r for r in valid if len(owners[_sha(r.base)] | owners[_sha(r.head)]) > 1]
    for raw in shared:
        raw.reason = "cross_split_source_duplicate"
    excluded += shared
    valid = [r for r in valid if r.reason is None]
    clusters: dict[str, list[Raw]] = defaultdict(list)
    for raw in valid:
        clusters[raw.cluster].append(raw)
    kept: list[Raw] = []
    for members in clusters.values():
        home = min(members, key=lambda r: (SPLIT_PRIORITY[r.split], r.key)).split
        first = True
        for raw in sorted(members, key=lambda r: (SPLIT_PRIORITY[r.split], r.key)):
            if raw.split is not home:
                raw.reason = "cross_split_behavior_duplicate"
            elif not first:
                raw.reason = "near_duplicate_behavior"
            else:
                first = False
                kept.append(raw)
                continue
            excluded.append(raw)
    chosen: list[Raw] = []
    for split in (M.Split.TRAIN, M.Split.VALIDATION, M.Split.TEST):
        target = protocol.split_targets[split.value]
        for clean, want in ((False, target.defect_cases), (True, target.clean_controls)):
            by_family: dict[str, list[Raw]] = defaultdict(list)
            for raw in sorted(kept, key=lambda r: _h(protocol.master_seed, "order", r.key)):
                if raw.split is split and (raw.kind is M.LabelKind.CLEAN) == clean:
                    by_family[raw.family_id].append(raw)
            picked, rest = _round_robin(by_family, want)
            for raw in rest:
                raw.reason = "over_split_target"
            chosen += picked
            excluded += rest
    return chosen, excluded


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def case_id(raw: Raw) -> str:
    return "a-" + _h(T.TEMPLATE_REGISTRY_VERSION, raw.template_id, raw.params_sha256)[:20]


def to_record(raw: Raw, master_seed: int) -> M.CaseRecord:
    cid = case_id(raw)
    origin = (f"clean:{raw.template_id}@{T.TEMPLATE_VERSION}" if raw.kind is M.LabelKind.CLEAN
              else f"op:{raw.family_id}@{T.TEMPLATE_VERSION}")
    return M.CaseRecord(
        schema_version="research-case-v1", track=M.Track.A, case_id=cid, split=raw.split,
        family_id=raw.family_id, defect_origin=origin, near_dup_cluster=raw.cluster,
        template_id=raw.template_id, template_version=T.TEMPLATE_VERSION,
        template_sha256=T.template_sha256(raw.template_id), params=raw.params,
        params_sha256=raw.params_sha256, contract_id="checkout-rounding@1",
        base_source_sha256=_sha(raw.base), head_source_sha256=_sha(raw.head),
        candidate_seed=int(_h(master_seed, "candidate-seed", cid)[:8], 16))


def to_label(raw: Raw, probes: Sequence[X.Input], probe_count: int) -> M.CaseLabel:
    cid = case_id(raw)
    if raw.kind is M.LabelKind.CLEAN:
        return M.CaseLabel(
            case_id=cid, kind=raw.kind, defect_family=None,
            expected_verdict=M.Verdict.NO_DEFECT_FOUND, witness=None,
            review_rationale=(f"reviewed cosmetic refactor template {raw.template_id}"
                              " (names, docstring, empty guard, subtotal/numerator form); base and"
                              f" head equal the trusted oracle on all {probe_count} probe inputs"))
    assert raw.witness_index is not None
    prices, discount = probes[raw.witness_index]
    data = {"discount_percent": discount, "prices_cents": list(prices)}
    witness = M.Witness(input=M.CheckoutInput(**data), input_sha256=M.input_sha256(data),
                        expected_cents=raw.witness["expected"], base_cents=raw.witness["base"],
                        head_cents=raw.witness["head"], reproduced_by="reviewed_template_execution")
    verdict = (M.Verdict.CONFIRMED_REGRESSION if raw.kind is M.LabelKind.INTRODUCED_REGRESSION
               else M.Verdict.CONFIRMED_EXISTING_DEFECT)
    return M.CaseLabel(case_id=cid, kind=raw.kind, defect_family=raw.family_id,
                       expected_verdict=verdict, witness=witness, review_rationale=None)


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=1, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return _sha(data)


def build_cases(protocol: M.ProtocolV1, out_dir: Path, *, workers: int | None = None,
                dataset_commit: str | None = None) -> M.CaseManifest:
    probes = X.probe_set(protocol.replicate_seeds, protocol.master_seed)
    expected = [X.expected(p) for p in probes]
    raws = enumerate_defects(protocol.master_seed) + enumerate_clean(protocol.master_seed)
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(workers, initializer=_init, initargs=(probes, expected)) as pool:
        validated = pool.map(validate, raws, chunksize=32)
    bench = _bench_source_hashes()
    chosen, excluded = select(validated, protocol, bench)
    with ctx.Pool(workers, initializer=_init, initargs=(probes, expected)) as pool:
        backfill = pool.map(validate, enumerate_test_clean_backfill(protocol.master_seed),
                            chunksize=32)
    chosen, backfill_excluded = repair_test_split(chosen, validated, backfill, protocol, bench)
    picked_ids = {id(r) for r in chosen}
    excluded = [r for r in excluded if id(r) not in picked_ids] + backfill_excluded
    records = [to_record(r, protocol.master_seed) for r in chosen]
    manifest = assign_splits(records, protocol, dataset_commit=dataset_commit or _git_head())
    by_id = {case_id(r): r for r in chosen}
    hashes: dict[str, str] = {}
    src_dir = out_dir / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    for raw in chosen:
        for data in (raw.base, raw.head):
            (src_dir / f"{_sha(data)}.py").write_bytes(data)
    hashes["case-manifest.json"] = _write_json(out_dir / "case-manifest.json",
                                               manifest.model_dump(mode="json"))
    label_files: dict[M.Split, M.LabelFile] = {}
    for split in M.Split:
        labels = [to_label(by_id[c.case_id], probes, len(probes))
                  for c in manifest.cases if c.split is split]
        lf = M.LabelFile(schema_version="research-labels-v1", split=split,
                         sealed=split is M.Split.TEST, manifest_cases_sha256=manifest.cases_sha256,
                         labels=labels)
        label_files[split] = lf
        name = "test.sealed.json" if split is M.Split.TEST else f"{split.value}.json"
        hashes[f"labels/{name}"] = _write_json(out_dir / "labels" / name,
                                               lf.model_dump(mode="json"))
    rows = [{"template_id": r.template_id, "family_id": r.family_id, "split": r.split.value,
             "kind": r.kind.value, "params_sha256": r.params_sha256, "reason": r.reason,
             "cluster": r.cluster or None} for r in excluded]
    exclusions = out_dir / "exclusions.jsonl"
    exclusions.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in rows))
    hashes["exclusions.jsonl"] = _sha(exclusions.read_bytes())
    report = {
        "schema": "track-a-exclusion-report-v1",
        "raw_candidates": len(validated),
        "kept": len(chosen),
        "excluded": len(excluded),
        "excluded_by_reason": dict(sorted(Counter(r.reason for r in excluded).items())),
        "excluded_by_reason_and_split": {
            f"{s}:{reason}": n for (s, reason), n in sorted(
                Counter((r.split.value, r.reason) for r in excluded).items())},
        "notes": ("over_split_target rows are valid cases left out by the deterministic family "
                  "round-robin; cross_split_behavior_duplicate keeps the cluster in the "
                  "highest-priority split (test > validation > train)"),
    }
    hashes["reports/exclusion-report.json"] = _write_json(
        out_dir / "reports" / "exclusion-report.json", report)
    fingerprints = {case_id(r): r.cluster for r in chosen}
    audit = leakage_audit(manifest, label_files, fingerprints,
                          {case_id(r): r.fingerprint for r in chosen}, bench,
                          {case_id(r): S.diff_signature(r.base, r.head) for r in chosen})
    hashes["reports/split-audit.json"] = _write_json(out_dir / "reports" / "split-audit.json",
                                                     audit)
    provenance = {
        "schema": "track-a-provenance-v1",
        "template_registry_version": T.TEMPLATE_REGISTRY_VERSION,
        "template_module_sha256": T._module_sha256(),
        "probe_set_version": X.PROBE_SET_VERSION,
        "probe_inputs": len(probes),
        "probe_set_sha256": M.canonical_sha256([[list(p), d] for p, d in probes]),
        "oracle": "proofhunter.oracle.rounding.rounding_half_up_once",
        "label_rule": ("defect: base equals oracle on every probe (introduced) or base == head "
                       "everywhere (existing); witness = first probe where head != oracle; fresh "
                       "head re-execution in a new namespace must return the same value"),
        "clean_rule": "base and head equal the oracle on every probe; reviewed cosmetic refactor",
        "dataset_commit": manifest.dataset_commit,
        "manifest_cases_sha256": manifest.cases_sha256,
        "master_seed": protocol.master_seed,
        "families": {f.family_id: {"split": f.split.value, "description": f.description}
                     for f in T.FAMILIES},
        "shapes": {s.shape_id: {"split": s.split.value, "rounding": s.rounding,
                                "subtotal": s.subtotal, "numerator": s.numerator}
                   for s in T.SHAPES},
        "artifact_sha256": hashes,
        "source_files": len(list(src_dir.glob("*.py"))),
    }
    _write_json(out_dir / "reports" / "provenance.json", provenance)
    return manifest
