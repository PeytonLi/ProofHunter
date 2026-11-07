"""Allowlisted in-process transport, not an isolation claim or cloud timing."""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter_ns
from types import ModuleType

from fixtures.adapters import Fault, FaultKey, FixtureRunner
from proofhunter.contracts import (
    Enrollment,
    ExecutionRequest,
    IsolationStatus,
    ObservationOutcome,
    RunnerMode,
    SandboxObservation,
    canonical_json,
    sha256_hex,
)


class BenchRunner(FixtureRunner):
    def __init__(self, enrolled: Mapping[str, bytes], enrollment: Enrollment, *,
                 faults: Mapping[FaultKey, Fault] | None = None,
                 isolation: IsolationStatus = IsolationStatus.OFFLINE_FIXTURE) -> None:
        super().__init__(enrollment, faults=faults, isolation=isolation)
        self.enrolled = dict(enrolled)
        if any(sha256_hex(data) != digest for digest, data in self.enrolled.items()):
            raise ValueError("enrollment bytes/hash mismatch")

    async def observe(self, request: ExecutionRequest) -> SandboxObservation:
        self.requests.append(request)
        start = perf_counter_ns()
        started = self._clock.now()
        outcome = ObservationOutcome.OK
        value = None
        exit_code: int | None = 0
        data = self.enrolled.get(request.source_sha256)
        if (data is None or data != request.source_content.encode()
                or request.snapshot_id != self._enrollment.snapshot_id
                or request.image_digest != self._enrollment.image_digest
                or request.harness_sha256 != self._enrollment.harness_sha256
                or request.harness_version != self._enrollment.harness_version
                or self._isolation is not IsolationStatus.OFFLINE_FIXTURE):
            outcome, exit_code = ObservationOutcome.DENIED, None
        else:
            fault = self._fault(request)
            if isinstance(fault, ObservationOutcome):
                outcome = fault
                exit_code = 1 if fault is ObservationOutcome.NONZERO else None
            else:
                module = ModuleType("_bench_checkout")
                try:
                    exec(compile(data, "<enrolled-checkout>", "exec"), module.__dict__)  # noqa: S102
                    computed = module.total_cents(list(request.input.prices_cents),
                                                  request.input.discount_percent)
                    computed = fault if isinstance(fault, int) else computed
                    if type(computed) is not int or not -(2**63) <= computed < 2**63:
                        outcome, exit_code = ObservationOutcome.INVALID, None
                    else:
                        value = computed
                except Exception:
                    outcome, exit_code = ObservationOutcome.NONZERO, 1
        stdout = canonical_json({"value": value}) if outcome is ObservationOutcome.OK else b""
        return SandboxObservation(
            attempt_id=request.attempt_id, side=request.side, commit_sha=request.commit_sha,
            source_sha256=request.source_sha256, input_sha256=request.input_sha256,
            runner_mode=RunnerMode.OFFLINE_FIXTURE, snapshot_id=request.snapshot_id,
            image_digest=request.image_digest, harness_sha256=request.harness_sha256,
            sandbox_id=None, started_at=started,
            duration_ms=(perf_counter_ns() - start) // 1_000_000,
            outcome=outcome, value=value, exit_code=exit_code,
            stdout_sha256=sha256_hex(stdout), stderr_sha256=sha256_hex(b""), truncated=False)
