"""Isolated execution of *reviewed* Track A template sources only.

Sources must pass a reviewer (``verify_reviewed``) before any byte runs: their hashes must equal
the case record and, when the dataset lane's renderer is importable, the bytes must equal its
re-rendering of the reviewed template. Execution happens in a fresh ``python -I -S`` child with an
empty environment (no secrets), CPU/memory limits and a wall timeout per call batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from research.schemas.interfaces import CaseSources

INVOCATION_TIMEOUT_S: Final = 5.0
_CHILD: Final = r"""
import json, sys
req = json.loads(sys.stdin.read())
ns = {"__name__": "checkout"}
try:
    exec(compile(req["source"], "checkout.py", "exec"), ns)
    fn = ns["total_cents"]
except BaseException as exc:
    print(json.dumps({"load_error": type(exc).__name__})); raise SystemExit(0)
out = []
for prices, discount in req["calls"]:
    try:
        value = fn(list(prices), discount)
        ok = type(value) is int and value >= 0
        out.append({"value": value} if ok else {"error": "invalid_output"})
    except BaseException as exc:
        out.append({"error": "exception"})
print(json.dumps({"results": out}))
"""

Reviewer = Callable[[CaseSources], None]


class UnreviewedSourceError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_reviewed(sources: CaseSources) -> None:
    """Default reviewer: hashes match the case and the dataset lane's renderer reproduces them."""
    case = sources.case
    if (sha256(sources.base_source) != case.base_source_sha256
            or sha256(sources.head_source) != case.head_source_sha256):
        raise UnreviewedSourceError(f"{case.case_id}: source hash mismatch")
    try:
        from research.data import templates  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError as exc:
        raise UnreviewedSourceError("dataset template renderer unavailable") from exc
    reviewed = {(t.template_id, t.template_version, t.template_sha256)
                for t in templates.list_templates()}
    if (case.template_id, case.template_version, case.template_sha256) not in reviewed:
        raise UnreviewedSourceError(f"{case.case_id}: template is not in the reviewed list")
    base, head = templates.render(case.template_id, case.params)
    if (base, head) != (sources.base_source, sources.head_source):
        raise UnreviewedSourceError(f"{case.case_id}: sources differ from reviewed template")


@dataclass(frozen=True)
class CallResult:
    value: int | None
    error: str | None


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))


def run_batch(source: bytes, calls: Sequence[tuple[Sequence[int], int]]
              ) -> tuple[list[CallResult], float]:
    """One fresh child process per batch; returns per-call results and batch wall ms."""
    payload = json.dumps({"source": source.decode("utf-8"),
                          "calls": [[list(p), d] for p, d in calls]})
    start = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 - reviewed source, isolated child
            [sys.executable, "-I", "-S", "-c", _CHILD], input=payload, capture_output=True,
            text=True, timeout=INVOCATION_TIMEOUT_S * max(1, len(calls)), env={},
            cwd=os.path.abspath(os.sep), preexec_fn=_limits, check=False)
    except subprocess.TimeoutExpired:
        return [CallResult(None, "timeout") for _ in calls], (time.perf_counter() - start) * 1e3
    wall = (time.perf_counter() - start) * 1e3
    try:
        body = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return [CallResult(None, "infra_failure") for _ in calls], wall
    if "load_error" in body:
        return [CallResult(None, "load_error") for _ in calls], wall
    results = [CallResult(r.get("value"), r.get("error")) for r in body["results"]]
    if len(results) != len(calls):
        return [CallResult(None, "infra_failure") for _ in calls], wall
    return results, wall
