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
from dataclasses import dataclass, field, replace
from time import perf_counter_ns
from types import ModuleType
from typing import Final

from pydantic import ValidationError

from fixtures.adapters import SequentialIds
from proofhunter.contracts import (
    CandidateInput,
    Enrollment,
    ExecutionRequest,
    ObservationOutcome,
    ResourcePolicy,
    Side,
    input_sha256,
    sha256_hex,
)
from research.execution.allowlist import ReviewedSourceAllowlist
from research.execution.isolation import LocalTemplateSandbox

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1


@dataclass(frozen=True)
class Execution:
    outcome: ObservationOutcome
    value: int | None
    duration_ms: float
    startup_ms: float = 0.0
    cleanup_ms: float = 0.0
    launched: bool = False


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
        reset = getattr(self.inner, "reset", None)
        if reset is not None:
            reset()

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> Execution:
        self.counts[side] = self.counts.get(side, 0) + 1
        fault = self.faults.get((side, self.counts[side]))
        result = self.inner(side, source, candidate)
        if isinstance(fault, ObservationOutcome):
            return replace(result, outcome=fault, value=None)
        if isinstance(fault, int):
            return replace(result, outcome=ObservationOutcome.OK, value=fault)
        return result


class ProcessTemplateExecutor(Executor):
    """One fresh isolated child per invocation (``isolation.LocalTemplateSandbox``).

    The allowlist refuses anything that is not byte-identical reviewed template code before a
    process starts. Per-phase startup, execution and cleanup timings are reported.
    """

    def __init__(self, allowlist: ReviewedSourceAllowlist, enrollment: Enrollment, *,
                 timeout_seconds: float) -> None:
        self.sandbox = LocalTemplateSandbox(allowlist, enrollment)
        self.resources = ResourcePolicy(
            execution_timeout_seconds=max(1, min(5, int(timeout_seconds))))
        self.ids = SequentialIds(0x7E5)
        self.run_id = self.ids.new_run_id()

    def reset(self) -> None:
        self.run_id = self.ids.new_run_id()

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> Execution:
        e = self.sandbox.enrollment
        digest = sha256_hex(source)
        try:
            request = ExecutionRequest(
                run_id=self.run_id, attempt_id=self.ids.new_attempt_id(), side=Side(side),
                commit_sha=hashlib.sha1(bytes.fromhex(digest)).hexdigest(),  # noqa: S324
                source_path="checkout.py", source_content=source.decode("utf-8"),
                source_sha256=digest, input=candidate, input_sha256=input_sha256(candidate),
                entrypoint=e.entrypoint, resources=self.resources, snapshot_id=e.snapshot_id,
                image_digest=e.image_digest, harness_version=e.harness_version,
                harness_sha256=e.harness_sha256)
        except (UnicodeDecodeError, ValidationError):
            return Execution(ObservationOutcome.DENIED, None, 0.0)
        observation, timing = self.sandbox.invoke(request)
        if not timing.cleaned:
            raise RuntimeError("sandbox cleanup evidence missing for an invocation")
        return Execution(observation.outcome, observation.value, timing.execution_ms,
                         startup_ms=timing.startup_ms, cleanup_ms=timing.cleanup_ms,
                         launched=timing.refusal is None)
