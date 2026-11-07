"""Live smoke for the baseline arms on reviewed Track A demo revisions (pilot phase).

Runs only reviewed ``fixtures/demo`` template code in-process (never dataset/target code). The
episode loop is a minimal local stand-in for the measurement lane's ``EpisodeRunner``: shared
production candidate bank, 6 paired probes, coverage guard, stop at first head mismatch.

    uv run --project research python -m research.baselines.smoke --live
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Literal

from fixtures import catalog
from fixtures.reference import oracle_expected
from proofhunter import bayes
from proofhunter import contracts as c
from proofhunter.meta import build_request
from research.baselines import frontier, meta, selectors
from research.baselines.config import load_models
from research.baselines.ledger import JsonlLedger, model_call_entry
from research.schemas.interfaces import (
    EpisodeContext,
    EpisodeState,
    Ledger,
    PairFeedback,
    Selector,
)
from research.schemas.models import (
    ContextFile,
    CostStatus,
    GeneratorInput,
    LedgerEntry,
    LedgerPhase,
    MeasurementMode,
    Outcome,
)

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "baselines"
REQUIRED = tuple(c.CoverageTag(t) for t in
                 ("empty", "full_discount", "half_cent", "multi_item", "zero_discount"))
MAX_PAIRS = 6
SEEDS = (1633385816, 3469185555)
CASES = (("buggy", SEEDS[0]), ("half_even", SEEDS[0]), ("fixed", SEEDS[0]), ("buggy", SEEDS[1]))
ESTIMATED_JOB_USD = 2.0
INTERFACE: Final = "checkout.total_cents(prices_cents, discount_percent)"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_ph_smoke_{path.parent.name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_context(case: str, seed: int, plan: c.HypothesisPlanPayload) -> EpisodeContext:
    pr = catalog.PR_CASES[case]
    base, head = pr.base_revision.source_bytes, pr.head_revision.source_bytes
    import difflib

    diff = "".join(difflib.unified_diff(
        base.decode().splitlines(keepends=True), head.decode().splitlines(keepends=True),
        fromfile="a/checkout.py", tofile="b/checkout.py"))
    bank = bayes.generate(selectors.plan_from_payload(c.builtin_hypothesis_payload()), seed)
    return EpisodeContext(case_id=f"demo-{case}-{seed}", seed=seed, base_source=base,
                          head_source=head, unified_diff=diff, bank=bank, plan=plan)


def generator_input(ctx: EpisodeContext) -> GeneratorInput:
    sides: tuple[tuple[Literal["base", "head"], bytes], ...] = (
        ("base", ctx.base_source), ("head", ctx.head_source))
    files = [ContextFile(side=side, path="checkout.py", sha256=c.sha256_hex(src),
                         content=src.decode()) for side, src in sides]
    return GeneratorInput(prompt_format_version="gen-prompt-v1",
                          contract_id="checkout-rounding@1",
                          behavior_spec_sha256=catalog.contract_sha256(),
                          allowed_test_interface=INTERFACE,
                          context_files=files, unified_diff=ctx.unified_diff)


def run_episode(selector: Selector, ctx: EpisodeContext, case: str) -> dict[str, Any]:
    pr = catalog.PR_CASES[case]
    base_fn = _load(pr.base_revision.path).total_cents
    head_fn = _load(pr.head_revision.path).total_cents
    selector.reset(ctx)
    history: list[PairFeedback] = []
    seen: set[str] = set()
    covered: set[c.CoverageTag] = set()
    found: int | None = None
    by_sha = {x.input_sha256: x for x in ctx.bank.candidates}
    start = time.monotonic()
    for step in range(1, MAX_PAIRS + 1):
        remaining = MAX_PAIRS - step + 1
        missing = [t for t in REQUIRED if t not in covered]
        pool = [x for x in ctx.bank.candidates if x.input_sha256 not in seen]
        if missing and remaining <= len(missing):
            guarded = [x for x in pool if set(missing) & set(x.coverage_tags)]
            pool = guarded or pool
        if not pool:
            break
        sha = selector.choose(EpisodeState(step=step, remaining_pairs=remaining, available=pool,
                                           missing_coverage=missing, history=list(history)))
        if sha not in {x.input_sha256 for x in pool}:
            raise RuntimeError(f"{selector.arm_id} chose a candidate outside state.available")
        cand = by_sha[sha]
        seen.add(sha)
        covered |= set(cand.coverage_tags)
        prices, disc = list(cand.prices_cents), cand.discount_percent
        expected, b, h = oracle_expected(prices, disc), base_fn(prices, disc), head_fn(prices, disc)
        outcome = (Outcome.BASE_MISMATCH if b != expected else
                   Outcome.MISMATCH if h != expected else Outcome.MATCH)
        history.append(PairFeedback(step=step, candidate_input_sha256=sha, outcome=outcome,
                                    expected_cents=expected, base_cents=b, head_cents=h))
        if outcome is Outcome.MISMATCH:
            found = step
            break
    return {"arm_id": selector.arm_id, "case_id": ctx.case_id,
            "expected_verdict": pr.expected_verdict, "pairs": len(history),
            "first_mismatch_step": found,
            "outcomes": [x.outcome.value for x in history],
            "degraded": getattr(selector, "degraded", None),
            "episode_wall_ms": round((time.monotonic() - start) * 1000, 1)}


def _usage(sel: Selector) -> dict[str, Any]:
    client = getattr(sel, "client", None)
    if client is None:
        return {}
    u = client.usage
    return {"model_calls": u.calls, "input_tokens": u.input_tokens,
            "cached_tokens": u.cached_tokens, "output_tokens": u.output_tokens,
            "cost_usd": round(u.cost_usd, 6), "provider_wall_ms": round(u.wall_ms, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default=load_models()["frontier"]["primary_model_id"])
    parser.add_argument("--out", type=Path, default=ARTIFACTS)
    parser.add_argument("--case", action="append", choices=sorted({x for x, _ in CASES}))
    parser.add_argument("--tag", default="")
    parser.add_argument("--max-cases", type=int, default=len(CASES))
    parser.add_argument("--meta-only", action="store_true",
                        help="skip the frontier arms (Meta + deterministic arms only)")
    parser.add_argument("--meta-arm", action="append", default=[],
                        help="restrict to these Meta arm ids (default: all enabled)")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    ledger: Ledger = JsonlLedger(args.out / "ledger.jsonl")
    mode = MeasurementMode.LIVE if args.live else MeasurementMode.OFFLINE
    common: dict[str, Any] = {"ledger": ledger, "phase": LedgerPhase.PILOT, "mode": mode}
    rows: list[dict[str, Any]] = []
    builtin = c.builtin_hypothesis_payload()
    cases = [x for x in CASES if not args.case or x[0] in args.case][: args.max_cases]
    for case, seed in cases:
        ctx = make_context(case, seed, builtin)
        arms: list[tuple[Selector, EpisodeContext]] = [
            (selectors.make_selector(a, seed=seed), ctx) for a in
            ("fixed_order", "uniform_random", "simple_heuristic", "builtin_eig")]
        meta_rows: dict[str, dict[str, Any]] = {}
        if args.live:
            for arm_id, model_id in meta.meta_arms().items():
                if args.meta_arm and arm_id not in args.meta_arm:
                    continue
                gen = meta.meta_generator(ledger=ledger, model_id=model_id)
                assert isinstance(gen, meta.MetaGenerator)
                gen.case_id, gen.estimated_job_usd = ctx.case_id, ESTIMATED_JOB_USD
                t0 = time.monotonic()
                plan = gen.propose(generator_input(ctx))
                last = gen.last
                meta_rows[arm_id] = {
                    "meta_model_id": gen.model,
                    "meta_wall_ms": round((time.monotonic() - t0) * 1000, 1),
                    "meta_mode": last.mode.value if last else None,
                    "meta_attempts": last.attempts if last else 0,
                    "meta_reason": last.reason_code.value if last and last.reason_code
                    else None, "meta_hypotheses": [h.mode.value for h in plan.hypotheses]}
                arms.append((selectors.make_selector(arm_id, seed=seed),
                             make_context(case, seed, plan)))
            for factory in () if args.meta_only else (frontier.frontier_reviewer,
                                                       frontier.frontier_agent):
                sel = factory(args.model, estimated_job_usd=ESTIMATED_JOB_USD, **common)
                arms.append((sel, ctx))
        for sel, sctx in arms:
            row = run_episode(sel, sctx, case) | _usage(sel)
            row |= meta_rows.get(sel.arm_id, {})
            if sel.arm_id.startswith("frontier"):
                row["frontier_model_id"] = args.model
            rows.append(row)
            print(json.dumps(row, sort_keys=True))
    report = {"generated_at": datetime.now(UTC).isoformat(), "measurement_mode": mode.value,
              "phase": "pilot", "frontier_model_id": args.model, "cases": [list(x) for x in cases],
              "rows": rows}
    out = args.out / f"smoke-{mode.value}{args.tag}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


def record_exploratory_call(ledger: Ledger, cost_usd: float, model_id: str) -> None:
    """One-off: the pre-implementation Responses API shape check (no usage retained)."""
    cfg = load_models()["frontier"]
    ledger.append(model_call_entry(
        provider="openai", unit="tokens", quantity=0.0, cost_usd=cost_usd,
        cost_status=CostStatus.ESTIMATED,
        pricing_reference=f"{cfg['pricing_reference']}; model={model_id}; upper bound, usage "
        "not retained", pricing_date=cfg["pricing_date"], wall_ms=None, estimated_job_usd=0.01,
        phase=LedgerPhase.PILOT, mode=MeasurementMode.LIVE, run_id=None, case_id=None))


def reprice_meta_rows(path: Path) -> int:
    """Price legacy Meta rows recorded as ``unknown`` (no usage kept) at the upper bound.

    Rewrites ``path`` in place (same entry ids); returns the number of rows repriced.
    """
    cfg = load_models()["meta"]
    builtin = c.builtin_hypothesis_payload()
    entries = [LedgerEntry.model_validate_json(line)
               for line in path.read_text().splitlines() if line.strip()]
    repriced = 0
    out = []
    for entry in entries:
        if (entry.provider == "meta" and entry.cost_status is CostStatus.UNKNOWN
                and entry.unit == "http_attempts" and entry.case_id):
            _, case, seed = entry.case_id.split("-")
            ctx = make_context(case, int(seed), builtin)
            body = build_request(meta.planning_context(generator_input(ctx)), cfg["model_id"])
            usage = meta.AttemptUsage(request_bytes=len(json.dumps(body).encode()))
            price = cfg["pricing_usd_per_1m_tokens"][cfg["model_id"]]
            entry = entry.model_copy(update={
                "cost_usd": entry.quantity * meta.attempt_cost(usage, price),
                "cost_status": CostStatus.ESTIMATED,
                "pricing_reference": f"{cfg['pricing_reference']}; model={cfg['model_id']}; "
                "repriced 2026-11-07, no usage recorded: upper bound per attempt "
                "(input=ceil(request_bytes/3), output=max_completion_tokens)"})
            entry = LedgerEntry.model_validate_json(entry.model_dump_json())
            repriced += 1
        out.append(entry.model_dump_json())
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(line + "\n" for line in out))
    tmp.replace(path)
    return repriced


Main = Callable[[list[str] | None], int]
if __name__ == "__main__":
    raise SystemExit(main())
