"""Offline-safe configuration: no live defaults, no secrets, fixed origins."""

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest

from proofhunter import config
from proofhunter.ports import ConfigError

REPO = Path(__file__).resolve().parents[2]
FAKE = "fixture-value-not-a-secret"
PEM = "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----"


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


@pytest.mark.parametrize("role", list(config.ServiceRole))
def test_role_offline_ignores_secrets_and_rejects_flags(role: config.ServiceRole) -> None:
    env = dict.fromkeys(config.SECRET_VARIABLES, FAKE)
    env["GITHUB_APP_ID"] = FAKE
    settings = config.load_settings(env, role=role)
    assert settings == config.load_settings({}, role=role)
    assert settings.github.private_key is None
    with pytest.raises(ConfigError, match="unavailable"):
        config.github_private_key_pem(settings)
    for flag in ("PROOFHUNTER_ENABLE_LIVE_RUNNER", "PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",
                 "PROOFHUNTER_ENABLE_GITHUB_CHECKS"):
        with pytest.raises(ConfigError, match="offline"):
            config.load_settings({flag: "true"}, role=role)


@pytest.mark.parametrize("role", [config.ServiceRole.API, config.ServiceRole.AGENT])
def test_nonworker_roles_do_not_read_worker_secrets(role: config.ServiceRole) -> None:
    env = {"PROOFHUNTER_PROFILE": "live", "PROOFHUNTER_API_TOKEN": FAKE,
           "PROOFHUNTER_OPERATOR_TOKEN": FAKE + "-operator", "AGENT_SEED_PHRASE": FAKE}
    clean = config.load_settings(env, role=role)
    env.update(dict.fromkeys(("MODEL_API_KEY", "SUBQ_API_KEY", "DAYTONA_API_KEY",
                             "GITHUB_PRIVATE_KEY", "GITHUB_PRIVATE_KEY_PATH",
                             "GITHUB_APP_ID", "GITHUB_WEBHOOK_SECRET"), FAKE))
    settings = config.load_settings(env, role=role)
    assert settings == clean
    assert settings.meta.api_key is settings.snap.api_key is settings.daytona.api_key is None
    assert settings.github.private_key is settings.github.private_key_path is None
    assert settings.github.app_id is settings.github.webhook_secret is None
    if role is config.ServiceRole.API:
        assert settings.agent.seed_phrase is None
    else:
        assert settings.operator_token is None
    assert FAKE not in repr(settings)


@pytest.mark.parametrize(("role", "required"), [
    (config.ServiceRole.API, ("PROOFHUNTER_API_TOKEN", "PROOFHUNTER_OPERATOR_TOKEN",
                            "DAYTONA_SNAPSHOT", "DAYTONA_IMAGE_DIGEST")),
    (config.ServiceRole.AGENT, ("PROOFHUNTER_API_TOKEN", "AGENT_SEED_PHRASE")),
    (config.ServiceRole.WORKER, ("MODEL_API_KEY", "SUBQ_API_KEY", "GITHUB_APP_ID",
                               "GITHUB_PRIVATE_KEY_PATH", "DAYTONA_API_KEY",
                               "DAYTONA_SNAPSHOT", "DAYTONA_IMAGE_DIGEST")),
])
def test_roles_require_each_variable(role: config.ServiceRole, required: tuple[str, ...]) -> None:
    env = _live_env(AGENT_SEED_PHRASE=FAKE, PROOFHUNTER_ENABLE_LIVE_RUNNER="true",
                    DAYTONA_API_KEY=FAKE, DAYTONA_SNAPSHOT="fixture",
                    DAYTONA_IMAGE_DIGEST="sha256:" + "a" * 64)
    if role is config.ServiceRole.AGENT:
        env.pop("PROOFHUNTER_ENABLE_LIVE_RUNNER")
    config.load_settings(env, role=role)
    for name in required:
        missing = {key: value for key, value in env.items() if key != name}
        with pytest.raises(ConfigError, match=name) as error:
            config.load_settings(missing, role=role)
        assert FAKE not in str(error.value)


