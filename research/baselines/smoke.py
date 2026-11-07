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
from research.baselines import frontier, grid, meta, selectors
from research.baselines.config import load_models
from research.baselines.ledger import JsonlLedger, Usage, model_call_entry
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


def _usage(u: Usage | None) -> dict[str, Any]:
    if u is None:
        return {}
    return {"model_calls": u.calls, "input_tokens": u.input_tokens,
            "cached_tokens": u.cached_tokens, "output_tokens": u.output_tokens,
            "cost_usd": round(u.cost_usd, 6), "provider_wall_ms": round(u.wall_ms, 1)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per grid arm: cases, cost, provider latency, calls and degraded reasons."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if "role" not in row:
            continue
        s = out.setdefault(row["arm_id"], {"cases": 0, "cost_usd": 0.0, "model_calls": 0,
                                           "provider_wall_ms": [], "pairs": [],
                                           "degraded": {}})
        s["cases"] += 1
        s["cost_usd"] += row.get("cost_usd", 0.0)
        s["model_calls"] += row.get("model_calls", 0)
        s["provider_wall_ms"].append(row.get("provider_wall_ms", 0.0))
        s["pairs"].append(row["pairs"])
        if row.get("degraded"):
            s["degraded"][row["degraded"]] = s["degraded"].get(row["degraded"], 0) + 1
    for s in out.values():
        walls = s.pop("provider_wall_ms")
        s["cost_usd"] = round(s["cost_usd"], 6)
        s["mean_cost_usd_per_case"] = round(s["cost_usd"] / s["cases"], 6)
        s["provider_wall_ms_min_mean_max"] = [round(min(walls), 1),
                                              round(sum(walls) / len(walls), 1),
                                              round(max(walls), 1)]
    return out


def grid_row(arm: str, case: str, seed: int, ctx: EpisodeContext,
             common: dict[str, Any]) -> dict[str, Any]:
    role, model = grid.parse_arm(arm)
    kw = {"case_id": ctx.case_id, "estimated_job_usd": ESTIMATED_JOB_USD, **common}
    tags = {"role": role, "model_id": model, "provider": frontier.provider_of(model)}
    if role == "generator":
        gen = grid.make_generator(arm, **kw)
        plan = gen.propose(generator_input(ctx))
        sel = grid.make_arm_selector(arm, seed=seed)
        row = run_episode(sel, make_context(case, seed, plan), case)
        row["degraded"] = getattr(gen, "degraded", None)
        row["hypotheses"] = [h.mode.value for h in plan.hypotheses]
        return row | _usage(getattr(gen, "usage", None)) | tags
    sel = grid.make_arm_selector(arm, seed=seed, **kw)
    assert isinstance(sel, frontier.FrontierReviewer | frontier.FrontierAgent)
    return run_episode(sel, ctx, case) | _usage(sel.client.usage) | tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", type=Path, default=ARTIFACTS)
    parser.add_argument("--case", action="append", choices=sorted({x for x, _ in CASES}))
    parser.add_argument("--tag", default="")
    parser.add_argument("--max-cases", type=int, default=len(CASES))
    parser.add_argument("--arm", action="append", default=[],
                        help="grid arms <role>:<model> (default: every non-skipped cell)")
    parser.add_argument("--no-deterministic", action="store_true")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    ledger: Ledger = JsonlLedger(args.out / "ledger.jsonl")
    mode = MeasurementMode.LIVE if args.live else MeasurementMode.OFFLINE
    common: dict[str, Any] = {"ledger": ledger, "phase": LedgerPhase.PILOT, "mode": mode}
    rows: list[dict[str, Any]] = []
    builtin = c.builtin_hypothesis_payload()
    cases = [x for x in CASES if not args.case or x[0] in args.case][: args.max_cases]
    arms = args.arm or list(grid.grid_arms())
    for case, seed in cases:
        ctx = make_context(case, seed, builtin)
        batch = [] if args.no_deterministic else [
            run_episode(selectors.make_selector(a, seed=seed), ctx, case) for a in
            ("fixed_order", "uniform_random", "simple_heuristic", "builtin_eig")]
        if args.live:
            batch += [grid_row(arm, case, seed, ctx, common) for arm in arms]
        for row in batch:
            rows.append(row)
            print(json.dumps(row, sort_keys=True), flush=True)
    report = {"generated_at": datetime.now(UTC).isoformat(), "measurement_mode": mode.value,
              "phase": "pilot", "grid_arms": arms if args.live else [],
              "skipped_cells": grid.skipped(), "cases": [list(x) for x in cases],
              "per_arm": summarize(rows), "rows": rows}
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


def relabel_meta_rows(path: Path) -> int:
    """Rewrite Meta rows' ``pricing_reference`` to the ``cost_basis=...; price_source=...`` form.

    Token rows → ``user_supplied_price``; attempt rows → ``upper_bound_estimate``; ``unknown``-cost
    rows are left as they are. Costs are unchanged (same prices). Returns rows relabelled.
    """
    import re

    default = load_models()["meta"]["model_id"]
    out, changed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = LedgerEntry.model_validate_json(line)
        ref = entry.pricing_reference or ""
        if (entry.provider == "meta" and entry.cost_status is not CostStatus.UNKNOWN
                and not ref.startswith("cost_basis=")):
            found = re.search(r"model=([\w.\-]+)", ref)
            note = ref.split("; ", 2)[2] if ref.count("; ") >= 2 else ""
            basis = meta.USER_SUPPLIED if entry.unit == "tokens" else meta.UPPER_BOUND
            entry = LedgerEntry.model_validate_json(entry.model_copy(update={
                "pricing_reference": meta.meta_pricing_reference(
                    found.group(1) if found else default, basis, note)[:500]}).model_dump_json())
            changed += 1
        out.append(entry.model_dump_json())
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(x + "\n" for x in out))
    tmp.replace(path)
    return changed


Main = Callable[[list[str] | None], int]
if __name__ == "__main__":
    raise SystemExit(main())
