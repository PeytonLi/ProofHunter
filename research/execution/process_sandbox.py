"""Process executor for reviewed Track A template code (offline measurement only).

Drop-in ``sandbox.Executor`` mirroring ``ops/sandbox/wrapper.py`` without root. Each call is
one counted attempt: the SHA-256 review gate and a static AST screen run first (a refusal is
``denied`` and no process starts), then the reviewed bytes are written to a fresh temporary
directory and run by an isolated ``python3 -I -B -S`` child with an empty environment,
per-process rlimits, a wall-clock deadline and bounded output, inside a fresh network
namespace when unprivileged user namespaces exist. The process group is killed and the
directory deleted every time, and the per-phase timings (startup, execution, cleanup) and
cleanup evidence are returned. This is not an untrusted-code sandbox and never runs dataset or
target-repository code; Track B stays design-only (``track_b.py``).
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
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
from pathlib import Path
from typing import Final

from proofhunter.contracts import MAX_SOURCE_BYTES, CandidateInput, ObservationOutcome
from research.execution.sandbox import Execution, Executor

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
MAX_OUTPUT_BYTES: Final = 4096
MEMORY_MIB: Final = 256
MAX_PROCESSES: Final = 64

ALLOWED_CALLS: Final = frozenset(
    {"abs", "divmod", "enumerate", "int", "len", "list", "max", "min", "range", "reversed",
     "round", "sorted", "sum", "tuple", "zip"}
)
ALLOWED_ANNOTATIONS: Final = frozenset({"int", "list", "tuple", "bool"})
_NODES: Final = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.AnnAssign, ast.For, ast.If, ast.IfExp, ast.Expr, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.Call, ast.Name, ast.Constant, ast.Subscript, ast.Slice, ast.List, ast.Tuple,
    ast.ListComp, ast.GeneratorExp, ast.comprehension, ast.Load, ast.Store, ast.operator,
    ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context, ast.Pass, ast.Break, ast.Continue,
)


def screen_source(data: bytes) -> str | None:
    """Return a refusal reason, or ``None`` if the source is a plain integer checkout module."""
    if len(data) > MAX_SOURCE_BYTES:
        return "source_too_large"
    try:
        tree = ast.parse(data.decode("utf-8"), filename="checkout.py")
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return "unparseable"
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    if "total_cents" not in names:
        return "missing_entrypoint"
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if not isinstance(stmt, ast.FunctionDef):
            return "top_level_statement"
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            return f"node_{type(node).__name__.lower()}"
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.type_params):
            return "decorator"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return "dunder_name"
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or (
                func.id not in ALLOWED_CALLS and func.id not in names
            ):
                return "call_not_allowed"
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes | complex):
            return "constant_not_allowed"
    return None


@dataclass(frozen=True)
class PhasedExecution(Execution):
    """``Execution`` plus per-phase timings and cleanup evidence for one attempt."""

    startup_ms: float = 0.0
    execution_ms: float = 0.0
    cleanup_ms: float = 0.0
    cleaned: bool = True
    refusal: str | None = None
    truncated: bool = False


@dataclass
class ProcessTemplateExecutor(Executor):
    """``(side, source, input) -> PhasedExecution``; refuses anything not reviewed."""

    reviewed: Callable[[str], bool]
    timeout_seconds: float
    python: str = field(default_factory=lambda: _python())
    network_namespace: bool | None = None
    attempts: int = 0
    log: list[PhasedExecution] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.network_namespace is None:
            self.network_namespace = network_namespace_available()

    def reset(self) -> None:
        self.log.clear()

    def command(self, target: Path) -> list[str]:
        base = [self.python, "-I", "-B", "-S", "-c", CHILD, str(target)]
        return ["unshare", "--user", "--net", "--", *base] if self.network_namespace else base

    def refusal(self, source: bytes) -> str | None:
        if not self.reviewed(hashlib.sha256(source).hexdigest()):
            return "not_reviewed"
        return screen_source(source)

    def __call__(self, side: str, source: bytes, candidate: CandidateInput) -> PhasedExecution:
        self.attempts += 1
        start = time.perf_counter()
        reason = self.refusal(source)
        if reason is not None:
            result = PhasedExecution(ObservationOutcome.DENIED, None,
                                     (time.perf_counter() - start) * 1000, refusal=reason)
        else:
            result = self._run(source, candidate, start)
        self.log.append(result)
        return result

    def _run(self, source: bytes, candidate: CandidateInput, start: float) -> PhasedExecution:
        target = Path(tempfile.mkdtemp(prefix="ph-research-exec-"))
        process: subprocess.Popen[bytes] | None = None
        outcome, value, truncated = ObservationOutcome.INFRA_ERROR, None, False
        started = executed = start
        try:
            path = target / "checkout.py"
            path.write_bytes(source)
            path.chmod(0o444)
            target.chmod(0o555)
            timeout = self.timeout_seconds
            process = subprocess.Popen(  # noqa: S603
                self.command(target), cwd=str(target), env=ENV, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                close_fds=True, preexec_fn=lambda: _limits(timeout))
            started = time.perf_counter()
            outcome, value, truncated = _collect(process, candidate, timeout)
        except OSError:
            outcome = ObservationOutcome.INFRA_ERROR
        finally:
            executed = time.perf_counter()
            if process is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            target.chmod(0o755)
            shutil.rmtree(target, ignore_errors=True)
            cleaned = not target.exists()
        done = time.perf_counter()
        return PhasedExecution(
            outcome, value, (done - start) * 1000, startup_ms=(started - start) * 1000,
            execution_ms=(executed - started) * 1000, cleanup_ms=(done - executed) * 1000,
            cleaned=cleaned, truncated=truncated)


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


def _limits(timeout_seconds: float) -> None:
    for kind, limit in (
        (resource.RLIMIT_AS, MEMORY_MIB * 1024 * 1024),
        (resource.RLIMIT_CPU, int(timeout_seconds + 0.999) + 1),  # wall deadline fires first
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_NPROC, MAX_PROCESSES),
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


def _collect(process: subprocess.Popen[bytes], candidate: CandidateInput, timeout: float
             ) -> tuple[ObservationOutcome, int | None, bool]:
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    deadline = time.monotonic() + timeout
    payload = json.dumps({"prices_cents": list(candidate.prices_cents),
                          "discount_percent": candidate.discount_percent}).encode()
    with contextlib.suppress(BrokenPipeError):
        process.stdin.write(payload)
        process.stdin.close()
    stdout = bytearray()
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ObservationOutcome.TIMEOUT, None, False
            for key, _ in selector.select(min(remaining, 0.05)):
                chunk = os.read(key.fd, min(4096, MAX_OUTPUT_BYTES + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if key.data == "stdout":
                    stdout.extend(chunk)
                if total > MAX_OUTPUT_BYTES:
                    return ObservationOutcome.INVALID, None, True
    try:
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return ObservationOutcome.TIMEOUT, None, False
    if code != 0:
        return ObservationOutcome.NONZERO, None, False
    try:
        parsed = json.loads(bytes(stdout), object_pairs_hook=_unique)
    except ValueError:
        return ObservationOutcome.INVALID, None, False
    if (type(parsed) is dict and set(parsed) == {"value"} and type(parsed["value"]) is int
            and INT64[0] <= parsed["value"] <= INT64[1]):
        return ObservationOutcome.OK, parsed["value"], False
    return ObservationOutcome.INVALID, None, False
