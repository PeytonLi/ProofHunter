"""``research prepare-generator-examples``: build train/validation GeneratorExample JSONL."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Sequence
from pathlib import Path

from research.generator.examples import SplitRefused, build_examples
from research.generator.model import sha256_file
from research.schemas.interfaces import CaseStore
from research.schemas.models import GeneratorExample, Split


def open_store(factory: str, root: Path) -> CaseStore:
    module, _, name = factory.partition(":")
    store = getattr(importlib.import_module(module), name)(root)
    if not isinstance(store, CaseStore):
        raise SystemExit(f"{factory} did not return a CaseStore")
    return store


def prepare_generator_examples(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="research prepare-generator-examples")
    ap.add_argument("--store", type=Path, required=True, help="dataset lane case store root")
    ap.add_argument("--store-factory", default="research.data.store:open_store")
    ap.add_argument("--split", choices=[Split.TRAIN.value, Split.VALIDATION.value],
                    required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(list(argv))
    store = open_store(args.store_factory, args.store)
    try:
        rows = list(build_examples(store, Split(args.split)))
    except SplitRefused as exc:
        raise SystemExit(str(exc)) from None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(r.model_dump_json() + "\n" for r in rows))
    for line in args.out.read_text().splitlines():
        GeneratorExample.model_validate_json(line)
    print(json.dumps({"split": args.split, "examples": len(rows), "out": str(args.out),
                      "sha256": sha256_file(args.out)}))
    return 0
