"""``research train-generator``: QLoRA on Runpod (dry-run prints the plan and estimate)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from research.generator.ledger import JsonlLedger
from research.schemas.models import GeneratorExample, Split
from research.training.qlora import SMOKE_CONFIG, QloraTrainer, TrainConfig
from research.training.runpod import GPU_CHOICES, estimate_usd


def read_examples(path: Path, split: Split) -> list[GeneratorExample]:
    rows = [GeneratorExample.model_validate_json(line)
            for line in path.read_text().splitlines() if line.strip()]
    if any(r.split is not split for r in rows):
        raise SystemExit(f"{path} contains records outside the {split.value} split")
    return rows


def train_generator(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="research train-generator")
    ap.add_argument("--train", type=Path, required=True)
    ap.add_argument("--validation", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--gpu", choices=sorted(GPU_CHOICES), default="rtx4090")
    ap.add_argument("--max-hours", type=float, default=3.0)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--ledger", type=Path, default=JsonlLedger().path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(list(argv))
    train = read_examples(args.train, Split.TRAIN)
    val = read_examples(args.validation, Split.VALIDATION)
    config = SMOKE_CONFIG if args.smoke else TrainConfig()
    plan = {"run_id": args.run_id, "gpu": args.gpu, "max_hours": args.max_hours,
            "estimate_usd_max": estimate_usd(args.gpu, args.max_hours), "train": len(train),
            "validation": len(val), "config": config.model_dump(mode="json")}
    print(json.dumps(plan, sort_keys=True))
    if args.dry_run:
        return 0
    trainer = QloraTrainer(run_id=args.run_id, config=config, gpu=args.gpu,
                           max_hours=args.max_hours, ledger=JsonlLedger(args.ledger))
    digest = trainer.train(train, val, args.out)
    print(json.dumps({"adapter_sha256": digest, "out": str(args.out)}))
    return 0
