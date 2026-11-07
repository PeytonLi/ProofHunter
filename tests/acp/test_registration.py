from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from uagents_core.registration import AgentProfile

from proofhunter.acp import registration
from proofhunter.acp.__main__ import main

KEY = "test-only-agentverse-key"


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name, value in {
        "PROOFHUNTER_PROFILE": "live", "PROOFHUNTER_API_BASE": "http://localhost:8000",
        "AGENT_SEED_PHRASE": "test-only-seed", "PROOFHUNTER_API_TOKEN": "test-only-token",
        "AGENTVERSE_API_KEY": KEY,
        "PROOFHUNTER_ACP_PUBLISH": "true",
    }.items():
        monkeypatch.setenv(name, value)
    fake = SimpleNamespace(
        address="agent1test", name="proofhunter", _handle=None, _prefix="agent",
        _endpoints=[], protocols={"chat-digest": object()}, metadata={},
        identity=object(), agentverse=object(), run=Mock(),
        _build_registration_profile=lambda: AgentProfile(description="public metadata"),
    )
    monkeypatch.setattr("proofhunter.acp.__main__.build_agent", Mock(return_value=fake))
    monkeypatch.setattr("proofhunter.acp.__main__.prepare_storage", Mock())
    return fake


def test_register_uses_sdk_connect_flow(agent: Any, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    sdk = AsyncMock(return_value=SimpleNamespace(success=True, detail=KEY))
    monkeypatch.setattr(registration, "register_in_agentverse", sdk)
    assert main(["register"]) == 0
    kwargs = sdk.call_args.kwargs
    assert kwargs["request"].user_token == KEY
    assert kwargs["request"].agent_type == "mailbox"
    assert kwargs["identity"] is agent.identity
    assert kwargs["agent_details"].protocols == ["chat-digest"]
    assert kwargs["agent_details"].profile.description == "public metadata"
    assert KEY not in repr(kwargs["agent_details"])
    assert not agent.run.called
    output = capsys.readouterr()
    assert output.out == "registration succeeded\n"
    assert KEY not in output.err


@pytest.mark.parametrize("key", [None, "", "   "])
def test_register_requires_key(agent: Any, key: str | None,
                               monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:
    if key is None:
        monkeypatch.delenv("AGENTVERSE_API_KEY")
    else:
        monkeypatch.setenv("AGENTVERSE_API_KEY", key)
    assert main(["register"]) == 2
    assert "AGENTVERSE_API_KEY is required" in capsys.readouterr().err
    assert not agent.run.called


@pytest.mark.parametrize("exception", [False, True])
def test_registration_failure_is_sanitized(agent: Any, exception: bool,
                                            monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    sdk = AsyncMock(return_value=SimpleNamespace(success=False, detail=KEY))
    if exception:
        sdk.side_effect = RuntimeError(KEY)
    monkeypatch.setattr(registration, "register_in_agentverse", sdk)
    assert main(["register"]) == 1
    output = capsys.readouterr()
    assert "registration failed" in output.out
    assert KEY not in output.out + output.err


def test_mailbox_run_does_not_read_registration_key(agent: Any,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("must not read registration key"))
    monkeypatch.setattr("proofhunter.acp.__main__.registration_key", forbidden)
    assert main([]) == 0
    agent.run.assert_called_once()
    forbidden.assert_not_called()


def test_registration_rejects_conflicting_flags(agent: Any) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["register", "--address"])


@pytest.mark.parametrize("command", [[], ["register"]])
@pytest.mark.parametrize("flag", [None, "", "false", "invalid"])
def test_default_off_blocks_all_agent_network_startup(
    agent: Any, command: list[str], flag: str | None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    if flag is None:
        monkeypatch.delenv("PROOFHUNTER_ACP_PUBLISH")
    else:
        monkeypatch.setenv("PROOFHUNTER_ACP_PUBLISH", flag)
    builder = Mock(side_effect=AssertionError("must not construct publishing agent"))
    monkeypatch.setattr("proofhunter.acp.__main__.build_agent", builder)
    assert main(command) == 2
    assert "isolated three-run gate" in capsys.readouterr().err
    builder.assert_not_called()
