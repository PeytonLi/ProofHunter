"""Shared configuration with offline-safe defaults (PRD section 9).

``load_settings(environ)`` is the only entry point. Defaults are offline: the
fixture profile never reads provider credentials, every live feature flag
defaults to ``false`` and enabling one in the offline profile is a
``ConfigError``. Provider origins are fixed constants and cannot be configured.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, ValidationError

from proofhunter.contracts import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TTL_SECONDS,
    MAX_HEARTBEAT_TTL_SECONDS,
    SCHEMA_VERSION,
    BudgetLimits,
    Enrollment,
    EnrollmentRegistry,
    PolicyVersions,
    QuotaPolicy,
    canonical_sha256,
    enrollment_fingerprint,
)
from proofhunter.contracts import (
    Profile as Profile,
)
from proofhunter.ports import ConfigError

# Fixed provider origins and pinned identifiers (never user-supplied).
META_BASE_URL: Final = "https://api.meta.ai/v1"
META_CHAT_COMPLETIONS_URL: Final = f"{META_BASE_URL}/chat/completions"
META_DEFAULT_MODEL: Final = "muse-spark-1.3"
SUBQ_BASE_URL: Final = "https://api.subq.ai/v1"
SUBQ_DECISIONS_URL: Final = f"{SUBQ_BASE_URL}/systemone"
SUBQ_MODELS_URL: Final = f"{SUBQ_BASE_URL}/models"
SUBQ_DEFAULT_MODEL: Final = "subq-snap-1.1"
GITHUB_API_ORIGIN: Final = "https://api.github.com"
GITHUB_WEB_ORIGIN: Final = "https://github.com"
GITHUB_DEFAULT_API_VERSION: Final = "2026-03-10"
AGENTVERSE_ORIGIN: Final = "https://agentverse.ai"

#: Environment variables that hold secrets. The offline profile never reads them.
SECRET_VARIABLES: Final = (
    "PROOFHUNTER_API_TOKEN",
    "PROOFHUNTER_OPERATOR_TOKEN",
    "MODEL_API_KEY",
    "SUBQ_API_KEY",
    "DAYTONA_API_KEY",
    "GITHUB_PRIVATE_KEY_PATH",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "AGENT_SEED_PHRASE",
)
#: Every variable understood by ``load_settings`` (documented in .env.example).
KNOWN_VARIABLES: Final = (
    "PROOFHUNTER_PROFILE",
    "PROOFHUNTER_DB_PATH",
    "PROOFHUNTER_ENROLLMENTS",
    "PROOFHUNTER_API_TOKEN",
    "PROOFHUNTER_API_BASE",
    "PROOFHUNTER_OPERATOR_TOKEN",
    "PROOFHUNTER_BIND_HOST",
    "PROOFHUNTER_BIND_PORT",
    "PROOFHUNTER_ACCEPTED_RUNS_PER_HOUR",
    "PROOFHUNTER_ENABLE_INTAKE",
    "MODEL_API_KEY",
    "META_MODEL",
    "SUBQ_API_KEY",
    "SUBQ_MODEL",
    "DAYTONA_API_KEY",
    "DAYTONA_API_URL",
    "DAYTONA_SNAPSHOT",
    "DAYTONA_IMAGE_DIGEST",
    "PROOFHUNTER_ENABLE_LIVE_RUNNER",
    "GITHUB_APP_ID",
    "GITHUB_PRIVATE_KEY_PATH",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_API_VERSION",
    "GITHUB_WEBHOOK_SECRET",
    "PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",
    "PROOFHUNTER_ENABLE_GITHUB_CHECKS",
    "AGENT_SEED_PHRASE",
    "AGENT_PORT",
)
DEFAULT_ENROLLMENTS: Final = "config/enrollments.example.json"
DEFAULT_DB_PATH: Final = ".proofhunter/proofhunter.sqlite3"


class ServiceRole(StrEnum):
    ALL = "all"
    API = "api"
    WORKER = "worker"
    AGENT = "agent"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetaSettings(_Frozen):
    api_key: SecretStr | None
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = META_DEFAULT_MODEL
    url: Literal["https://api.meta.ai/v1/chat/completions"] = "https://api.meta.ai/v1/chat/completions"


class SnapSettings(_Frozen):
    api_key: SecretStr | None
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = SUBQ_DEFAULT_MODEL
    url: Literal["https://api.subq.ai/v1/systemone"] = "https://api.subq.ai/v1/systemone"
    models_url: Literal["https://api.subq.ai/v1/models"] = "https://api.subq.ai/v1/models"


class DaytonaSettings(_Frozen):
    api_key: SecretStr | None
    api_url: Annotated[str, Field(pattern=r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]*)?$")] | None
    snapshot: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:@/+=-]{1,128}$")] | None
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    enable_live_runner: StrictBool = False


class GithubSettings(_Frozen):
    app_id: Annotated[StrictInt, Field(ge=1)] | None
    private_key_path: SecretStr | None
    private_key: SecretStr | None = None
    api_version: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] = GITHUB_DEFAULT_API_VERSION
    webhook_secret: SecretStr | None
    enable_webhook: StrictBool = False
    enable_checks: StrictBool = False
    api_origin: Literal["https://api.github.com"] = GITHUB_API_ORIGIN


class AgentSettings(_Frozen):
    """uAgents ACP process. Mailbox mode: ``Agent(seed=..., mailbox=True)``."""

    seed_phrase: SecretStr | None
    port: Annotated[StrictInt, Field(ge=1024, le=65535)] = 8001
    mailbox: StrictBool = True


class Settings(_Frozen):
    profile: Profile
    db_path: Path
    enrollments_path: Path
    api_token: SecretStr | None
    operator_token: SecretStr | None
    api_base: Annotated[str, Field(pattern=r"^https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._/-]*)?$")]
    bind_host: str = "127.0.0.1"
    bind_port: Annotated[StrictInt, Field(ge=1, le=65535)] = 8000
    enable_intake: StrictBool = True
    budget: BudgetLimits = BudgetLimits()
    quota: QuotaPolicy = QuotaPolicy()
    meta: MetaSettings
    snap: SnapSettings
    daytona: DaytonaSettings
    github: GithubSettings
    agent: AgentSettings

    @property
    def offline(self) -> bool:
        return self.profile is Profile.OFFLINE


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "false").strip().lower()
    if value not in {"true", "false", "1", "0", ""}:
        raise ConfigError(f"{name} must be true or false")
    return value in {"true", "1"}


def _secret(env: Mapping[str, str], name: str) -> SecretStr | None:
    value = env.get(name, "")
    return SecretStr(value) if value else None


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def load_settings(
    environ: Mapping[str, str] | None = None, *, role: ServiceRole = ServiceRole.ALL,
) -> Settings:
    """Build validated settings. Raises ``ConfigError`` on unsafe or invalid config."""
    env = os.environ if environ is None else environ
    try:
        role = ServiceRole(role)
    except ValueError:
        raise ConfigError("role must be all, api, worker or agent") from None
    raw_profile = env.get("PROOFHUNTER_PROFILE", "offline").strip() or "offline"
    try:
        profile = Profile(raw_profile)
    except ValueError as error:
        raise ConfigError("PROOFHUNTER_PROFILE must be offline or live") from error
    live = profile is Profile.LIVE

    flags = {name: _flag(env, name) for name in (
        "PROOFHUNTER_ENABLE_LIVE_RUNNER", "PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",
        "PROOFHUNTER_ENABLE_GITHUB_CHECKS")}
    if not live and any(flags.values()):
        enabled = sorted(name for name, on in flags.items() if on)
        raise ConfigError(f"offline profile cannot enable live features: {enabled}")

    allowed = {
        ServiceRole.ALL: set(SECRET_VARIABLES) | {"GITHUB_APP_ID"},
        ServiceRole.API: {"PROOFHUNTER_API_TOKEN", "PROOFHUNTER_OPERATOR_TOKEN"},
        ServiceRole.WORKER: {"MODEL_API_KEY", "SUBQ_API_KEY", "GITHUB_APP_ID",
                             "GITHUB_PRIVATE_KEY", "GITHUB_PRIVATE_KEY_PATH", "DAYTONA_API_KEY"},
        ServiceRole.AGENT: {"PROOFHUNTER_API_TOKEN", "AGENT_SEED_PHRASE"},
    }[role]
    if flags["PROOFHUNTER_ENABLE_GITHUB_WEBHOOK"] and role in {ServiceRole.ALL, ServiceRole.API}:
        allowed.add("GITHUB_WEBHOOK_SECRET")
    forbidden_flags = {
        ServiceRole.ALL: (),
        ServiceRole.API: ("PROOFHUNTER_ENABLE_LIVE_RUNNER", "PROOFHUNTER_ENABLE_GITHUB_CHECKS"),
        ServiceRole.WORKER: ("PROOFHUNTER_ENABLE_GITHUB_WEBHOOK",),
        ServiceRole.AGENT: tuple(flags),
    }[role]
    if any(flags[name] for name in forbidden_flags):
        raise ConfigError("service role cannot enable features: " +
                          ", ".join(name for name in forbidden_flags if flags[name]))

    def secret(name: str) -> SecretStr | None:
        return _secret(env, name) if live and name in allowed else None

    def opt(name: str) -> str | None:
        return _opt(env, name) if live else None

    try:
        app_id = opt("GITHUB_APP_ID") if "GITHUB_APP_ID" in allowed else None
        private_key = secret("GITHUB_PRIVATE_KEY")
        private_key_path = secret("GITHUB_PRIVATE_KEY_PATH")
        if private_key is not None and private_key_path is not None:
            raise ConfigError(
                "GITHUB_PRIVATE_KEY and GITHUB_PRIVATE_KEY_PATH are mutually exclusive")
        if private_key is not None:
            private_key = _validated_pem(private_key.get_secret_value())
        per_hour = env.get("PROOFHUNTER_ACCEPTED_RUNS_PER_HOUR", "").strip()
        quota = QuotaPolicy(accepted_runs_per_hour=int(per_hour)) if per_hour else QuotaPolicy()
        settings = Settings(
            profile=profile,
            db_path=Path(env.get("PROOFHUNTER_DB_PATH") or DEFAULT_DB_PATH),
            enrollments_path=Path(env.get("PROOFHUNTER_ENROLLMENTS") or DEFAULT_ENROLLMENTS),
            api_token=secret("PROOFHUNTER_API_TOKEN"),
            operator_token=secret("PROOFHUNTER_OPERATOR_TOKEN"),
            api_base=env.get("PROOFHUNTER_API_BASE") or "http://127.0.0.1:8000",
            bind_host=env.get("PROOFHUNTER_BIND_HOST") or "127.0.0.1",
            bind_port=int(env.get("PROOFHUNTER_BIND_PORT") or 8000),
            enable_intake=_flag({"x": env.get("PROOFHUNTER_ENABLE_INTAKE", "true")}, "x"),
            quota=quota,
            meta=MetaSettings(api_key=secret("MODEL_API_KEY"),
                              model=env.get("META_MODEL") or META_DEFAULT_MODEL),
            snap=SnapSettings(api_key=secret("SUBQ_API_KEY"),
                              model=env.get("SUBQ_MODEL") or SUBQ_DEFAULT_MODEL),
            daytona=DaytonaSettings(
                api_key=secret("DAYTONA_API_KEY"), api_url=opt("DAYTONA_API_URL"),
                snapshot=opt("DAYTONA_SNAPSHOT"), image_digest=opt("DAYTONA_IMAGE_DIGEST"),
                enable_live_runner=flags["PROOFHUNTER_ENABLE_LIVE_RUNNER"]),
            github=GithubSettings(
                app_id=int(app_id) if app_id else None,
                private_key_path=private_key_path, private_key=private_key,
                api_version=env.get("GITHUB_API_VERSION") or GITHUB_DEFAULT_API_VERSION,
                webhook_secret=secret("GITHUB_WEBHOOK_SECRET"),
                enable_webhook=flags["PROOFHUNTER_ENABLE_GITHUB_WEBHOOK"],
                enable_checks=flags["PROOFHUNTER_ENABLE_GITHUB_CHECKS"]),
            agent=AgentSettings(seed_phrase=secret("AGENT_SEED_PHRASE"),
                                port=int(env.get("AGENT_PORT") or 8001)),
        )
    except (ValidationError, ValueError) as error:
        raise ConfigError(f"invalid configuration: {error.__class__.__name__}") from error
    if live:
        _require_live(settings, role)
    return settings


def _require_live(settings: Settings, role: ServiceRole) -> None:
    required = (
        ("PROOFHUNTER_API_TOKEN", settings.api_token),
        ("PROOFHUNTER_OPERATOR_TOKEN", settings.operator_token),
        ("MODEL_API_KEY", settings.meta.api_key),
        ("SUBQ_API_KEY", settings.snap.api_key),
        ("GITHUB_APP_ID", settings.github.app_id),
        ("GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH",
         settings.github.private_key or settings.github.private_key_path),
    )
    names = {
        ServiceRole.ALL: {name for name, _ in required},
        ServiceRole.API: {"PROOFHUNTER_API_TOKEN", "PROOFHUNTER_OPERATOR_TOKEN"},
        ServiceRole.WORKER: {"MODEL_API_KEY", "SUBQ_API_KEY", "GITHUB_APP_ID",
                             "GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH"},
        ServiceRole.AGENT: {"PROOFHUNTER_API_TOKEN", "AGENT_SEED_PHRASE"},
    }[role]
    missing = [name for name, value in (
        *required, ("AGENT_SEED_PHRASE", settings.agent.seed_phrase)
    ) if name in names and value is None]
    if settings.daytona.enable_live_runner:
        missing += [name for name, value in (
            ("DAYTONA_API_KEY", settings.daytona.api_key),
            ("DAYTONA_SNAPSHOT", settings.daytona.snapshot),
            ("DAYTONA_IMAGE_DIGEST", settings.daytona.image_digest)) if value is None]
    if settings.github.enable_webhook and settings.github.webhook_secret is None:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if missing:
        raise ConfigError(f"live profile is missing required variables: {missing}")
    tokens = [t.get_secret_value() for t in (settings.api_token, settings.operator_token) if t]
    if len(set(tokens)) != len(tokens):
        raise ConfigError("control API and operator credentials must differ")


def _validated_pem(value: str) -> SecretStr:
    value = value.replace("\\n", "\n").strip()
    if re.fullmatch(
        r"-----BEGIN ((?:[A-Z]+ )?PRIVATE KEY)-----\n[A-Za-z0-9+/=\s]+\n-----END \1-----",
        value,
    ) is None:
        raise ConfigError("invalid GitHub private key PEM")
    return SecretStr(value)


def github_private_key_pem(settings: Settings) -> SecretStr:
    """Return validated PEM without exposing key material or filesystem paths."""
    key = settings.github
    if key.private_key is not None and key.private_key_path is not None:
        raise ConfigError("GITHUB_PRIVATE_KEY and GITHUB_PRIVATE_KEY_PATH are mutually exclusive")
    if key.private_key is not None:
        return _validated_pem(key.private_key.get_secret_value())
    if key.private_key_path is not None:
        try:
            value = Path(key.private_key_path.get_secret_value()).read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            raise ConfigError("GitHub private key unavailable") from None
        return _validated_pem(value)
    raise ConfigError("GitHub private key unavailable")


def load_enrollments(path: Path) -> tuple[Enrollment, ...]:
    """Load the trusted enrollment registry. Raises ``ConfigError`` when invalid."""
    try:
        registry = EnrollmentRegistry.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, json.JSONDecodeError) as error:
        raise ConfigError(f"invalid enrollment registry {path.name}") from error
    return tuple(e for e in registry.enrollments if e.enabled)


def public_mode_config(settings: Settings, enrollments: Sequence[Enrollment]) -> dict[str, object]:
    """Explicit public-safe allowlist; no secret, secret digest, path or operator ID.

    API and worker must pass the same loaded trusted enrollment registry. Hash
    no arbitrary settings dump: redacted SecretStr values are not configuration.
    """
    return {
        "fingerprint_version": "proofhunter-mode-config-v1",
        "schema_version": SCHEMA_VERSION,
        "profile": settings.profile.value,
        "policy_versions": PolicyVersions().model_dump(mode="json"),
        "budget": settings.budget.model_dump(mode="json"),
        "quota": settings.quota.model_dump(mode="json"),
        "enable_intake": settings.enable_intake,
        "meta": {"model": settings.meta.model, "url": settings.meta.url},
        "snap": {"model": settings.snap.model, "url": settings.snap.url},
        "runner": {"enabled": settings.daytona.enable_live_runner,
                   "api_url": settings.daytona.api_url,
                   "snapshot": settings.daytona.snapshot,
                   "image_digest": settings.daytona.image_digest},
        "github": {"api_version": settings.github.api_version,
                   "checks": settings.github.enable_checks,
                   "webhook": settings.github.enable_webhook},
        "heartbeat": {"ttl_seconds": HEARTBEAT_TTL_SECONDS,
                      "max_ttl_seconds": MAX_HEARTBEAT_TTL_SECONDS,
                      "interval_seconds": HEARTBEAT_INTERVAL_SECONDS},
        "enrollments": sorted(enrollment_fingerprint(e) for e in enrollments if e.enabled),
    }


def mode_config_fingerprint(settings: Settings, enrollments: Sequence[Enrollment]) -> str:
    """Deterministic canonical SHA-256 of public_mode_config, with no I/O."""
    return canonical_sha256(public_mode_config(settings, enrollments))


__all__ = [name for name in dir() if not name.startswith("_") and name != "annotations"]
