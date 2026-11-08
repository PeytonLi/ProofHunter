"""Exercise the trusted wrapper without spawning any local target process."""

import importlib.util
import io
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def wrapper() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "ops/sandbox/wrapper.py"
    spec = importlib.util.spec_from_file_location("trusted_wrapper", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePath:
    def read_bytes(self) -> bytes:
        return b"reviewed source bytes, never executed"

    def __truediv__(self, other: str) -> "FakePath":
        return self

    def mkdir(self, *, mode: int) -> None:
        assert mode == 0o755

    def write_bytes(self, source: bytes) -> None:
        assert source == self.read_bytes()

    def chmod(self, mode: int) -> None:
        assert mode == 0o444


class FakeSelector:
    def __init__(self) -> None:
        self.entries: dict[Any, Any] = {}

    def register(self, stream: io.BytesIO, event: int, data: str) -> None:
        self.entries[stream] = SimpleNamespace(fd=stream, fileobj=stream, data=data)

    def unregister(self, stream: io.BytesIO) -> None:
        del self.entries[stream]

    def get_map(self) -> dict[Any, Any]:
        return self.entries

    def select(self, timeout: float) -> list[Any]:
        return [(entry, None) for entry in self.entries.values()]

    def close(self) -> None:
        self.entries.clear()


@pytest.mark.parametrize(
    ("raw", "code", "expected"),
    [
        (b'{"value":42}', 0, ("ok", 42)),
        (b'{"value":0}', 0, ("ok", 0)),
        (b"", 0, None),
        (b'{"value":true}', 0, ("invalid", None)),
        (b'{"value":42.0}', 0, ("invalid", None)),
        (b'{"value":1,"value":2}', 0, None),
        (b"x" * 16385, 0, ("invalid", None)),
        (b'{"value":42}', 1, ("nonzero", None)),
    ],
)
def test_bounded_wrapper_streams(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    code: int,
    expected: tuple[str, int | None] | None,
) -> None:
    module = wrapper()
    stdin, stdout, stderr = io.BytesIO(), io.BytesIO(raw), io.BytesIO()
    killed = []
    process = SimpleNamespace(
        stdin=stdin, stdout=stdout, stderr=stderr, pid=123, wait=lambda **kwargs: code
    )

    def popen(command: list[str], **kwargs: Any) -> Any:
        assert command[:4] == ["/usr/local/bin/python3", "-I", "-B", "-c"]
        assert kwargs["env"] == module.ENV
        assert kwargs["start_new_session"] is True
        return process

    monkeypatch.setattr(module, "Path", lambda _: FakePath())
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.selectors, "DefaultSelector", FakeSelector)
    monkeypatch.setattr(module.os, "read", lambda fd, count: fd.read(count))
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: killed.append(pid))
    data = {
        "resources": {
            "execution_timeout_seconds": 5,
            "memory_mib": 256,
            "max_processes": 16,
            "max_output_bytes": 16384,
        },
        "input": {"prices_cents": [], "discount_percent": 10},
    }
    if expected is None:
        with pytest.raises(ValueError):
            module.execute(data)
    else:
        assert module.execute(data) == (*expected, len(raw) > 16384)
    assert killed == [123]
    assert stdout.closed
    assert stderr.closed


def test_resource_policy_rejects_booleans() -> None:
    with pytest.raises(ValueError, match="policy"):
        wrapper().execute(
            {
                "resources": {
                    "execution_timeout_seconds": True,
                    "memory_mib": 256,
                    "max_processes": 16,
                    "max_output_bytes": 16384,
                }
            }
        )


def test_child_network_namespace_before_privilege_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    module = wrapper()
    calls: list[Any] = []
    monkeypatch.setattr(module.os, "unshare", lambda flags: calls.append(("unshare", flags)))
    monkeypatch.setattr(module.os, "setgroups", lambda groups: calls.append(("setgroups", groups)))
    monkeypatch.setattr(module.os, "setgid", lambda gid: calls.append(("setgid", gid)))
    monkeypatch.setattr(module.os, "setuid", lambda uid: calls.append(("setuid", uid)))
    monkeypatch.setattr(
        module.ctypes, "CDLL", lambda _: SimpleNamespace(prctl=lambda *a: calls.append("nnp") or 0)
    )
    monkeypatch.setattr(module.resource, "setrlimit", lambda kind, limit: calls.append("rlimit"))
    module.restrict(256, 16, 5)
    assert calls[:5] == [
        ("unshare", module.os.CLONE_NEWNET),
        ("setgroups", []),
        ("setgid", 65534),
        ("setuid", 65534),
        "nnp",
    ]


def test_namespace_failure_aborts_before_privilege_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    module = wrapper()

    def refuse(flags: int) -> None:
        raise PermissionError("unshare refused")

    monkeypatch.setattr(module.os, "unshare", refuse)
    monkeypatch.setattr(module.os, "setuid", lambda uid: pytest.fail("continued without netns"))
    with pytest.raises(PermissionError):
        module.restrict(256, 16, 5)
