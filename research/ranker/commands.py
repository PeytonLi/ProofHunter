"""CLI handlers for ``collect-observations`` and ``train-ranker`` (dispatched by research.cli)."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from research.schemas import interfaces as I
from research.schemas import models as M

if TYPE_CHECKING:
    from research.ranker.executor import Reviewer

# Heavy (lightgbm/numpy) imports stay inside handlers so research.cli resolves in any env.
ABLATION_CHOICES = ("full", "no_belief", "no_belief_no_predictor", "no_heuristic",
                    "no_diff")


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
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args(list(argv))
    from research.ranker.collect import replicate_seeds

    store, _ = _store(a.store, a.root)
    seeds = [int(s) for s in a.seeds.split(",")] if a.seeds else replicate_seeds()
    split = M.Split(a.split)
    ids = [c.case_id for c in store.manifest().cases if c.split is split]
    chunks = [ids[i:i + 16] for i in range(0, len(ids), 16)]
    job = functools.partial(_collect_chunk, a.store, a.root, a.split, seeds, a.policy)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    digest, stable, n = hashlib.sha256(), hashlib.sha256(), 0
    with a.out.open("w") as fh, ProcessPoolExecutor(max(1, a.workers)) as pool:
        for lines in pool.map(job, chunks):
            for line, key in lines:
                fh.write(line)
                digest.update(line.encode())
                stable.update(key.encode())
                n += 1
    print(json.dumps({"rows": n, "sha256": digest.hexdigest(),
                      "sha256_excluding_wall_ms": stable.hexdigest(), "out": str(a.out)}))
    return 0


def _collect_chunk(store_name: str, root: Path | None, split: str, seeds: list[int],
                   policy: str, case_ids: list[str]) -> list[tuple[str, str]]:
    from research.ranker.collect import collect_cases
    from research.ranker.metrics import stable_json

    store, reviewer = _store(store_name, root)
    allowed: Literal[M.Split.TRAIN, M.Split.VALIDATION] = (
        M.Split.TRAIN if split == "train" else M.Split.VALIDATION)
    return [(row.model_dump_json() + "\n", stable_json(row) + "\n") for row in
            collect_cases(store, case_ids, allowed, seeds, policy, reviewer=reviewer)]


def read_rows(path: Path) -> list[M.RankingObservation]:
    return [M.RankingObservation.model_validate_json(line)
            for line in path.read_text().splitlines() if line.strip()]


def train_ranker(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="research.cli train-ranker")
    ap.add_argument("--train", type=Path, required=True)
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ablation", action="append", choices=ABLATION_CHOICES)
    ap.add_argument("--group-by", choices=("decision", "episode"), default="decision")
    ap.add_argument("--weighting", choices=("none", "ips"), default="none")
    ap.add_argument("--store", choices=("fixture", "dataset"),
                    help="re-featurize logged rows from another feature set via this store")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--grid", choices=("v1", "v2"), default="v1")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args(list(argv))
    from research.ranker.features import FEATURE_SET_VERSION
    from research.ranker.relabel import upgrade
    from research.ranker.train import GRIDS, LambdaRankTrainer

    train, validation = upgrade(read_rows(a.train)), upgrade(read_rows(a.validation))
    if any(r.feature_set_version != FEATURE_SET_VERSION for r in train):
        if a.store is None:
            raise SystemExit("train rows use another feature set; pass --store/--root")
        from research.ranker.relabel import refeaturize

        train = list(refeaturize(_store(a.store, a.root)[0], train))
    result = {}
    for ablation in a.ablation or ["full", "no_belief"]:
        trainer = LambdaRankTrainer(ablation, group_by=a.group_by, weighting=a.weighting,
                                    grid=GRIDS[a.grid], jobs=a.jobs)
        result[ablation] = {"checkpoint_sha256": trainer.train(train, validation,
                                                                a.out / ablation),
                            "validation": trainer.report["validation_metrics"]}
    print(json.dumps(result, sort_keys=True))
    return 0
