from __future__ import annotations

import itertools
import random

import pytest

from proofhunter.contracts import Side
from research.execution.budget import BudgetExceeded, EpisodeBudget


def test_budget_matches_frozen_protocol(protocol):
    b = EpisodeBudget.from_protocol(protocol)
    assert (b.max_pairs, b.max_confirmations, b.max_retries, b.max_invocations) == (6, 1, 2, 15)
    with pytest.raises(ValueError):
        EpisodeBudget(max_pairs=6, max_confirmations=1, max_retries=2, max_invocations=16)


def test_six_distinct_pairs_only():
    b = EpisodeBudget()
    for i in range(6):
        b.open_pair(f"c{i}")
    with pytest.raises(BudgetExceeded):
        b.open_pair("c6")
    b2 = EpisodeBudget()
    b2.open_pair("x")
    with pytest.raises(BudgetExceeded, match="distinct"):
        b2.open_pair("x")


def test_one_side_each_per_pair_retries_bounded_confirmation_reserved():
    b = EpisodeBudget()
    b.open_pair("a")
    with pytest.raises(BudgetExceeded, match="repeat"):
        b.charge(Side.BASE, "a", retry=True)
    b.charge(Side.BASE, "a")
    with pytest.raises(BudgetExceeded):
        b.charge(Side.BASE, "a")
    b.charge(Side.BASE, "a", retry=True)
    b.charge(Side.HEAD, "a")
    b.charge(Side.HEAD, "a", retry=True)
    with pytest.raises(BudgetExceeded, match="retry"):
        b.charge(Side.HEAD, "a", retry=True)
    b.charge(Side.CONFIRM, "a")
    with pytest.raises(BudgetExceeded):
        b.charge(Side.CONFIRM, "a")
    with pytest.raises(BudgetExceeded):
        b.charge(Side.BASE, "other")
    assert (b.base, b.head, b.confirm, b.retries) == (2, 2, 1, 2)


def test_random_adversarial_call_sequences_never_exceed_ceiling():
    rng = random.Random(20261107)
    for _ in range(2000):
        b = EpisodeBudget()
        for _ in range(60):
            op = rng.randrange(5)
            key = f"c{rng.randrange(8)}"
            with _maybe():
                if op == 0:
                    b.open_pair(key)
                elif b.pairs:
                    side = (Side.BASE, Side.HEAD, Side.CONFIRM)[op % 3]
                    b.charge(side, b.pairs[-1] if rng.random() < 0.9 else key,
                             retry=rng.random() < 0.3)
            assert b.invocations <= 15
            assert len(b.pairs) <= 6
            assert b.confirm <= 1
            assert b.retries <= 2
            assert b.base + b.head <= 14


def test_exhaustive_full_budget_is_exactly_fifteen():
    b = EpisodeBudget()
    for i in range(6):
        b.open_pair(f"c{i}")
        b.charge(Side.BASE, f"c{i}")
        b.charge(Side.HEAD, f"c{i}")
    for side in itertools.islice(itertools.cycle((Side.BASE, Side.HEAD)), 2):
        b.charge(side, "c5", retry=True)
    b.charge(Side.CONFIRM, "c5")
    assert b.invocations == 15
    for side, retry in itertools.product(Side, (False, True)):
        with pytest.raises(BudgetExceeded):
            b.charge(side, "c5", retry=retry)


class _maybe:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        return kind is BudgetExceeded
