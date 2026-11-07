"""CLI handlers: ``run-pilot`` and ``evaluate`` (dispatched by the frozen ``research/cli.py``)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from research.execution import pilot
from research.execution.lanes import (
    ArmFactory,
    FrozenArtifacts,
    LaneMissing,
    open_ledger,
    open_store,
)
from research.execution.runner import (
    REPO,
    ResearchEpisodeRunner,
    implementation_sha,
    make_runner,
)
from research.schemas.interfaces import CaseStore
from research.schemas.models import (
    LedgerPhase,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
    Split,
)

PROTOCOL = REPO / "research" / "spec" / "protocol-v1.json"
ARTIFACTS = REPO / "research" / "artifacts" / "execution"
PILOT_ARMS = ("fixed_order", "uniform_random", "simple_heuristic", "builtin_eig")


def load_protocol(path: Path = PROTOCOL) -> ProtocolV1:
    return ProtocolV1.model_validate_json(path.read_text(encoding="utf-8"))


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", type=Path, help="dataset store root (research.data.store)")
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mode", choices=[m.value for m in MeasurementMode], default="offline")
    parser.add_argument("--arms", help="comma-separated arm ids (default: see command)")
    parser.add_argument("--estimate", action="append", default=[], metavar="ARM=USD",
                        help="pre-launch job estimate for a paid arm (required for live arms)")
    parser.add_argument("--doubles", action="store_true",
                        help="integration self-test: reviewed bench corpus + selector doubles")


def _estimates(items: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        arm, _, usd = item.partition("=")
        out[arm] = float(usd)
    return out


def _setup(
    args: argparse.Namespace, split: Split
) -> tuple[ProtocolV1, Path, CaseStore, ResearchEpisodeRunner, pilot.Factory]:
    protocol = load_protocol(args.protocol)
    out = args.out or ARTIFACTS / f"{args.command}-{_stamp()}"
    out.mkdir(parents=True, exist_ok=True)
    ledger = open_ledger(out / "ledger.jsonl")
    store: CaseStore
    runner: ResearchEpisodeRunner
    factory: pilot.Factory
    if args.doubles:
        from research.execution import doubles

        store = doubles.BenchStore(split)
        runner = doubles.offline_runner(protocol, isolated=True)
        runner.ledger = ledger
        runner.implementation = implementation_sha()
        factory = ArmFactory(
            FrozenArtifacts.load(getattr(args, "config", None)), ledger=ledger,
            mode=MeasurementMode(args.mode),
            phase=LedgerPhase.PILOT if args.command == "pilot" else LedgerPhase.EVALUATION,
            estimated_job_usd=_estimates(args.estimate))
    else:
        if args.store is None:
            raise LaneMissing("--store is required (or --doubles for the self-test)")
        store = open_store(args.store)
        runner = make_runner(protocol, ledger)
        factory = ArmFactory(
            FrozenArtifacts.load(getattr(args, "config", None)), ledger=ledger,
            mode=MeasurementMode(args.mode),
            phase=LedgerPhase.PILOT if args.command == "pilot" else LedgerPhase.EVALUATION,
            estimated_job_usd=_estimates(args.estimate))
    addons = getattr(getattr(factory, "artifacts", None), "addon_arms", None) or {}
    runner.addons = {arm_id: spec["base"] for arm_id, spec in addons.items()}
    return protocol, out, store, runner, factory


def run_pilot(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="research run-pilot")
    _common(parser)
    parser.add_argument("--split", choices=["train", "validation"], default="validation")
    parser.add_argument("--cases", type=int, default=50, help="maximum pilot cases (<= 50)")
    parser.add_argument("--smoke", action="store_true", help="3 cases, 1 seed")
    parser.add_argument("--seeds", type=int, default=1, help="number of preregistered seeds")
    parser.add_argument("--config", type=Path, help="frozen artifacts JSON for model arms")
    args = parser.parse_args(argv)
    args.command = "pilot"
    if not 1 <= args.cases <= 50 or not 1 <= args.seeds <= 5:
        parser.error("--cases must be 1..50 and --seeds 1..5")
    try:
        protocol, out, store, runner, factory = _setup(args, Split(args.split))
        seeds = protocol.replicate_seeds[: 1 if args.smoke else args.seeds]
        arms = args.arms.split(",") if args.arms else list(PILOT_ARMS)
        report = pilot.run(store, protocol, runner, factory, arms=arms,
                           split=Split(args.split), max_cases=3 if args.smoke else args.cases,
                           seeds=seeds, mode=MeasurementMode(args.mode), out=out)
    except (LaneMissing, ValueError) as error:
        print(f"run-pilot: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"out": str(out), "cases": report["cases"],
                      "arms": sorted(report["arms"]), "skipped": report["skipped_arms"],
                      "rows_sha256": report["rows_sha256"]}, sort_keys=True))
    return 0


def evaluate(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="research evaluate")
    _common(parser)
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    parser.add_argument("--config", type=Path, required=True,
                        help="frozen artifacts JSON (checkpoint/adapter sha256, model ids, arms)")
    parser.add_argument("--max-cases", type=int, help="development cap (never with --split test)")
    args = parser.parse_args(argv)
    args.command = "evaluate"
    if args.split == "test" and args.max_cases is not None:
        parser.error("the sealed test split runs in full")
    try:
        artifacts = FrozenArtifacts.load(args.config)
        protocol, out, store, runner, factory = _setup(args, Split(args.split))
        arm_ids = (args.arms.split(",") if args.arms else list(artifacts.arms or [])
                   or [a.arm_id for a in protocol.arms])
        specs = runner.arm_specs()
        if set(arm_ids) - set(specs):
            raise ValueError(f"arms not in protocol: {sorted(set(arm_ids) - set(specs))}")
        cases = sorted((c for c in store.manifest().cases
                        if c.split.value == args.split), key=lambda c: c.case_id)
        cases = cases[: args.max_cases] if args.max_cases else cases
        rows: list[RawResultRow] = []
        skipped: dict[str, str] = {}
        degraded: list[dict[str, str | None]] = []
        for arm_id in arm_ids:
            spec = specs[arm_id]
            try:
                generator = factory.generator(spec)
                factory.selector(spec, protocol.replicate_seeds[0])
            except (LaneMissing, NotImplementedError) as error:
                skipped[arm_id] = str(error)
                continue
            for case in cases:
                sources = store.sources(case.case_id)
                for repetition, seed in enumerate(protocol.replicate_seeds):
                    selector = factory.selector(spec, seed)
                    rows.append(runner.run(
                        sources, selector, generator, arm_id=arm_id, seed=seed,
                        repetition=repetition, mode=MeasurementMode(args.mode)))
                    degraded.append({"run_id": rows[-1].run_id,
                                     "degraded": pilot.degraded_reason(selector, generator)})
    except (LaneMissing, ValueError) as error:
        print(f"evaluate: {error}", file=sys.stderr)
        return 1
    pilot.write_degraded(out / "degraded.jsonl", degraded)
    rows_path = out / "rows.jsonl"
    rows_path.write_text("".join(r.model_dump_json() + "\n" for r in rows), encoding="utf-8")
    (out / "events.jsonl").write_text("".join(
        json.dumps(e, sort_keys=True) + "\n" for e in runner.events), encoding="utf-8")
    manifest = {
        "schema": "research-evaluation-run-v1", "protocol_version": protocol.schema_version,
        "implementation_sha": runner.implementation,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "split": args.split, "measurement_mode": args.mode, "training": "disabled",
        "seeds": protocol.replicate_seeds, "cases": len(cases), "arms_run": sorted(
            set(arm_ids) - set(skipped)), "arms_skipped": skipped, "rows": len(rows),
        "rows_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
    }
    (out / "run-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"out": str(out), **{k: manifest[k] for k in (
        "rows", "rows_sha256", "arms_run", "arms_skipped")}}, sort_keys=True))
    return 0 if rows else 1
