"""Exact episode budget for every frozen arm under adversarial selectors and fault patterns."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from proofhunter.contracts import ObservationOutcome
from research.execution import doubles
from research.execution.sandbox import Execution, FaultInjectingExecutor
from research.schemas.interfaces import EpisodeContext, EpisodeState
from research.schemas.models import MeasurementMode, ProtocolV1, RawResultRow

REPO = Path(__file__).resolve().parents[3]
PROTOCOL = ProtocolV1.model_validate_json((REPO / "research/spec/protocol-v1.json").read_text())
ARMS = tuple(a.arm_id for a in PROTOCOL.arms)
SIDES = ("base", "head", "confirm")
BAD = (ObservationOutcome.TIMEOUT, ObservationOutcome.INFRA_ERROR, ObservationOutcome.NONZERO,
       ObservationOutcome.INVALID)


class Adversary:
    """Picks the first, last or a random candidate; never stops on its own."""

    def __init__(self, arm_id: str, how: str, seed: int = 0) -> None:
        self.arm_id, self.how, self.seed = arm_id, how, seed

    def reset(self, context: EpisodeContext) -> None:
        self.rng = random.Random(f"{self.seed}:{context.case_id}")

    def choose(self, state: EpisodeState) -> str:
        pool = sorted(c.input_sha256 for c in state.available)
        return {"first": pool[0], "last": pool[-1]}.get(self.how) or self.rng.choice(pool)


class Recording(FaultInjectingExecutor):
    def __call__(self, side, source, candidate):  # type: ignore[no-untyped-def]
        result = super().__call__(side, source, candidate)
        self.seen.append(result)
        return result


def _faults(name: str, seed: int) -> dict:
    if name == "none":
        return {}
    if name == "all_fail":
        return {(s, n): BAD[n % 4] for s in SIDES for n in range(1, 30)}
    if name == "base_timeout":
        return {("base", n): ObservationOutcome.TIMEOUT for n in range(1, 30)}
    if name == "head_flaky":
        return {("head", n): ObservationOutcome.INFRA_ERROR for n in range(1, 30, 2)}
    if name == "confirm_fail":
        return {("confirm", n): ObservationOutcome.TIMEOUT for n in range(1, 30)}
    rng = random.Random(seed)
    return {(s, n): rng.choice(BAD) for s in SIDES for n in range(1, 30) if rng.random() < 0.4}


def _check(row: RawResultRow, seen: list[Execution]) -> None:
    inv = row.invocations
    assert inv.base + inv.head + inv.confirm == len(seen) <= PROTOCOL.max_invocations_per_episode
    assert len(row.probes) <= PROTOCOL.max_pairs
    assert inv.confirm <= PROTOCOL.max_confirmations
    assert inv.base <= PROTOCOL.max_pairs + PROTOCOL.max_retries_per_episode
    assert inv.head <= PROTOCOL.max_pairs + PROTOCOL.max_retries_per_episode
    assert sum(p.retries for p in row.probes) <= PROTOCOL.max_retries_per_episode
    assert len({p.candidate_input_sha256 for p in row.probes}) == len(row.probes)
    assert inv.failed == sum(r.outcome is not ObservationOutcome.OK for r in seen)
    assert RawResultRow.model_validate_json(row.model_dump_json()) == row


@pytest.mark.parametrize("arm_id", ARMS)
@pytest.mark.parametrize("pattern", ["none", "all_fail", "base_timeout", "head_flaky",
                                     "confirm_fail", "random"])
def test_no_arm_exceeds_budget_and_every_attempt_is_counted(arm_id: str, pattern: str) -> None:
    for i, case in enumerate(("per_line", "clean_divmod", "existing")):
        for how in ("last", "random"):
            runner = doubles.offline_runner(PROTOCOL)
            inner = runner.executor
            assert isinstance(inner, FaultInjectingExecutor)
            executor = Recording(inner.inner, _faults(pattern, i))
            executor.seen = []  # type: ignore[attr-defined]
            runner.executor = executor
            row = runner.run(doubles.bench_sources(case), Adversary(arm_id, how, i), None,
                             arm_id=arm_id, seed=i, repetition=0,
                             mode=MeasurementMode.OFFLINE)
            _check(row, executor.seen)  # type: ignore[attr-defined]
            if pattern == "all_fail":
                assert row.verdict.value == "inconclusive"  # type: ignore[union-attr]
                assert row.invocations.failed == len(executor.seen)  # type: ignore[attr-defined]


def test_full_budget_is_exactly_fifteen_and_reserves_the_confirmation() -> None:
    """Six usable-on-base pairs, both retries spent on head, then the reserved confirmation."""
    head = {("head", 1): ObservationOutcome.TIMEOUT, ("head", 2): ObservationOutcome.TIMEOUT}
    runner = doubles.offline_runner(PROTOCOL, faults=head)
    rows = [runner.run(doubles.bench_sources(c), Adversary("fixed_order", how), None,
                       arm_id="fixed_order", seed=1, repetition=0, mode=MeasurementMode.OFFLINE)
            for c in doubles.BENCH_IDS for how in ("first", "last", "random")]
    worst = max(r.invocations.base + r.invocations.head + r.invocations.confirm for r in rows)
    assert worst <= PROTOCOL.max_invocations_per_episode
    for row in rows:
        if row.probes and row.probes[-1].outcome.value == "mismatch":
            assert row.invocations.confirm == 1


def test_protocol_ceiling_is_the_frozen_formula() -> None:
    p = PROTOCOL
    assert (p.max_pairs, p.max_confirmations, p.max_retries_per_episode) == (6, 1, 2)
    assert p.max_invocations_per_episode == 2 * p.max_pairs + p.max_confirmations + \
        p.max_retries_per_episode == 15
