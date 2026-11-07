"""Offline-safe configuration: no live defaults, no secrets, fixed origins."""

from pathlib import Path

import pytest

from proofhunter import config
from proofhunter.ports import ConfigError

REPO = Path(__file__).resolve().parents[2]
FAKE = "fixture-value-not-a-secret"


def test_defaults_are_offline_and_disabled() -> None:
    settings = config.load_settings({})
    assert settings.offline
    assert settings.bind_host == "127.0.0.1"
    assert not settings.daytona.enable_live_runner
    assert not settings.github.enable_webhook
    assert not settings.github.enable_checks
    assert settings.github.api_version == "2026-03-10"
    assert settings.meta.model == "muse-spark-1.3"
    assert settings.snap.model == "subq-snap-1.1"
    assert settings.budget.deadline_seconds == 180
    assert settings.quota.accepted_runs_per_hour == 6


def test_offline_never_reads_secrets() -> None:
    env = {name: FAKE for name in config.SECRET_VARIABLES}
    settings = config.load_settings(env)
    for value in (settings.api_token, settings.operator_token, settings.meta.api_key,
                  settings.snap.api_key, settings.daytona.api_key, settings.github.webhook_secret,
                  settings.github.private_key_path, settings.agent.seed_phrase):
        assert value is None
    assert FAKE not in settings.model_dump_json()


@pytest.mark.parametrize("flag", ["PROOFHUNTER_ENABLE_LIVE_RUNNER",
                                  "PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",
                                  "PROOFHUNTER_ENABLE_GITHUB_CHECKS"])
def test_offline_rejects_live_flags(flag: str) -> None:
    with pytest.raises(ConfigError):
        config.load_settings({flag: "true"})


def test_invalid_profile_and_flag() -> None:
    with pytest.raises(ConfigError):
        config.load_settings({"PROOFHUNTER_PROFILE": "prod"})
    with pytest.raises(ConfigError):
        config.load_settings({"PROOFHUNTER_ENABLE_GITHUB_CHECKS": "yes"})


def _live_env(**extra: str) -> dict[str, str]:
    env = {"PROOFHUNTER_PROFILE": "live", "PROOFHUNTER_API_TOKEN": FAKE + "-a",
           "PROOFHUNTER_OPERATOR_TOKEN": FAKE + "-b", "MODEL_API_KEY": FAKE,
           "SUBQ_API_KEY": FAKE, "GITHUB_APP_ID": "123", "GITHUB_PRIVATE_KEY_PATH": "/k.pem"}
    env.update(extra)
    return env


def test_live_requires_credentials_and_separate_tokens() -> None:
    with pytest.raises(ConfigError, match="MODEL_API_KEY"):
        config.load_settings({"PROOFHUNTER_PROFILE": "live"})
    settings = config.load_settings(_live_env())
    assert settings.meta.api_key is not None
    assert FAKE not in repr(settings)
    with pytest.raises(ConfigError, match="differ"):
        config.load_settings(_live_env(PROOFHUNTER_OPERATOR_TOKEN=FAKE + "-a"))
    with pytest.raises(ConfigError, match="DAYTONA"):
        config.load_settings(_live_env(PROOFHUNTER_ENABLE_LIVE_RUNNER="true"))
    with pytest.raises(ConfigError, match="WEBHOOK"):
        config.load_settings(_live_env(PROOFHUNTER_ENABLE_GITHUB_WEBHOOK="true"))


def test_provider_origins_are_fixed() -> None:
    settings = config.load_settings({})
    assert settings.meta.url == "https://api.meta.ai/v1/chat/completions"
    assert settings.snap.url == "https://api.subq.ai/v1/systemone"
    assert settings.github.api_origin == "https://api.github.com"
    with pytest.raises(ValueError):
        config.MetaSettings(api_key=None, url="https://attacker.example")  # type: ignore[arg-type]


def test_env_example_lists_every_variable_without_secrets() -> None:
    lines = [ln for ln in (REPO / ".env.example").read_text().splitlines()
             if ln and not ln.startswith("#")]
    values = dict(ln.split("=", 1) for ln in lines)
    assert set(values) == set(config.KNOWN_VARIABLES)
    for name in config.SECRET_VARIABLES:
        assert values[name] == "", name
    assert values["PROOFHUNTER_PROFILE"] == "offline"
    for flag in ("PROOFHUNTER_ENABLE_LIVE_RUNNER", "PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",
                 "PROOFHUNTER_ENABLE_GITHUB_CHECKS"):
        assert values[flag] == "false"
    config.load_settings(values)


def test_example_enrollments_load() -> None:
    enrollments = config.load_enrollments(REPO / "config" / "enrollments.example.json")
    assert [e.repo_full_name for e in enrollments] == ["proofhunter-demo/checkout-demo"]
    with pytest.raises(ConfigError):
        config.load_enrollments(REPO / "pyproject.toml")


def test_gitignore_covers_local_state() -> None:
    text = (REPO / ".gitignore").read_text()
    for entry in (".venv/", ".env", ".proofhunter/", "*.sqlite3", "*.log", "*-receipt.json"):
        assert entry in text
