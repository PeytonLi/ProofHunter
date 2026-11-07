"""CLI handlers for ``collect-observations`` and ``train-ranker`` (dispatched by research.cli)."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from research.schemas import interfaces as I
from research.schemas import models as M

if TYPE_CHECKING:
    from research.ranker.executor import Reviewer

# Heavy (lightgbm/numpy) imports stay inside handlers so research.cli resolves in any env.
ABLATION_CHOICES = ("full", "no_belief", "no_belief_no_predictor")


def _store(name: str, root: Path | None) -> tuple[I.CaseStore, Reviewer]:
    from research.ranker.executor import verify_reviewed

    if name == "fixture":
        from research.ranker.fixture import FixtureStore, verify_fixture
        return FixtureStore(), verify_fixture
    if root is None:
        raise SystemExit("--root is required for --store dataset")
    from research.data.store import open_store  # type: ignore[import-not-found,import-untyped,unused-ignore]
    store: I.CaseStore = open_store(root)
    return store, verify_reviewed


def collect_observations(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="research.cli collect-observations")
    ap.add_argument("--store", choices=("fixture", "dataset"), default="fixture")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--split", choices=("train", "validation"), required=True)
    ap.add_argument("--policy", default="eig_eps0.3")
    ap.add_argument("--seeds", help="comma-separated; default: protocol replicate seeds")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(list(argv))
    from research.ranker.collect import collect_observations as _collect
    from research.ranker.collect import replicate_seeds

    store, reviewer = _store(a.store, a.root)
    seeds = [int(s) for s in a.seeds.split(",")] if a.seeds else replicate_seeds()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    digest, n = hashlib.sha256(), 0
    with a.out.open("w") as fh:
        for row in _collect(store, M.Split(a.split), seeds, a.policy, reviewer=reviewer):
            line = row.model_dump_json() + "\n"
            fh.write(line)
            digest.update(line.encode())
            n += 1
    print(json.dumps({"rows": n, "sha256": digest.hexdigest(), "out": str(a.out)}))
    return 0


def read_rows(path: Path) -> list[M.RankingObservation]:
    return [M.RankingObservation.model_validate_json(line)
            for line in path.read_text().splitlines() if line.strip()]


def train_ranker(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="research.cli train-ranker")
    ap.add_argument("--train", type=Path, required=True)
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ablation", action="append", choices=ABLATION_CHOICES)
    a = ap.parse_args(list(argv))
    from research.ranker.train import LambdaRankTrainer

    train, validation = read_rows(a.train), read_rows(a.validation)
    result = {}
    for ablation in a.ablation or ["full", "no_belief"]:
        trainer = LambdaRankTrainer(ablation)
        result[ablation] = {"checkpoint_sha256": trainer.train(train, validation,
                                                                a.out / ablation),
                            "validation": trainer.report["validation_metrics"]}
    print(json.dumps(result, sort_keys=True))
    return 0
