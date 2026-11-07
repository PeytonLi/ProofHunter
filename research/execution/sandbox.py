"""Executes reviewed Track A template output only, with a per-invocation timeout.

This is an allowlisted in-process transport for reviewed first-party template code. It is not an
isolation boundary and never runs dataset or target-repository code.
"""

from __future__ import annotations

import hashlib
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter_ns
from types import ModuleType
from typing import Final

from proofhunter.contracts import CandidateInput, ObservationOutcome

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1


@dataclass(frozen=True)
class Execution:
    outcome: ObservationOutcome
    value: int | None
    duration_ms: float


class InvocationTimeout(Exception):
    pass


@contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("reviewed execution needs the main thread for its SIGALRM deadline")

    def expire(signum: int, frame: object) -> None:
        raise InvocationTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Executor:
    """Callable ``(side, source, input) -> Execution``; ``side`` is base, head or confirm."""

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> Execution:
        raise NotImplementedError


class ReviewedTemplateExecutor(Executor):
    def __init__(self, reviewed_sha256: Callable[[str], bool], *, timeout_seconds: float) -> None:
        self.reviewed = reviewed_sha256
        self._timeout = timeout_seconds
        self._compiled: dict[str, ModuleType] = {}

    def _module(self, source: bytes) -> ModuleType | None:
        digest = hashlib.sha256(source).hexdigest()
        if not self.reviewed(digest):
            return None
        module = self._compiled.get(digest)
        if module is None:
            module = ModuleType("_research_checkout")
            exec(compile(source, "<reviewed-template>", "exec"), module.__dict__)  # noqa: S102
            self._compiled[digest] = module
        return module

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> Execution:
        start = perf_counter_ns()
        outcome, value = ObservationOutcome.OK, None
        try:
            with _deadline(self._timeout):
                module = self._module(source)
                if module is None:
                    outcome = ObservationOutcome.DENIED
                else:
                    computed = module.total_cents(
                        list(candidate.prices_cents), candidate.discount_percent
                    )
                    if type(computed) is not int or not INT64_MIN <= computed <= INT64_MAX:
                        outcome = ObservationOutcome.INVALID
                    else:
                        value = computed
        except InvocationTimeout:
            outcome = ObservationOutcome.TIMEOUT
        except Exception:
            outcome = ObservationOutcome.NONZERO
        return Execution(outcome, value, (perf_counter_ns() - start) / 1e6)


FaultKey = tuple[str, int]  # (side, 1-based invocation number of that side in the episode)


@dataclass
class FaultInjectingExecutor(Executor):
    """Test/drill wrapper: replaces chosen invocations with a fault outcome or a forced value."""

    inner: Executor
    faults: Mapping[FaultKey, ObservationOutcome | int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.counts.clear()

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> Execution:
        self.counts[side] = self.counts.get(side, 0) + 1
        fault = self.faults.get((side, self.counts[side]))
        result = self.inner(side, source, candidate)
        if isinstance(fault, ObservationOutcome):
            return Execution(fault, None, result.duration_ms)
        if isinstance(fault, int):
            return Execution(ObservationOutcome.OK, fault, result.duration_ms)
        return result
