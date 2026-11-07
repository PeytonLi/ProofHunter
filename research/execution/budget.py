"""Exact per-episode execution budget (RESEARCH-SPEC 3.3), enforced before every invocation."""

from __future__ import annotations

from dataclasses import dataclass, field

from proofhunter.contracts import Side
from research.schemas.models import (
    MAX_CONFIRMATIONS,
    MAX_INVOCATIONS_PER_EPISODE,
    MAX_PAIRS,
    MAX_RETRIES_PER_EPISODE,
    ProtocolV1,
)


class BudgetExceeded(RuntimeError):
    """An arm or adapter tried to spend beyond the frozen episode budget."""


@dataclass
class EpisodeBudget:
    """Counts every attempted invocation, including denied, failed and retried ones.

    Pairs are distinct candidates; the confirmation slot is reserved and can only be
    spent on the head of an already-run pair; retries repeat the same candidate's
    base or head and never create a new pair.
    """

    max_pairs: int = MAX_PAIRS
    max_confirmations: int = MAX_CONFIRMATIONS
    max_retries: int = MAX_RETRIES_PER_EPISODE
    max_invocations: int = MAX_INVOCATIONS_PER_EPISODE
    pairs: list[str] = field(default_factory=list)
    base: int = 0
    head: int = 0
    confirm: int = 0
    retries: int = 0
    failed: int = 0
    first_runs: set[tuple[str, Side]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.max_invocations != 2 * self.max_pairs + self.max_confirmations + self.max_retries:
            raise ValueError("invocation ceiling must equal 2*pairs + confirmations + retries")

    @classmethod
    def from_protocol(cls, protocol: ProtocolV1) -> EpisodeBudget:
        return cls(protocol.max_pairs, protocol.max_confirmations,
                   protocol.max_retries_per_episode, protocol.max_invocations_per_episode)

    @property
    def invocations(self) -> int:
        return self.base + self.head + self.confirm

    @property
    def remaining_pairs(self) -> int:
        return self.max_pairs - len(self.pairs)

    def open_pair(self, input_sha256: str) -> None:
        if input_sha256 in self.pairs:
            raise BudgetExceeded("candidate already probed; pairs must be distinct")
        if len(self.pairs) >= self.max_pairs:
            raise BudgetExceeded("paired-probe budget exhausted")
        self.pairs.append(input_sha256)

    def charge(self, side: Side, input_sha256: str, *, retry: bool = False) -> None:
        """Reserve one invocation; raises before anything runs if it would exceed a cap."""
        if not self.pairs or self.pairs[-1] != input_sha256:
            raise BudgetExceeded("invocation is not for the currently open pair")
        if self.invocations >= self.max_invocations:
            raise BudgetExceeded("episode invocation ceiling reached")
        if side is Side.CONFIRM:
            if retry or self.confirm >= self.max_confirmations:
                raise BudgetExceeded("reserved confirmation already spent")
            self.confirm += 1
            return
        if retry:
            if (input_sha256, side) not in self.first_runs:
                raise BudgetExceeded("a retry must repeat a side that already ran")
            if self.retries >= self.max_retries:
                raise BudgetExceeded("retry budget exhausted")
            self.retries += 1
        elif (input_sha256, side) in self.first_runs:
            raise BudgetExceeded("side already ran for this pair; use a counted retry")
        # Probes may never eat into the reserved confirmation slot.
        if self.base + self.head + 1 > 2 * self.max_pairs + self.max_retries:
            raise BudgetExceeded("probe invocations would consume the reserved confirmation")
        if not retry:
            self.first_runs.add((input_sha256, side))
        if side is Side.BASE:
            self.base += 1
        else:
            self.head += 1

    def can_retry(self) -> bool:
        return (self.retries < self.max_retries
                and self.invocations < self.max_invocations - self.max_confirmations + self.confirm)
