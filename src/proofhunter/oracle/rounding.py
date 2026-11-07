"""Trusted ``checkout-rounding@1`` oracle (T1), evaluated on the control plane.

The oracle is independent of the Bayes predictors and of every target revision.
It never runs inside the sandbox and its value never enters an
``ExecutionRequest``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from proofhunter.contracts import (
    CONTRACT_ID,
    MAX_DISCOUNT_PERCENT,
    MAX_ITEMS,
    MAX_PRICE_CENTS,
    ORACLE_VERSION,
    Candidate,
    Enrollment,
    OracleResult,
    OracleStatus,
    TrustTier,
    input_sha256,
)

OracleFn = Callable[[Sequence[int], int], int]


def _strict_int(value: object) -> bool:
    return type(value) is int


def legal_input(prices_cents: object, discount_percent: object) -> bool:
    """Boundary check of the enrolled domain: strict ints only, no booleans."""
    if not isinstance(prices_cents, list | tuple) or len(prices_cents) > MAX_ITEMS:
        return False
    for price in prices_cents:
        if not (isinstance(price, int) and _strict_int(price) and 0 <= price <= MAX_PRICE_CENTS):
            return False
    return (isinstance(discount_percent, int) and _strict_int(discount_percent)
            and 0 <= discount_percent <= MAX_DISCOUNT_PERCENT)


def rounding_half_up_once(prices_cents: Sequence[int], discount_percent: int) -> int:
    """``n = sum(prices) * (100 - discount)``; return ``(n + 50) // 100``.

    Pure integer arithmetic. Raises ``ValueError`` outside the enrolled domain.
    """
    if not legal_input(list(prices_cents), discount_percent):
        raise ValueError("input outside checkout-rounding@1 domain")
    n = 0
    for price in prices_cents:
        n += price
    n *= 100 - discount_percent
    return (n + 50) // 100


#: Reviewed oracles by ``(contract_id, oracle_version)``. Only these confer T1.
TRUSTED_ORACLES: Final[Mapping[tuple[str, str], OracleFn]] = {
    (CONTRACT_ID, ORACLE_VERSION): rounding_half_up_once,
}


def _bound_hash(candidate: Candidate) -> str:
    try:
        recomputed = input_sha256({"discount_percent": candidate.discount_percent,
                                   "prices_cents": list(candidate.prices_cents)})
    except (ValueError, TypeError, KeyError):
        recomputed = None
    if recomputed is not None:
        return recomputed
    stored = getattr(candidate, "input_sha256", "")
    if isinstance(stored, str) and len(stored) == 64 and all(
            ch in "0123456789abcdef" for ch in stored):
        return stored
    return "0" * 64


def evaluate(enrollment: Enrollment, candidate: Candidate) -> OracleResult:
    """Expected value for ``candidate`` under the enrolled contract.

    Returns ``status=unavailable`` (tier T3, no expected value) when the enrollment
    is disabled or names no reviewed oracle, and ``status=invalid`` when the
    candidate is outside the domain or its input hash does not bind its values.
    """
    digest = _bound_hash(candidate)

    def result(status: OracleStatus, tier: TrustTier, expected: int | None) -> OracleResult:
        return OracleResult(contract_id=CONTRACT_ID, contract_sha256=enrollment.contract_sha256,
                            oracle_version=enrollment.oracle_version, input_sha256=digest,
                            trust_tier=tier, expected_cents=expected, status=status)

    oracle = TRUSTED_ORACLES.get((str(enrollment.contract_id), str(enrollment.oracle_version)))
    if oracle is None or enrollment.enabled is not True:
        return result(OracleStatus.UNAVAILABLE, TrustTier.T3, None)
    prices = getattr(candidate, "prices_cents", None)
    discount = getattr(candidate, "discount_percent", None)
    if not legal_input(prices, discount) or digest != getattr(candidate, "input_sha256", None):
        return result(OracleStatus.INVALID, TrustTier.T1, None)
    if not isinstance(prices, list | tuple) or not isinstance(discount, int):
        return result(OracleStatus.INVALID, TrustTier.T1, None)
    return result(OracleStatus.OK, TrustTier.T1, oracle(list(prices), discount))
