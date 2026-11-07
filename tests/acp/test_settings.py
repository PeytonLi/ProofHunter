from __future__ import annotations

from pathlib import Path

import pytest

from proofhunter.acp.__main__ import main
from proofhunter.acp.client import create_control_client
from proofhunter.acp.service import ChatService, MemoryKeyValue, ReplyLedger
from proofhunter.acp.settings import load_acp_settings, prepare_storage
from proofhunter.ports import ConfigError

from .conftest import PR_URL, FakeControlHttp


def role_env() -> dict[str, str]:
    return {"PROOFHUNTER_PROFILE": "live", "AGENT_SEED_PHRASE": "test-only-seed",
            "PROOFHUNTER_API_TOKEN": "test-only-token",
            "PROOFHUNTER_API_BASE": "http://control.internal:8080"}


def test_role_credentials_only_and_unrelated_config_ignored() -> None:
    env = role_env()
    env.update({"MODEL_API_KEY": "forbidden", "SUBQ_API_KEY": "forbidden",
                "GITHUB_APP_ID": "invalid", "GITHUB_PRIVATE_KEY": "invalid",
                "DAYTONA_API_KEY": "forbidden", "PROOFHUNTER_OPERATOR_TOKEN": "forbidden"})
    settings = load_acp_settings(env)
    assert settings.meta.api_key is settings.snap.api_key is None
    assert settings.github.app_id is None
    assert settings.github.private_key is None
    assert settings.daytona.api_key is settings.operator_token is None
    assert "test-only-token" not in repr(settings)
    assert "test-only-seed" not in repr(settings)


@pytest.mark.parametrize("key", ["AGENT_SEED_PHRASE", "PROOFHUNTER_API_TOKEN",
                                 "PROOFHUNTER_API_BASE"])
@pytest.mark.parametrize("empty", [True, False])
def test_missing_role_settings(key: str, empty: bool) -> None:
    env = role_env()
    if empty:
        env[key] = ""
    else:
        del env[key]
    with pytest.raises(ConfigError, match=key):
        load_acp_settings(env)


async def test_role_settings_wire_real_http_client() -> None:
    http = FakeControlHttp()
    client = create_control_client(load_acp_settings(role_env()), transport=http.transport())
    svc = ChatService(client, ReplyLedger(MemoryKeyValue()))
    reply = await svc.reply_for("sender", "message", f"verify {PR_URL}")
    assert "queued" in reply
    assert await svc.reply_for("sender", "message", f"verify {PR_URL}") == reply
    assert len(http.requests) == 1
    assert http.requests[0].headers["authorization"] == "Bearer test-only-token"
    assert http.requests[0].headers["idempotency-key"]


def test_storage_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    prepare_storage({"PROOFHUNTER_AGENT_STORAGE_DIR": str(tmp_path / "mailbox")})
    assert Path.cwd() == tmp_path / "mailbox"
    with pytest.raises(ConfigError):
        prepare_storage({"PROOFHUNTER_AGENT_STORAGE_DIR": ""})


def test_startup_reports_safe_error(monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    for key, value in role_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENT_SEED_PHRASE")
    assert main([]) == 2
    output = capsys.readouterr().err
    assert "AGENT_SEED_PHRASE" in output
    assert "test-only-token" not in output
