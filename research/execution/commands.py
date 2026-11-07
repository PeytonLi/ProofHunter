"""``run-pilot`` and ``evaluate`` CLI handlers (offline, local, no paid calls)."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proofhunter.contracts import Strategy, sha256_hex
from research.schemas.interfaces import CaseSources, EpisodeContext, EpisodeState, Ledger, Selector
from research.schemas.models import (
    CostStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerPhase,
    MeasurementMode,
    ProtocolV1,
    RawResultRow,
)

REPO = Path(__file__).resolve().parents[2]
PROTOCOL = REPO / "research" / "spec" / "protocol-v1.json"
ARTIFACTS = REPO / "research" / "artifacts" / "execution"
FIXED_ORDER = (Strategy.BOUNDARY, Strategy.CONSTRAINT, Strategy.SEEDED_FUZZ)
MISSING = 3


class FixedOrderSelector:
    """Spec ``fixed_order``: anchors, then boundary, constraint, seeded_fuzz; ties by hash."""

    arm_id = "fixed_order"

    def reset(self, context: EpisodeContext) -> None:
        del context

    def choose(self, state: EpisodeState) -> str:
        return min(state.available, key=lambda c: (
            not c.is_anchor, FIXED_ORDER.index(c.strategy), c.input_sha256)).input_sha256


class JsonlLedger:
    """Fallback append-only ledger used until ``research.metrics.ledger`` is available."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(LedgerEntry.model_validate(entry.model_dump()).model_dump_json() + "\n")

    def entries(self) -> Sequence[LedgerEntry]:
        if not self.path.exists():
            return []
        return [LedgerEntry.model_validate_json(x) for x in self.path.read_text().splitlines()]


def open_ledger(path: Path) -> Ledger:
    try:
        module = importlib.import_module("research.metrics.ledger")
    except ModuleNotFoundError:
        return JsonlLedger(path)
    ledger: Ledger = module.open_ledger(path)
    return ledger


def load_protocol(path: Path = PROTOCOL) -> ProtocolV1:
    return ProtocolV1.model_validate_json(path.read_text())


def local_compute_entry(run_id: str, phase: LedgerPhase, wall_ms: float, episodes: int
                        ) -> LedgerEntry:
    """Unpaid local execution: recorded for timing, cost measured as 0 USD."""
    return LedgerEntry(
        schema_version="research-ledger-v1",
        entry_id=f"integration:{run_id}", recorded_at=datetime.now(UTC), lane="integration",
        provider="local", category=LedgerCategory.EXECUTION_INFRA, phase=phase,
        resource_id=None, run_id=run_id, case_id=None, quantity=float(episodes),
        unit="episode", unit_price_usd=0.0, cost_usd=0.0, cost_status=CostStatus.MEASURED,
        pricing_reference="local VM subprocess execution; no provider billing",
        pricing_date=datetime.now(UTC).strftime("%Y-%m-%d"), wall_ms=wall_ms, queue_ms=None,
        measurement_mode=MeasurementMode.OFFLINE, paid=False, estimated_job_usd=0.0,
        coordinator_checkin_ref=None, teardown_verified=None)


def bench_plumbing_sources() -> list[CaseSources]:
    """Frozen bench corpus wrapped as cases: verifies execution plumbing, not a research result."""
    from bench.corpus import ROOT, load
    from proofhunter.contracts import canonical_sha256
    from research.schemas.models import CaseRecord, Split, Track

    out = []
    for i, case in enumerate(load(ROOT).cases):
        base, head = (ROOT / case.base_path).read_bytes(), (ROOT / case.head_path).read_bytes()
        out.append(CaseSources(CaseRecord(
            schema_version="research-case-v1", track=Track.A, case_id=f"bench-{case.case_id}",
            split=Split.VALIDATION, family_id="bench_plumbing",
            defect_origin=f"bench:{case.case_id}", near_dup_cluster=f"bench:{case.case_id}",
            template_id="bench-corpus", template_version="1",
            template_sha256=sha256_hex(base + head), params={}, params_sha256=canonical_sha256({}),
            contract_id="checkout-rounding@1", base_source_sha256=sha256_hex(base),
            head_source_sha256=sha256_hex(head), candidate_seed=i), base, head))
    return out


def _store_sources(root: Path, splits: Iterable[str]) -> list[CaseSources]:
    store = importlib.import_module("research.data.store").open_store(root)
    wanted = set(splits)
    return [store.sources(c.case_id) for c in store.manifest().cases if c.split.value in wanted]


