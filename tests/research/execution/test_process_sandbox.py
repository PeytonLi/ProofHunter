"""Process executor: non-reviewed code never starts, reviewed code is bounded and cleaned up."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import tempfile
from pathlib import Path

import pytest

from proofhunter.contracts import CandidateInput, ObservationOutcome
from research.execution import doubles
from research.execution import process_sandbox as ps
from research.execution.process_sandbox import ProcessTemplateExecutor, screen_source
from research.execution.runner import ResearchEpisodeRunner
from research.schemas.models import MeasurementMode, ProtocolV1

REPO = Path(__file__).resolve().parents[3]
PROTOCOL = ProtocolV1.model_validate_json((REPO / "research/spec/protocol-v1.json").read_text())
INPUT = CandidateInput(prices_cents=[101, 202], discount_percent=10)
GOOD = b"def total_cents(prices_cents, discount_percent):\n    return 1\n"
SPIN = (b"def total_cents(prices_cents, discount_percent):\n"
        b"    for _ in range(10 ** 12):\n        pass\n    return 0\n")
HOG = (b"def total_cents(prices_cents, discount_percent):\n    x = [1]\n"
       b"    for _ in range(40):\n        x = x + x\n    return 0\n")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _leftovers() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("ph-research-exec-*")}


def executor(*reviewed: bytes, timeout: float = 5) -> ProcessTemplateExecutor:
    return ProcessTemplateExecutor({_sha(s) for s in reviewed}.__contains__,
                                   timeout_seconds=timeout)


class Unscreened(ProcessTemplateExecutor):
    """Test-only: skips the gate so the wrapper's own limits can be exercised."""

    def refusal(self, source: bytes) -> str | None:
        return None


@pytest.fixture
def no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    ps.network_namespace_available()

    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("a process was started")
    monkeypatch.setattr(ps.subprocess, "Popen", refuse)


@pytest.mark.parametrize("source", [
    b"import os\ndef total_cents(p, d):\n    return 1\n",
    b"def total_cents(p, d):\n    return open('/etc/passwd')\n",
    b"def total_cents(p, d):\n    return __import__('os').system('id')\n",
    b"def total_cents(p, d):\n    return p.__class__\n",
    b"def total_cents(p, d):\n    while True:\n        pass\n",
    b"def other(p, d):\n    return 1\n",
    b"x = 1\ndef total_cents(p, d):\n    return x\n",
])
def test_screen_rejects_unreviewable_source(source: bytes, no_process: None) -> None:
    assert screen_source(source) is not None
    box = executor(source)
    result = box(  "head", source, INPUT)
    assert result.outcome is ObservationOutcome.DENIED
    assert result.refusal == screen_source(source)


def test_every_reviewed_bench_source_passes_the_screen() -> None:
    for case in doubles.BENCH_IDS:
        sources = doubles.bench_sources(case)
        assert screen_source(sources.base_source) is None
        assert screen_source(sources.head_source) is None


def test_non_allowlisted_source_denied_without_process(no_process: None) -> None:
    box = executor(GOOD)
    other = b"def total_cents(prices_cents, discount_percent):\n    return 2\n"
    result = box("base", other, INPUT)
    assert result.outcome is ObservationOutcome.DENIED
    assert result.refusal == "not_reviewed"
    assert result.startup_ms == 0
    assert box.attempts == 1
    assert box.log == [result]


def test_runner_counts_denied_attempts_and_never_starts_a_process(no_process: None) -> None:
    runner = ResearchEpisodeRunner(
        protocol=PROTOCOL, ledger=doubles.MemoryLedger(),
        executor=ProcessTemplateExecutor(lambda digest: False, timeout_seconds=1,
                                         network_namespace=False),
        review=doubles.bench_review, prompt_builder=doubles.bench_prompt,
        implementation=doubles.TEST_SHA)
    row = runner.run(doubles.bench_sources("per_line"), doubles.FixedOrderDouble(), None,
                     arm_id="fixed_order", seed=1, repetition=0, mode=MeasurementMode.OFFLINE)
    inv = row.invocations
    assert inv.failed == inv.base + inv.head + inv.confirm > 0
    assert row.verdict.value == "inconclusive"  # type: ignore[union-attr]


def test_reviewed_source_runs_with_phase_timings_and_cleanup() -> None:
    before = _leftovers()
    box = executor(GOOD)
    result = box("base", GOOD, INPUT)
    assert (result.outcome, result.value) == (ObservationOutcome.OK, 1)
    assert result.cleaned
    assert result.startup_ms > 0
    assert result.execution_ms > 0
    assert result.cleanup_ms > 0
    assert _leftovers() == before


