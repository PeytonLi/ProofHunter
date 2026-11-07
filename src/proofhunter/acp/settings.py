"""Credential-isolated configuration for the standalone mailbox process."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from proofhunter.acp.client import validate_api_base
from proofhunter.config import ServiceRole, Settings, load_settings
from proofhunter.ports import ConfigError

_VARIABLES = (
    "PROOFHUNTER_PROFILE", "PROOFHUNTER_API_BASE", "PROOFHUNTER_API_TOKEN",
    "AGENT_SEED_PHRASE", "AGENT_PORT",
)


def load_acp_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    scoped = {key: env[key] for key in _VARIABLES if key in env}
    if (scoped.get("PROOFHUNTER_PROFILE", "").strip() == "live"
            and not scoped.get("PROOFHUNTER_API_BASE", "").strip()):
        raise ConfigError("PROOFHUNTER_API_BASE is required")
    settings = load_settings(scoped, role=ServiceRole.AGENT)
    validate_api_base(settings.api_base, live=not settings.offline)
    return settings


def prepare_storage(environ: Mapping[str, str] | None = None) -> None:
    """uAgents 0.26.1 uses cwd for its KeyValueStore; set it before construction."""
    env = os.environ if environ is None else environ
    value = env.get("PROOFHUNTER_AGENT_STORAGE_DIR")
    if value is None:
        return
    if not value.strip():
        raise ConfigError("PROOFHUNTER_AGENT_STORAGE_DIR must not be empty")
    try:
        path = Path(value).expanduser().resolve()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chdir(path)
    except OSError:
        raise ConfigError("PROOFHUNTER_AGENT_STORAGE_DIR is unavailable") from None


def require_publication_approval() -> None:
    if os.environ.get("PROOFHUNTER_ACP_PUBLISH", "").strip().lower() != "true":
        raise ConfigError(
            "ACP publication is disabled; PROOFHUNTER_ACP_PUBLISH=true requires "
            "operator approval after the isolated three-run gate")