def _write(rows: list[RawResultRow], out: Path, run_id: str, extra: dict[str, Any]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    data = "".join(r.model_dump_json() + "\n" for r in rows)
    (out / "raw_results.jsonl").write_text(data)
    verdicts = Counter(f"{r.arm_id}:{r.verdict}" for r in rows)
    summary = {"run_id": run_id, "rows": len(rows),
               "raw_results_sha256": sha256_hex(data.encode()), "verdicts": dict(verdicts),
               "max_invocations_seen": max((r.invocations.base + r.invocations.head
                                            + r.invocations.confirm for r in rows), default=0),
               "failed_invocations": sum(r.invocations.failed for r in rows),
               "cost_usd": sum(r.cost_usd or 0.0 for r in rows),
               "cost_unknown_rows": sum(r.cost_status is CostStatus.UNKNOWN for r in rows),
               **extra}
    path = out / "summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return path


def run_pilot(argv: Sequence[str]) -> int:
    """Reproduction pilot: ``fixed_order`` over reviewed cases, first replicate seed."""
    from research.execution.allowlist import from_bench_corpus
    from research.execution.runner import TrackARunner

    parser = argparse.ArgumentParser(prog="research run-pilot")
    parser.add_argument("--store", type=Path, help="dataset store root (research.data.store)")
    parser.add_argument("--splits", nargs="+", default=["train", "validation"])
    parser.add_argument("--bench-plumbing", action="store_true",
                        help="use the frozen bench corpus to check execution plumbing only")
    parser.add_argument("--seeds", type=int, default=1, help="number of replicate seeds")
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "pilot")
    args = parser.parse_args(argv)
    if "test" in args.splits:
        parser.error("the pilot never touches the sealed test split")
    protocol = load_protocol()
    ledger = open_ledger(args.out / "ledger.jsonl")
    if args.bench_plumbing:
        sources, allowlist = bench_plumbing_sources(), from_bench_corpus()
    elif args.store is not None:
        try:
            sources, allowlist = _store_sources(args.store, args.splits), None
        except ModuleNotFoundError as error:
            print(f"dependency missing: {error.name} (dataset lane)", file=sys.stderr)
            return MISSING
    else:
        parser.error("give --store or --bench-plumbing")
    runner = TrackARunner(protocol=protocol, ledger=ledger, allowlist=allowlist)
    rows = [runner.run(s, FixedOrderSelector(), None, arm_id="fixed_order", seed=seed,
                       repetition=0, mode=MeasurementMode.OFFLINE)
            for seed in protocol.replicate_seeds[:args.seeds] for s in sources]
    run_id = "pilot-" + sha256_hex("".join(r.run_id for r in rows).encode())[:16]
    ledger.append(local_compute_entry(run_id, LedgerPhase.PILOT,
                                      sum(r.timings.wall_ms for r in rows), len(rows)))
    print(_write(rows, args.out, run_id, {
        "scope": "bench_plumbing_not_research_result" if args.bench_plumbing else "dataset",
        "implementation_sha": runner.implementation}))
    return 0


def _selector(arm_id: str, seed: int, checkpoints: Path | None) -> Selector:
    if arm_id == "fixed_order" and checkpoints is None:
        return FixedOrderSelector()
    if arm_id.endswith("_ranker"):
        ranker = importlib.import_module("research.ranker.selector")
        if checkpoints is None:
            raise ValueError("ranker arms need --checkpoints")
        loaded: Selector = ranker.load_ranker_selector(checkpoints / "ranker.txt", arm_id=arm_id)
        return loaded
    baselines = importlib.import_module("research.baselines.selectors")
    made: Selector = baselines.make_selector(arm_id, seed=seed)
    return made


def evaluate(argv: Sequence[str]) -> int:
    """Executing arms x preregistered seeds over a split; emits label-free raw rows."""
    from research.execution.runner import TrackARunner

    parser = argparse.ArgumentParser(prog="research evaluate")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--split", default="validation")
    parser.add_argument("--arms", nargs="+", default=["fixed_order"])
    parser.add_argument("--checkpoints", type=Path)
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "evaluate")
    args = parser.parse_args(argv)
    protocol = load_protocol()
    known = {a.arm_id: a for a in protocol.arms}
    if bad := [a for a in args.arms if a not in known or not known[a].executes]:
        parser.error(f"not executing protocol arms: {bad}")
    if any(known[a].generator is not None for a in args.arms):
        print("generator arms need the generator lane's frozen checkpoints (not wired yet)",
              file=sys.stderr)
        return MISSING
    try:
        sources = _store_sources(args.store, [args.split])
        selectors = {(a, s): _selector(a, s, args.checkpoints)
                     for a in args.arms for s in protocol.replicate_seeds}
    except ModuleNotFoundError as error:
        print(f"dependency missing: {error.name}", file=sys.stderr)
        return MISSING
    ledger = open_ledger(args.out / "ledger.jsonl")
    runner = TrackARunner(protocol=protocol, ledger=ledger)
    rows = [runner.run(src, selectors[(a, s)], None, arm_id=a, seed=s, repetition=0,
                       mode=MeasurementMode.OFFLINE)
            for a in args.arms for s in protocol.replicate_seeds for src in sources]
    run_id = "evaluate-" + sha256_hex("".join(r.run_id for r in rows).encode())[:16]
    ledger.append(local_compute_entry(run_id, LedgerPhase.EVALUATION,
                                      sum(r.timings.wall_ms for r in rows), len(rows)))
    print(_write(rows, args.out, run_id, {"split": args.split,
                                          "implementation_sha": runner.implementation}))
    return 0
