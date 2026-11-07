from __future__ import annotations

import ast
import subprocess
import tempfile
from pathlib import Path

import pytest

from fixtures.catalog import demo_enrollment
from proofhunter.contracts import ObservationOutcome
from research.execution import sandbox as sb
from research.execution.allowlist import ExecutionRefused, ReviewedSourceAllowlist
from research.execution.sandbox import LocalTemplateSandbox

GOOD = b"def total_cents(prices_cents, discount_percent):\n    return 1\n"
SPIN = (b"def total_cents(prices_cents, discount_percent):\n"
        b"    for _ in range(10 ** 12):\n        pass\n    return 0\n")
LOUD = b"def total_cents(prices_cents, discount_percent):\n    return [0] * 100000\n"
HOG = (b"def total_cents(prices_cents, discount_percent):\n    x = [1]\n"
       b"    for _ in range(40):\n        x = x + x\n    return 0\n")


def _leftovers() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("ph-research-exec-*")}


@pytest.fixture
def make_box(reviewed):
    return lambda *sources: LocalTemplateSandbox(reviewed(*sources), demo_enrollment())


@pytest.fixture
def no_process(monkeypatch):
    sb.network_namespace_available()
    def refuse(*a, **k):
        raise AssertionError("a process was started")
    monkeypatch.setattr(sb.subprocess, "Popen", refuse)


@pytest.mark.parametrize("source", [
    b"import os\ndef total_cents(p, d):\n    return 1\n",
    b"def total_cents(p, d):\n    return open('/etc/passwd')\n",
    b"def total_cents(p, d):\n    return __import__('os').system('id')\n",
    b"def total_cents(p, d):\n    return p.__class__\n",
    b"def total_cents(p, d):\n    while True:\n        pass\n",
    b"def other(p, d):\n    return 1\n",
])
def test_allowlist_rejects_unreviewable_source(source, reviewed):
    with pytest.raises(ExecutionRefused):
        reviewed(source)


def test_allowlist_rejects_hash_not_matching_bytes():
    with pytest.raises(ExecutionRefused):
        ReviewedSourceAllowlist({"0" * 64: GOOD}, "bad")


def test_non_allowlisted_source_denied_without_process(no_process, make_box, request_for):
    box = make_box(GOOD)
    other = b"def total_cents(prices_cents, discount_percent):\n    return 2\n"
    obs, timing = box.invoke(request_for(other))
    assert obs.outcome is ObservationOutcome.DENIED
    assert timing.refusal is not None
    assert box.invocations == 1
    assert timing.startup_ms == 0


def test_tampered_bytes_under_allowlisted_hash_denied(no_process, make_box, request_for):
    tampered = b"def total_cents(prices_cents, discount_percent):\n    return 3\n"
    forged = request_for(GOOD).model_copy(update={"source_content": tampered.decode()})
    obs, _ = make_box(GOOD).invoke(forged)
    assert obs.outcome is ObservationOutcome.DENIED


def test_enrollment_binding_mismatch_denied(no_process, make_box, request_for):
    box = make_box(GOOD)
    bad = request_for(GOOD).model_copy(update={"snapshot_id": "other-snapshot"})
    obs, timing = box.invoke(bad)
    assert obs.outcome is ObservationOutcome.DENIED
    assert timing.refusal == "enrollment_mismatch"


def test_reviewed_source_runs_and_cleans_up(make_box, request_for):
    before = _leftovers()
    box = make_box(GOOD)
    obs, timing = box.invoke(request_for(GOOD))
    assert obs.outcome is ObservationOutcome.OK
    assert obs.value == 1
    assert timing.cleaned
    assert timing.startup_ms > 0
    assert timing.execution_ms > 0
    assert _leftovers() == before


def test_timeout_is_bounded_and_counted(make_box, request_for):
    box = make_box(SPIN)
    obs, timing = box.invoke(request_for(SPIN, timeout=1))
    assert obs.outcome is ObservationOutcome.TIMEOUT
    assert timing.execution_ms < 3000
    assert box.invocations == 1
    assert timing.cleaned


def test_output_is_bounded(make_box, request_for):
    obs, _ = make_box(LOUD).invoke(request_for(LOUD))
    assert obs.outcome is ObservationOutcome.INVALID
    assert obs.truncated


def test_memory_limit_enforced(make_box, request_for):
    obs, _ = make_box(HOG).invoke(request_for(HOG))
    assert obs.outcome in (ObservationOutcome.NONZERO, ObservationOutcome.TIMEOUT)


def test_child_environment_has_no_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "should-not-leak")
    assert "GITHUB_TOKEN" not in sb.ENV
    assert set(sb.ENV) == {"PATH", "LANG", "HOME"}


@pytest.mark.skipif(not sb.network_namespace_available(), reason="no user+net namespaces")
@pytest.mark.parametrize("address", ["169.254.169.254", "1.1.1.1", "192.0.2.1"])
def test_network_namespace_denies_egress_and_metadata(address, make_box):
    probe = (f"import socket;s=socket.socket();s.settimeout(1);"
             f"print(s.connect_ex(({address!r},80)))")
    box = make_box(GOOD)
    cmd = box.command(Path("/nonexistent"))
    assert cmd[:3] == ["unshare", "--user", "--net"]
    out = subprocess.run(["unshare", "--user", "--net", "--", sb._python(), "-I", "-S", "-c",
                          probe], capture_output=True, timeout=10, env=sb.ENV, check=True)
    assert out.stdout.strip() not in (b"", b"0")


def test_child_program_is_fixed_and_minimal():
    tree = ast.parse(sb.CHILD)
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)
             for a in n.names}
    assert names == {"sys", "json", "total_cents"}