def test_combined_stack_shared_flags_and_policy() -> None:
    env = _live_env(PROOFHUNTER_ENABLE_LIVE_RUNNER="true",
                    PROOFHUNTER_ENABLE_GITHUB_CHECKS="true",
                    PROOFHUNTER_ENABLE_GITHUB_WEBHOOK="true",
                    DAYTONA_API_KEY=FAKE, DAYTONA_SNAPSHOT="live-snapshot",
                    DAYTONA_IMAGE_DIGEST="sha256:" + "a" * 64,
                    DAYTONA_API_URL="https://example.com", GITHUB_WEBHOOK_SECRET=FAKE,
                    AGENT_SEED_PHRASE=FAKE)
    env.pop("GITHUB_PRIVATE_KEY_PATH")
    env["GITHUB_PRIVATE_KEY"] = PEM
    api = config.load_settings(env, role=config.ServiceRole.API)
    worker = config.load_settings(env, role=config.ServiceRole.WORKER)
    assert config.public_mode_config(api, []) == config.public_mode_config(worker, [])
    assert config.mode_config_fingerprint(api, []) == config.mode_config_fingerprint(worker, [])
    for settings in (api, worker):
        assert settings.daytona.enable_live_runner
        assert settings.github.enable_checks
        assert settings.github.enable_webhook
        assert settings.daytona.snapshot == env["DAYTONA_SNAPSHOT"]
        assert settings.daytona.image_digest == env["DAYTONA_IMAGE_DIGEST"]
        assert settings.daytona.api_url == env["DAYTONA_API_URL"]
    assert api.meta.api_key is api.snap.api_key is api.daytona.api_key is None
    assert api.github.app_id is api.github.private_key is api.github.private_key_path is None
    assert api.agent.seed_phrase is None
    assert worker.api_token is worker.operator_token is worker.agent.seed_phrase is None
    assert worker.github.webhook_secret is None
    assert worker.daytona.api_key is not None
    assert worker.github.private_key is not None
    env.pop("DAYTONA_API_KEY")
    env.pop("GITHUB_WEBHOOK_SECRET")
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET"):
        config.load_settings(env, role=config.ServiceRole.API)
    with pytest.raises(ConfigError, match="DAYTONA_API_KEY"):
        config.load_settings(env, role=config.ServiceRole.WORKER)
    env["GITHUB_WEBHOOK_SECRET"] = FAKE
    config.load_settings(env, role=config.ServiceRole.API)


@pytest.mark.parametrize("flag_value", ["true", "not-a-boolean"])
def test_agent_ignores_shared_live_flags_and_worker_credentials(flag_value: str) -> None:
    env = dict.fromkeys(config.SECRET_VARIABLES, FAKE)
    env.update(PROOFHUNTER_PROFILE="live", GITHUB_APP_ID=FAKE,
               PROOFHUNTER_ENABLE_LIVE_RUNNER=flag_value,
               PROOFHUNTER_ENABLE_GITHUB_CHECKS=flag_value,
               PROOFHUNTER_ENABLE_GITHUB_WEBHOOK=flag_value,
               DAYTONA_API_URL=FAKE, DAYTONA_SNAPSHOT=FAKE, DAYTONA_IMAGE_DIGEST=FAKE)
    settings = config.load_settings(env, role=config.ServiceRole.AGENT)
    clean = config.load_settings({"PROOFHUNTER_PROFILE": "live",
                                  "PROOFHUNTER_API_TOKEN": FAKE, "AGENT_SEED_PHRASE": FAKE},
                                 role=config.ServiceRole.AGENT)
    assert settings == clean
    assert not settings.daytona.enable_live_runner
    assert not settings.github.enable_checks
    assert not settings.github.enable_webhook


def test_api_webhook_and_worker_tokens() -> None:
    env = _live_env()
    api = config.load_settings(env, role=config.ServiceRole.API)
    assert api.github.webhook_secret is None
    env["PROOFHUNTER_ENABLE_GITHUB_WEBHOOK"] = "true"
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET"):
        config.load_settings(env, role=config.ServiceRole.API)
    env["GITHUB_WEBHOOK_SECRET"] = FAKE
    assert config.load_settings(env, role=config.ServiceRole.API).github.webhook_secret
    env.pop("PROOFHUNTER_ENABLE_GITHUB_WEBHOOK")
    worker = config.load_settings(env, role=config.ServiceRole.WORKER)
    assert worker.api_token is worker.operator_token is worker.agent.seed_phrase is None
    assert worker.github.webhook_secret is None


