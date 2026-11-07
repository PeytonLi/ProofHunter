"""Local process wrapper for reviewed Track A template code (offline measurement only).

Mirrors ``ops/sandbox/wrapper.py`` without root: a fresh temporary directory per
invocation, an isolated ``python3 -I -B -S`` child with an empty environment,
per-process rlimits, a wall-clock deadline, bounded output, process-group kill
and directory deletion. When unprivileged user namespaces exist the child also
runs in a fresh network namespace. This is *not* an untrusted-code sandbox; the
allowlist (``allowlist.py``) is the gate and refuses everything else before any
process starts.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from proofhunter.contracts import (
    Enrollment,
    ExecutionRequest,
    ObservationOutcome,
    ResourcePolicy,
    RunnerMode,
    SandboxObservation,
    sha256_hex,
)
from research.execution.allowlist import ReviewedSourceAllowlist

CHILD: Final = (
    "import sys,json\n"
    "sys.path.insert(0,sys.argv[1])\n"
    "from checkout import total_cents\n"
    "d=json.loads(sys.stdin.read(8192))\n"
    "v=total_cents(d['prices_cents'],d['discount_percent'])\n"
    "sys.stdout.write(json.dumps({'value':v},allow_nan=False))\n"
)
ENV: Final = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
INT64: Final = (-(2**63), 2**63 - 1)


@dataclass
class PhaseTiming:
    startup_ms: float = 0.0
    execution_ms: float = 0.0
    cleanup_ms: float = 0.0
    cleaned: bool = False
    refusal: str | None = None


@dataclass
class LocalTemplateSandbox:
    """Synchronous runner; every ``invoke`` call is one counted attempt."""

    allowlist: ReviewedSourceAllowlist
    enrollment: Enrollment
    python: str = field(default_factory=lambda: _python())
    network_namespace: bool | None = None
    clock: Callable[[], float] = time.perf_counter
    invocations: int = 0

    def __post_init__(self) -> None:
        if self.network_namespace is None:
            self.network_namespace = network_namespace_available()

    def command(self, target: Path) -> list[str]:
        base = [self.python, "-I", "-B", "-S", "-c", CHILD, str(target)]
        return ["unshare", "--user", "--net", "--", *base] if self.network_namespace else base

    def _binding_refusal(self, request: ExecutionRequest) -> str | None:
        e = self.enrollment
        if (request.snapshot_id, request.image_digest, request.harness_sha256,
                request.harness_version) != (e.snapshot_id, e.image_digest, e.harness_sha256,
                                             e.harness_version):
            return "enrollment_mismatch"
        return self.allowlist.refusal(request.source_sha256, request.source_content.encode())

    def invoke(self, request: ExecutionRequest) -> tuple[SandboxObservation, PhaseTiming]:
        self.invocations += 1
        timing = PhaseTiming()
        started_at = datetime.now(UTC)
        timing.refusal = self._binding_refusal(request)
        if timing.refusal is not None:
            timing.cleaned = True
            return self._observation(request, started_at, ObservationOutcome.DENIED, None, b"",
                                     b"", False, None), timing
        outcome, value, stdout, stderr, truncated, code = self._run(request, timing)
        return self._observation(request, started_at, outcome, value, stdout, stderr, truncated,
                                 code), timing

    def _run(self, request: ExecutionRequest, timing: PhaseTiming
             ) -> tuple[ObservationOutcome, int | None, bytes, bytes, bool, int | None]:
        policy = request.resources
        t0 = self.clock()
        target = Path(tempfile.mkdtemp(prefix="ph-research-exec-"))
        process: subprocess.Popen[bytes] | None = None
        try:
            source = target / "checkout.py"
            source.write_bytes(request.source_content.encode())
            source.chmod(0o444)
            target.chmod(0o555)
            process = subprocess.Popen(  # noqa: S603
                self.command(target), cwd=str(target), env=ENV, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                close_fds=True, preexec_fn=lambda: _limits(policy))
            t1 = self.clock()
            timing.startup_ms = (t1 - t0) * 1000
            result = _collect(process, request, policy)
            timing.execution_ms = (self.clock() - t1) * 1000
            return result
        except OSError:
            return ObservationOutcome.INFRA_ERROR, None, b"", b"", False, None
        finally:
            t2 = self.clock()
            if process is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            target.chmod(0o755)
            shutil.rmtree(target, ignore_errors=True)
            timing.cleaned = not target.exists()
            timing.cleanup_ms = (self.clock() - t2) * 1000

    def _observation(self, request: ExecutionRequest, started_at: datetime,
                     outcome: ObservationOutcome, value: int | None, stdout: bytes,
                     stderr: bytes, truncated: bool, code: int | None) -> SandboxObservation:
        return SandboxObservation(
            attempt_id=request.attempt_id, side=request.side, commit_sha=request.commit_sha,
            source_sha256=request.source_sha256, input_sha256=request.input_sha256,
            runner_mode=RunnerMode.OFFLINE_FIXTURE, snapshot_id=request.snapshot_id,
            image_digest=request.image_digest, harness_sha256=request.harness_sha256,
            sandbox_id=None, started_at=started_at,
            duration_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
            outcome=outcome, value=value if outcome is ObservationOutcome.OK else None,
            exit_code=code, stdout_sha256=sha256_hex(stdout), stderr_sha256=sha256_hex(stderr),
            truncated=truncated)


def _python() -> str:
    return "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


@functools.cache
def network_namespace_available() -> bool:
    """True when ``unshare --user --net`` works and outbound connect fails inside it."""
    if shutil.which("unshare") is None:
        return False
    probe = ("import socket;s=socket.socket();s.settimeout(1);"
             "print(s.connect_ex(('192.0.2.1',80)))")
    try:
        result = subprocess.run(  # noqa: S603
            ["unshare", "--user", "--net", "--", _python(), "-I", "-S", "-c", probe],  # noqa: S607
            capture_output=True, timeout=5, env=ENV, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() not in (b"", b"0")


def _limits(policy: ResourcePolicy) -> None:
    for kind, limit in (
        (resource.RLIMIT_AS, policy.memory_mib * 1024 * 1024),
        (resource.RLIMIT_CPU, policy.execution_timeout_seconds),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_NPROC, policy.max_processes),
        (resource.RLIMIT_FSIZE, 0),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))


def _unique(items: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _collect(process: subprocess.Popen[bytes], request: ExecutionRequest, policy: ResourcePolicy
             ) -> tuple[ObservationOutcome, int | None, bytes, bytes, bool, int | None]:
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    cap = policy.max_output_bytes
    deadline = time.monotonic() + policy.execution_timeout_seconds
    payload = json.dumps(request.input.model_dump(mode="json")).encode()
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except BrokenPipeError:
        pass
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return (ObservationOutcome.TIMEOUT, None, bytes(buffers["stdout"]),
                        bytes(buffers["stderr"]), False, None)
            for key, _ in selector.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, min(4096, cap + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                buffers[key.data].extend(chunk)
                if total > cap:
                    return (ObservationOutcome.INVALID, None, bytes(buffers["stdout"][:cap]),
                            b"", True, None)
    try:
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return ObservationOutcome.TIMEOUT, None, bytes(buffers["stdout"]), b"", False, None
    stdout, stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])
    if code != 0:
        return ObservationOutcome.NONZERO, None, stdout, stderr, False, code
    try:
        parsed = json.loads(stdout, object_pairs_hook=_unique)
    except ValueError:
        return ObservationOutcome.INVALID, None, stdout, stderr, False, code
    if (type(parsed) is dict and set(parsed) == {"value"} and type(parsed["value"]) is int
            and INT64[0] <= parsed["value"] <= INT64[1]):
        return ObservationOutcome.OK, parsed["value"], stdout, stderr, False, code
    return ObservationOutcome.INVALID, None, stdout, stderr, False, code
