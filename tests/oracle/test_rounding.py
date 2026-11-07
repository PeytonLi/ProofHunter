from __future__ import annotations

import itertools
from decimal import ROUND_HALF_UP, Decimal

import pytest

from fixtures.reference import make_candidate
from proofhunter.contracts import (
    Candidate,
    Enrollment,
    OracleStatus,
    Strategy,
    TrustTier,
)
from proofhunter.oracle import evaluate, legal_input, rounding_half_up_once


def independent(prices: list[int], discount: int) -> int:
    total = Decimal(sum(prices)) * (Decimal(100 - discount) / Decimal(100))
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def test_sentinel_is_41_and_mutants_differ() -> None:
    assert rounding_half_up_once([15, 15, 15], 10) == 41
    per_item = sum(independent([p], 10) for p in [15, 15, 15])
    assert per_item == 42


@pytest.mark.parametrize(("prices", "discount", "expected"), [
    ([], 0, 0), ([], 17, 0), ([99], 100, 0), ([10_000] * 8, 100, 0), ([1, 2], 0, 3),
    ([10_000] * 8, 0, 80_000), ([5], 10, 5), ([1], 50, 1), ([3], 50, 2), ([45], 10, 41),
])
def test_known_values(prices: list[int], discount: int, expected: int) -> None:
    assert rounding_half_up_once(prices, discount) == expected


def test_matches_independent_decimal_rounding_on_grid() -> None:
    prices_grid = [[], [0], [1], [5], [15], [15, 15, 15], [9_999, 1], [10_000] * 8, [7, 7, 7, 7]]
    for prices, discount in itertools.product(prices_grid, range(0, 101)):
        assert rounding_half_up_once(prices, discount) == independent(prices, discount)


@pytest.mark.parametrize(("prices", "discount"), [
    ([True], 10), ([1.0], 10), ([1], True), ([1], 10.0), ([-1], 10), ([10_001], 10),
    ([1] * 9, 10), ([1], 101), ([1], -1), ("15", 10), ([1], "10"),
])
def test_boundary_rejects_illegal_input(prices: object, discount: object) -> None:
    assert not legal_input(prices, discount)
    with pytest.raises(ValueError, match="domain"):
        rounding_half_up_once(prices, discount)  # type: ignore[arg-type]


def test_evaluate_t1(enrollment: Enrollment) -> None:
    candidate = make_candidate([15, 15, 15], 10, Strategy.CONSTRAINT, is_anchor=True)
    result = evaluate(enrollment, candidate)
    assert result.status is OracleStatus.OK
    assert result.trust_tier is TrustTier.T1
    assert result.expected_cents == 41
    assert result.input_sha256 == candidate.input_sha256
    assert result.contract_sha256 == enrollment.contract_sha256
    assert result.oracle_version == enrollment.oracle_version


def test_evaluate_unavailable_when_oracle_unknown_or_disabled(enrollment: Enrollment) -> None:
    candidate = make_candidate([15, 15, 15], 10, Strategy.CONSTRAINT)
    for changed in ({"oracle_version": "checkout-rounding-oracle@2"}, {"enabled": False}):
        result = evaluate(enrollment.model_copy(update=changed), candidate)
        assert result.status is OracleStatus.UNAVAILABLE
        assert result.expected_cents is None
        assert result.trust_tier is not TrustTier.T1


def test_evaluate_invalid_for_unvalidated_or_unbound_candidates(enrollment: Enrollment) -> None:
    good = make_candidate([15, 15, 15], 10, Strategy.CONSTRAINT)
    forged_bool = Candidate.model_construct(**{**good.model_dump(), "prices_cents": [True]})
    forged_hash = good.model_copy(update={"prices_cents": [16, 15, 15]})
    for candidate in (forged_bool, forged_hash):
        result = evaluate(enrollment, candidate)
        assert result.status is OracleStatus.INVALID
        assert result.expected_cents is None