def test_pem_sources_and_accessor(tmp_path: Path) -> None:
    env = _live_env(GITHUB_PRIVATE_KEY=PEM)
    with pytest.raises(ConfigError, match="mutually exclusive"):
        config.load_settings(env)
    env.pop("GITHUB_PRIVATE_KEY_PATH")
    env["GITHUB_PRIVATE_KEY"] = FAKE
    with pytest.raises(ConfigError) as error:
        config.load_settings(env)
    assert FAKE not in str(error.value)
    env["GITHUB_PRIVATE_KEY"] = PEM.replace("\n", "\\n")
    settings = config.load_settings(env)
    assert config.github_private_key_pem(settings).get_secret_value() == PEM
    assert PEM not in repr(settings)
    path = tmp_path / "key.pem"
    path.write_text(PEM)
    env.pop("GITHUB_PRIVATE_KEY")
    env["GITHUB_PRIVATE_KEY_PATH"] = str(path)
    fallback = config.load_settings(env)
    assert config.github_private_key_pem(fallback).get_secret_value() == PEM
    assert config.mode_config_fingerprint(settings, []) == config.mode_config_fingerprint(
        fallback, [])
    malformed = "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----"
    for content in (FAKE, "", malformed):
        path.write_text(content)
        with pytest.raises(ConfigError) as error:
            config.github_private_key_pem(fallback)
        assert FAKE not in str(error.value)
        assert str(path) not in str(error.value)
    path.unlink()
    with pytest.raises(ConfigError, match="unavailable") as error:
        config.github_private_key_pem(fallback)
    assert str(path) not in str(error.value)


def test_public_config_equal_across_roles_without_secret_hashes() -> None:
    env = _live_env(AGENT_SEED_PHRASE=FAKE, GITHUB_PRIVATE_KEY_PATH="/private/path",
                    DAYTONA_API_URL="https://example.com", DAYTONA_SNAPSHOT="fixture",
                    DAYTONA_IMAGE_DIGEST="sha256:" + "a" * 64)
    roles = (config.ServiceRole.ALL, config.ServiceRole.API, config.ServiceRole.WORKER)
    configurations = [config.public_mode_config(config.load_settings(env, role=role), [])
                      for role in roles]
    assert all(value == configurations[0] for value in configurations)
    fingerprints = [config.mode_config_fingerprint(config.load_settings(env, role=role), [])
                    for role in roles]
    assert len(set(fingerprints)) == 1
    public = json.dumps(configurations)
    for name in config.SECRET_VARIABLES:
        if name in env:
            assert env[name] not in public
            assert sha256(env[name].encode()).hexdigest() not in public
    for forbidden in ("role", "private_key", "private_key_path", "api_token"):
        assert forbidden not in public


def test_default_role_compatibility_and_invalid_role() -> None:
    assert config.ServiceRole.__module__ == "proofhunter.config"
    assert config.load_settings(_live_env()) == config.load_settings(
        _live_env(), role=config.ServiceRole.ALL)
    assert config.mode_config_fingerprint(config.load_settings({}), []) == (
        "a695a8568952e2d4a7a14730dcbd795295ec3feef3a836a3effe3b74d4f4dea9")
    with pytest.raises(ConfigError, match="role"):
        config.load_settings({}, role="invalid")  # type: ignore[arg-type]


def test_secret_allowlist_does_not_access_foreign_variables() -> None:
    class GuardedEnv(dict[str, str]):
        def get(self, key: str, default: str | None = None) -> str | None:
            assert key not in {"MODEL_API_KEY", "SUBQ_API_KEY", "DAYTONA_API_KEY",
                               "GITHUB_PRIVATE_KEY", "GITHUB_PRIVATE_KEY_PATH", "GITHUB_APP_ID",
                               "GITHUB_WEBHOOK_SECRET", "AGENT_SEED_PHRASE"}
            return super().get(key, default)

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("environment must not be copied")

    config.load_settings(GuardedEnv(PROOFHUNTER_PROFILE="live", PROOFHUNTER_API_TOKEN=FAKE,
                                   PROOFHUNTER_OPERATOR_TOKEN=FAKE + "-operator"),
                         role=config.ServiceRole.API)
