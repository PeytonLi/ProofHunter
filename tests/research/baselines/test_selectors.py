from __future__ import annotations

import ast
from pathlib import Path

import pytest

from proofhunter import bayes
from research.baselines import selectors as s
from research.baselines.smoke import run_episode
from research.schemas.interfaces import EpisodeContext, EpisodeState, Selector

ARMS = ("fixed_order", "uniform_random", "simple_heuristic", "builtin_eig")


def state(ctx: EpisodeContext, **kw: object) -> EpisodeState:
    base = {"step": 1, "remaining_pairs": 6, "available": list(ctx.bank.candidates),
            "missing_coverage": [], "history": []}
    return EpisodeState(**(base | kw))  # type: ignore[arg-type]


@pytest.mark.parametrize("arm", ARMS)
def test_protocol_and_choice_in_pool(arm: str, ctx: EpisodeContext) -> None:
    sel = s.make_selector(arm, seed=7)
    assert isinstance(sel, Selector)
    assert sel.arm_id == arm
    sel.reset(ctx)
    assert sel.choose(state(ctx)) in {x.input_sha256 for x in ctx.bank.candidates}


@pytest.mark.parametrize("arm", ARMS)
def test_empty_pool_fails_clearly(arm: str, ctx: EpisodeContext) -> None:
    sel = s.make_selector(arm, seed=7)
    sel.reset(ctx)
    with pytest.raises(ValueError):
        sel.choose(state(ctx, available=[]))


def test_unknown_arm() -> None:
    with pytest.raises(ValueError):
        s.make_selector("frontier_agent", seed=1)


def test_fixed_order_anchors_then_strategy(ctx: EpisodeContext) -> None:
    pool = list(ctx.bank.candidates)
    order = sorted(pool, key=s.fixed_key)
    anchors = [x for x in order if x.is_anchor]
    assert order[: len(anchors)] == anchors
    rest = [s.FIXED_ORDER.index(x.strategy) for x in order[len(anchors):]]
    assert rest == sorted(rest)
    assert s.FixedOrder().choose(state(ctx)) == order[0].input_sha256


def test_uniform_random_seeded(ctx: EpisodeContext) -> None:
    def picks(seed: int) -> list[str]:
        sel = s.UniformRandom(seed)
        sel.reset(ctx)
        return [sel.choose(state(ctx)) for _ in range(5)]

    assert picks(1) == picks(1)
    assert picks(1) != picks(2)


def test_heuristic_prefers_disagreement(ctx: EpisodeContext) -> None:
    best = min(ctx.bank.candidates, key=s.heuristic_key)
    assert s.disagreement(best) == max(s.disagreement(x) for x in ctx.bank.candidates)


def test_builtin_eig_matches_production_select(ctx: EpisodeContext) -> None:
    from proofhunter import contracts as c

    sel = s.BuiltinEig()
    sel.reset(ctx)
    belief = bayes.initialize(s.plan_from_payload(ctx.plan)).belief
    expected = bayes.select(belief, list(ctx.bank.candidates),
                            c.SelectionPolicy(remaining_pairs=6, missing_coverage=[]))
    assert sel.choose(state(ctx)) == expected.candidate.input_sha256


def test_builtin_eig_updates_belief_from_feedback(ctx: EpisodeContext) -> None:
    sel = s.BuiltinEig()
    run_episode(sel, ctx, "fixed")
    assert sel.belief is not None
    assert sel.belief.updates >= 1


@pytest.mark.parametrize("arm", ARMS)
def test_episodes_deterministic(arm: str, ctx: EpisodeContext) -> None:
    a = run_episode(s.make_selector(arm, seed=3), ctx, "buggy")
    b = run_episode(s.make_selector(arm, seed=3), ctx, "buggy")
    assert {k: v for k, v in a.items() if k != "episode_wall_ms"} == \
        {k: v for k, v in b.items() if k != "episode_wall_ms"}
    assert a["pairs"] <= 6


def test_no_fork_of_production_eig() -> None:
    tree = ast.parse(Path(s.__file__).read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not names & {"eig", "entropy", "posterior", "likelihoods", "p_mismatch"}


def test_jsonl_ledger_round_trip(tmp_path: Path) -> None:
    from research.baselines.ledger import JsonlLedger, model_call_entry
    from research.schemas.models import CostStatus, LedgerPhase, MeasurementMode

    ledger = JsonlLedger(tmp_path / "l.jsonl")
    entry = model_call_entry(provider="openai", unit="tokens", quantity=1.0, cost_usd=0.1,
                             cost_status=CostStatus.ESTIMATED, pricing_reference="ref",
                             pricing_date="2026-11-07", wall_ms=1.0, estimated_job_usd=1.0,
                             phase=LedgerPhase.PILOT, mode=MeasurementMode.LIVE, run_id=None,
                             case_id=None)
    ledger.append(entry)
    assert ledger.entries() == [entry]
