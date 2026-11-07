"""``python -m research.verification`` - run every audit over discovered artifacts.

Artifacts are classified by their ``schema_version`` field, so the verifier needs no
knowledge of other lanes' file layouts. Reports go to ``research/artifacts/verification/``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from research.verification import budget, ledger, metrics, provenance, schema, splits, teardown
from research.verification.artifacts import iter_rows, sha256_file
from research.verification.gates import GateResult, render_markdown

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "research" / "spec"
MODEL_OF = {
    "research-case-manifest-v1": "CaseManifest", "research-labels-v1": "LabelFile",
    "research-ranking-observation-v1": "RankingObservation",
    "research-generator-example-v1": "GeneratorExample",
    "research-raw-result-v1": "RawResultRow", "research-ledger-v1": "LedgerEntry",
    "research-swe-smith-task-v1": "SweSmithTask",
}


def discover(roots: Sequence[Path]) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    found: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.json*"))
        for path in files:
            if path.resolve().is_relative_to(REPO / "research/artifacts/verification"):
                continue
            if path.suffix not in (".json", ".jsonl"):
                continue
            try:
                data = json.loads(path.read_text()) if path.suffix == ".json" else None
                rows = [data] if isinstance(data, dict) and "schema_version" in data else list(
                    iter_rows(path))
            except (ValueError, UnicodeDecodeError):
                continue
            for row in rows:
                model = MODEL_OF.get(str(row.get("schema_version")))
                if model:
                    found[model].append((path, row))
    return found


def production_hashes(protocol: dict[str, Any]) -> GateResult:
    gate = GateResult("production_frozen")
    for rel, want in protocol["production_lock_sha256"].items():
        got = sha256_file(REPO / rel)
        if got != want:
            gate.error("production_drift", f"{got} != {want}", rel)
    return gate


def run_all(roots: Sequence[Path], *, reported: Path | None, live: bool) -> list[GateResult]:
    protocol = json.loads((SPEC / "protocol-v1.json").read_text())
    policy = json.loads((SPEC / "spend-policy.json").read_text())
    found = discover(roots)
    results = [production_hashes(protocol), schema.audit_frozen_schemas(),
               schema.audit_file(SPEC / "protocol-v1.json", "ProtocolV1"),
               schema.audit_file(SPEC / "spend-policy.json", "SpendPolicy")]
    for model, items in sorted(found.items()):
        results.append(schema.audit_rows([r for _, r in items], model))
    manifests = [r for _, r in found.get("CaseManifest", [])]
    cases: list[dict[str, Any]] = manifests[-1]["cases"] if manifests else []
    case_map = {c["case_id"]: c for c in cases}
    label_files = {lf["split"]: lf for _, lf in found.get("LabelFile", [])}
    labels = {lab["case_id"]: lab for lf in label_files.values() for lab in lf["labels"]}
    if manifests:
        from bench.corpus import load as load_bench

        bench = load_bench()
        bench_hashes = {h for c in bench.cases for h in (c.base_sha256, c.head_sha256)}
        results.append(splits.audit_manifest_splits(cases, protocol=protocol, labels=labels,
                                                    bench_hashes=bench_hashes))
        results.append(provenance.audit_label_files(cases, label_files,
                                                    manifests[-1]["cases_sha256"]))
        train_paths = sorted({p for m in ("RankingObservation", "GeneratorExample")
                              for p, _ in found.get(m, [])})
        if train_paths:
            results.append(splits.audit_training_artifacts(cases, train_paths))
    else:
        results.append(GateResult("split_leakage"))
        results[-1].error("no_manifest", "no CaseManifest found")
    obs = [r for _, r in found.get("RankingObservation", [])]
    if obs:
        results.append(splits.audit_rows_against_manifest(obs, cases, name="observations"))
        results.append(provenance.audit_observations(obs, labels))
    gens = [r for _, r in found.get("GeneratorExample", [])]
    if gens:
        pos: dict[str, set[str]] = defaultdict(set)
        for o in obs:
            if o.get("relevance") == "positive":
                pos[o["case_id"]].add(o["candidate"]["input_sha256"])
        results.append(splits.audit_rows_against_manifest(gens, cases, name="generator"))
        results.append(provenance.audit_generator_examples(gens, cases, labels, pos))
    swe = [r for _, r in found.get("SweSmithTask", [])]
    if swe:
        results.append(splits.audit_swe_smith_splits(swe))
    raw = [r for _, r in found.get("RawResultRow", [])]
    if raw:
        results.append(budget.audit_raw_results(raw, protocol, cases=case_map or None))
        computed = metrics.recompute(raw, labels, case_map, protocol)
        out = REPO / "research/artifacts/verification"
        out.mkdir(parents=True, exist_ok=True)
        (out / "recomputed_metrics.json").write_text(json.dumps(computed, indent=2,
                                                                sort_keys=True))
        if reported:
            results.append(metrics.compare_reported(computed, json.loads(reported.read_text())))
    entries = [r for _, r in found.get("LedgerEntry", [])]
    results.append(ledger.audit_ledger(entries, policy, raw_rows=raw))
    results.append(teardown.audit_ledger_teardown(entries, policy))
    if live:
        listing = teardown.live_listing()
        out = REPO / "research/artifacts/verification"
        out.mkdir(parents=True, exist_ok=True)
        (out / "teardown_listing.json").write_text(json.dumps(listing, indent=2))
        results.append(teardown.audit_live_listing(listing))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m research.verification")
    p.add_argument("roots", nargs="*", type=Path, default=[REPO / "research/artifacts"])
    p.add_argument("--reported", type=Path, help="reported metrics JSON to recompute")
    p.add_argument("--live-teardown", action="store_true", help="read-only provider listings")
    p.add_argument("--out", type=Path, default=REPO / "research/artifacts/verification")
    args = p.parse_args(argv)
    results = run_all(args.roots, reported=args.reported, live=args.live_teardown)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps([r.to_dict() for r in results], indent=2,
                                                     default=str))
    (args.out / "report.md").write_text(render_markdown(results))
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.gate}")
    return 0 if all(r.passed for r in results) else 1
