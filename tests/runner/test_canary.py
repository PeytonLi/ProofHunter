"""Probe coverage with mocked sockets and child execution; never uses the network."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from proofhunter.runner.daytona import CANARY_EXPECTED


@pytest.mark.parametrize("reachable", [None, *CANARY_EXPECTED["probes_denied"], "controlled.test"])
def test_canary_probes_every_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reachable: str | None
) -> None:
    path = Path(__file__).resolve().parents[2] / "ops/sandbox/canary.py"
    spec = importlib.util.spec_from_file_location("trusted_canary", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def connect(address: tuple[str, int], **kwargs: Any) -> Any:
        calls.append(address)
        if address[0] == reachable:
            return io.BytesIO()
        raise OSError("denied")

    def resolve(*args: Any) -> Any:
        raise module.socket.gaierror("denied")

    monkeypatch.setattr(module.socket, "create_connection", connect)
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        module, "open", lambda *a, **kw: io.StringIO("controlled.test"), raising=False
    )
    monkeypatch.setattr(module.os, "environ", {})
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    module.main()
    result = json.loads(capsys.readouterr().out)
    assert set(calls) == {
        *((h, 443) for h in module.EGRESS_HOSTS),
        *((h, 80) for h in module.METADATA_HOSTS),
        ("controlled.test", 443),
    }
    if reachable is None:
        assert result == CANARY_EXPECTED
    else:
        assert not (result["egress_denied"] and result["metadata_denied"])