def test_runner_reports_per_phase_timings_with_process_executor() -> None:
    reviewed = {s for c in doubles.BENCH_IDS for s in (
        doubles.bench_sources(c).case.base_source_sha256,
        doubles.bench_sources(c).case.head_source_sha256)}
    runner = ResearchEpisodeRunner(
        protocol=PROTOCOL, ledger=doubles.MemoryLedger(),
        executor=ProcessTemplateExecutor(reviewed.__contains__, timeout_seconds=5),
        review=doubles.bench_review, prompt_builder=doubles.bench_prompt,
        implementation=doubles.TEST_SHA)
    row = runner.run(doubles.bench_sources("per_line"), doubles.FixedOrderDouble(), None,
                     arm_id="fixed_order", seed=1, repetition=0, mode=MeasurementMode.OFFLINE)
    t = row.timings
    assert row.verdict.value == "confirmed_regression"  # type: ignore[union-attr]
    assert row.invocations.failed == 0
    assert t.sandbox_startup_ms
    assert t.execution_ms
    assert t.cleanup_ms
    assert t.confirmation_ms
    assert t.wall_ms >= t.sandbox_startup_ms + t.execution_ms + t.cleanup_ms


def test_timeout_is_bounded_and_counted() -> None:
    box = executor(SPIN, timeout=1)
    result = box("head", SPIN, INPUT)
    assert result.outcome is ObservationOutcome.TIMEOUT
    assert result.execution_ms < 3000
    assert result.cleaned
    assert box.attempts == 1


def test_output_is_bounded() -> None:
    loud = (b"import sys\ndef total_cents(p, d):\n"
            b"    sys.stdout.write('x' * 100000)\n    return 0\n")
    result = Unscreened(lambda d: True, timeout_seconds=5)("head", loud, INPUT)
    assert result.outcome is ObservationOutcome.INVALID
    assert result.truncated


def test_memory_limit_enforced() -> None:
    result = executor(HOG)("head", HOG, INPUT)
    assert result.outcome in (ObservationOutcome.NONZERO, ObservationOutcome.TIMEOUT)
    assert result.cleaned


def test_child_has_no_credentials_and_cannot_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFHUNTER_OPENAI_API_KEY", "should-not-leak")
    probe = (b"import os\ndef total_cents(p, d):\n"
             b"    leaked = any('KEY' in k or 'TOKEN' in k for k in os.environ)\n"
             b"    try:\n        open('x', 'w').write('1')\n        wrote = 1\n"
             b"    except OSError:\n        wrote = 0\n"
             b"    return int(leaked) * 10 + wrote\n")
    result = Unscreened(lambda d: True, timeout_seconds=5)("head", probe, INPUT)
    assert (result.outcome, result.value) == (ObservationOutcome.OK, 0)
    assert set(ps.ENV) == {"PATH", "LANG", "HOME"}


@pytest.mark.skipif(not ps.network_namespace_available(), reason="no user+net namespaces")
@pytest.mark.parametrize("address", ["169.254.169.254", "1.1.1.1", "192.0.2.1"])
def test_network_namespace_denies_egress_and_metadata(address: str) -> None:
    probe = (b"import socket\ndef total_cents(p, d):\n    s = socket.socket()\n"
             b"    s.settimeout(1)\n    return s.connect_ex((" + repr(address).encode() +
             b", 80))\n")
    box = Unscreened(lambda d: True, timeout_seconds=5)
    assert box.command(Path("/x"))[:3] == ["unshare", "--user", "--net"]
    result = box("head", probe, INPUT)
    assert result.outcome is ObservationOutcome.OK
    assert result.value != 0


def test_unprivileged_namespace_probe_is_consistent() -> None:
    if not ps.network_namespace_available():
        pytest.skip("no user+net namespaces")
    out = subprocess.run(["unshare", "--user", "--net", "--", "true"], check=False,
                         capture_output=True, timeout=10)
    assert out.returncode == 0


def test_child_program_is_fixed_and_minimal() -> None:
    tree = ast.parse(ps.CHILD)
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)
             for a in n.names}
    assert names == {"sys", "json", "total_cents"}


def test_make_runner_uses_process_executor() -> None:
    from research.execution.runner import make_runner

    runner = make_runner(PROTOCOL, doubles.MemoryLedger())
    assert isinstance(runner.executor, ProcessTemplateExecutor)
