"""Probe coverage with mocked sockets and child execution; never uses the network."""

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from proofhunter.runner.approval import CANARY_EXPECTED, CHILD_TARGETS, DNS_NAMES, METADATA_TARGETS


def canary() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "ops/sandbox/canary.py"
    spec = importlib.util.spec_from_file_location("trusted_canary", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConn(io.BytesIO):
    def __init__(self, head: bytes) -> None:
        super().__init__(head)

    def settimeout(self, value: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        assert data.startswith(b"GET / HTTP/1.0")

    def recv(self, count: int) -> bytes:
        return self.read(count)


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    resolves: str | None = None,
    metadata: bytes | None = None,
    child: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    module = canary()
    assert module.DNS_NAMES == DNS_NAMES
    assert module.METADATA_TARGETS == METADATA_TARGETS
    assert module.CHILD_TARGETS == CHILD_TARGETS
    calls = []

    def connect(address: tuple[str, int], **kwargs: Any) -> Any:
        calls.append(address)
        if metadata is None:
            raise OSError("denied")
        return FakeConn(metadata)

    def resolve(name: str, *args: Any) -> Any:
        if name == resolves:
            return [("ok",)]
        raise module.socket.gaierror("denied")

    def subprocess_run(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["preexec_fn"] is module.isolate
        assert json.loads(kwargs["env"]["PH_TARGETS"]) == list(CHILD_TARGETS)
        out = child if child is not None else {
            "uid": 65534, "groups": [], **dict.fromkeys(CHILD_TARGETS, True)
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(out))

    monkeypatch.setattr(module.socket, "create_connection", connect)
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.os, "environ", dict(env or {}))
    monkeypatch.setattr(module.Path, "exists", lambda self: False)
    monkeypatch.setattr(module.subprocess, "run", subprocess_run)
    module.main()
    assert {f"{h}:{p}" for h, p in calls} == set(METADATA_TARGETS)
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    return result


def test_canary_expected_when_everything_denied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(monkeypatch, capsys) == CANARY_EXPECTED


def test_provider_403_filter_counts_as_metadata_denied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = b"HTTP/1.1 403 Forbidden\r\n\r\nInternet is restricted on Tier 1 and Tier 2."
    assert run(monkeypatch, capsys, metadata=head) == CANARY_EXPECTED


def test_metadata_service_answering_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(monkeypatch, capsys, metadata=b"HTTP/1.1 200 OK\r\n\r\nami-id")
    assert result["metadata_denied"] is False


@pytest.mark.parametrize("name", DNS_NAMES)
def test_any_resolving_name_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    result = run(monkeypatch, capsys, resolves=name)
    assert result["dns_denied"] is False
    assert result["probes_denied"][f"dns:{name}"] is False


@pytest.mark.parametrize("target", CHILD_TARGETS)
def test_child_reaching_any_target_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    child = {"uid": 65534, "groups": [], **dict.fromkeys(CHILD_TARGETS, True), target: False}
    result = run(monkeypatch, capsys, child=child)
    assert result["child_network_isolated"] is False


def test_child_still_root_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child = {"uid": 0, "groups": [], **dict.fromkeys(CHILD_TARGETS, True)}
    result = run(monkeypatch, capsys, child=child)
    assert result["restricted_child"] is False
    assert result["child_network_isolated"] is False


def test_secret_like_environment_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(monkeypatch, capsys, env={"DAYTONA_API_KEY": "redacted"})
    assert result["secrets_absent"] is False


def test_pinned_image_gpg_key_is_not_a_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run(monkeypatch, capsys, env={"GPG_KEY": "public-fingerprint"})
    assert result["secrets_absent"] is True
