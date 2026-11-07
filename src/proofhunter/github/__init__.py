"""GitHub adapters. Offline wiring injects fixtures; live factories never fall back."""

from collections.abc import Callable, Sequence

import httpx

from proofhunter.config import Settings, github_private_key_pem
from proofhunter.contracts import Enrollment
from proofhunter.github.checks import GithubChecks
from proofhunter.github.client import CallRecord, GithubHTTP, GithubSession, InstallationTokens
from proofhunter.github.source import GithubSource
from proofhunter.github.validation import GithubRepositoryValidator, parse_pr_url
from proofhunter.github.webhooks import GithubWebhookVerifier
from proofhunter.ports import ConfigError

Observer = Callable[[CallRecord], None]


def _session(
    settings: Settings, *, checks: bool = False, observer: Observer | None = None
) -> GithubSession:
    if settings.offline:
        raise ConfigError("offline integration must inject FixtureSource, not a GitHub HTTP client")
    config = settings.github
    if config.app_id is None:
        raise ConfigError("GitHub App configuration is missing")
    # In-memory GITHUB_PRIVATE_KEY or the path fallback; errors never echo key or path.
    private_key = github_private_key_pem(settings)
    http = GithubHTTP(
        httpx.AsyncClient(trust_env=False), config.api_version, observer=observer
    )
    return GithubSession(http, InstallationTokens(http, config.app_id, private_key, checks=checks))


def create_source_port(
    settings: Settings,
    enrollments: Sequence[Enrollment],
    *,
    observer: Observer | None = None,
) -> GithubSource:
    validator = GithubRepositoryValidator(enrollments)
    return GithubSource(_session(settings, observer=observer), validator)


def create_check_port(settings: Settings) -> GithubChecks:
    enabled = settings.github.enable_checks and not settings.offline
    return GithubChecks(
        _session(settings, checks=True) if enabled else None,
        app_id=settings.github.app_id,
        enabled=enabled,
        offline=settings.offline,
    )


def create_repository_validator(
    settings: Settings, enrollments: Sequence[Enrollment]
) -> GithubRepositoryValidator:
    return GithubRepositoryValidator(enrollments)


def create_webhook_verifier(settings: Settings) -> GithubWebhookVerifier:
    config = settings.github
    secret = config.webhook_secret
    return GithubWebhookVerifier(
        secret.get_secret_value().encode() if secret else None,
        enabled=config.enable_webhook and not settings.offline,
    )


__all__ = [
    "CallRecord",
    "GithubChecks",
    "GithubRepositoryValidator",
    "GithubSource",
    "GithubWebhookVerifier",
    "create_check_port",
    "create_repository_validator",
    "create_source_port",
    "create_webhook_verifier",
    "parse_pr_url",
]
