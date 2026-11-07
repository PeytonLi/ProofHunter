"""Dataset-lane CLI commands: ``inspect-dataset`` and ``prepare-splits``."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from research.data import observations, sft
from research.data.build import build_cases
from research.data.store import open_store
from research.data.swe_smith import prepare_swe_smith_manifest
from research.schemas import models as M

DEFAULT_OUT = Path("research/artifacts/dataset")
DEFAULT_PROTOCOL = Path("research/spec/protocol-v1.json")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_splits(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="prepare-splits")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--skip-learning-data", action="store_true")
    parser.add_argument("--swe-smith-source", type=Path, default=None,
                        help="metadata directory (metadata.jsonl, shards.json, pins.json, "
                             "split_proposal.json); metadata only, nothing is executed")
    args = parser.parse_args(list(argv))
    protocol = M.ProtocolV1.model_validate_json(args.protocol.read_bytes())
    root = args.out / "track_a"
    manifest = build_cases(protocol, root, workers=args.workers)
    print(f"track_a cases={len(manifest.cases)} cases_sha256={manifest.cases_sha256}")
    if not args.skip_learning_data:
        store = open_store(root)
        summary: dict[str, object] = {"schema": "track-a-learning-data-v1",
                                      "collection_policy": observations.POLICY,
                                      "feature_set_version": observations.FEATURE_SET,
                                      "prompt_format_version": sft.PROMPT_FORMAT,
                                      "behavior_spec_sha256": sft.BEHAVIOR_SPEC_SHA256}
        for split in (M.Split.TRAIN, M.Split.VALIDATION):
            rows = observations.collect(store, split, protocol.replicate_seeds)
            obs_path = root / "observations" / f"{split.value}.jsonl"
            observations.write(rows, obs_path)
            examples = sft.build_examples(store, split)
            gen_path = root / "generator" / f"{split.value}.jsonl"
            sft.write(examples, gen_path)
            per_case = Counter(r.case_id for r in rows)
            summary[split.value] = {
                "observations": len(rows), "observation_cases": len(per_case),
                "max_rows_per_case": max(per_case.values()),
                "relevance": dict(Counter(r.relevance.value for r in rows)),
                "exploration_rows": sum(r.exploration for r in rows),
                "observations_sha256": _sha(obs_path),
                "generator_examples": len(examples), "generator_sha256": _sha(gen_path)}
            print(f"{split.value}: observations={len(rows)} generator_examples={len(examples)}")
        report = root / "reports" / "learning-data.json"
        report.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    if args.swe_smith_source is not None:
        tasks = prepare_swe_smith_manifest(args.swe_smith_source, args.out / "track_b")
        print(f"track_b tasks={len(tasks)}")
    return 0


def inspect_dataset(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="inspect-dataset")
    parser.add_argument("--root", type=Path, default=DEFAULT_OUT / "track_a")
    args = parser.parse_args(list(argv))
    store = open_store(args.root)
    manifest = store.manifest()
    for case in manifest.cases:
        store.sources(case.case_id)
    out = {
        "manifest_version": manifest.manifest_version,
        "cases_sha256": manifest.cases_sha256,
        "dataset_commit": manifest.dataset_commit,
        "cases_per_split": dict(Counter(c.split.value for c in manifest.cases)),
        "families_per_split": {s.value: len({c.family_id for c in manifest.cases if c.split is s})
                               for s in M.Split},
        "label_kinds": {s: dict(Counter(lab.kind.value for lab in store.labels(s)))
                        for s in ("train", "validation")},
        "test_labels": "sealed",
        "sources_verified": True,
    }
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0
