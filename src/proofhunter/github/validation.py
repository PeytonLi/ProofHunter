"""Canonical PR parsing and trusted enrollment binding."""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from pydantic import ValidationError

from proofhunter.contracts import Enrollment, EnrollmentRegistry, PrRef, PrTarget, ReasonCode
from proofhunter.ports import AdapterError, ConfigError


def failure(code: ReasonCode, message: str, *, retryable: bool = False) -> AdapterError:
    return AdapterError("github", code, message, retryable=retryable)


def parse_pr_url(url: str) -> PrRef:
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 512 or not url.isascii():
            raise ValueError
        if any(ord(c) <= 32 or ord(c) == 127 for c in url) or any(c in url for c in "%?#\\"):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "github.com":
            raise ValueError
        match = re.fullmatch(
            r"/([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})/pull/([1-9][0-9]{0,9})", parts.path
        )
        if match is None or any(v in {".", ".."} for v in match.group(1, 2)):
            raise ValueError
        return PrRef(owner=match[1].lower(), repo=match[2].lower(), number=int(match[3]))
    except (ValueError, ValidationError):
        raise failure(
            ReasonCode.INVALID_PR_URL, "expected one canonical HTTPS GitHub PR URL"
        ) from None


class GithubRepositoryValidator:
    def __init__(self, enrollments: Sequence[Enrollment]) -> None:
        try:
            registry = EnrollmentRegistry(schema_version="1.0", enrollments=list(enrollments))
            for item in registry.enrollments:
                parse_pr_url(f"https://github.com/{item.repo_full_name}/pull/1")
        except (ValidationError, AdapterError):
            raise ConfigError("invalid GitHub enrollment registry") from None
        self.enrollments = {e.repo_full_name.lower(): e for e in registry.enrollments if e.enabled}

    def resolve(self, pr_url: str) -> tuple[PrTarget, Enrollment]:
        ref = parse_pr_url(pr_url)
        enrollment = self.enrollments.get(ref.full_name)
        if enrollment is None:
            raise failure(ReasonCode.REPO_NOT_ENROLLED, "repository is not enrolled")
        return PrTarget(
            **ref.model_dump(),
            repo_id=enrollment.repo_id,
            installation_id=enrollment.installation_id,
        ), enrollment

    def bind(self, target: PrTarget) -> Enrollment:
        resolved, enrollment = self.resolve(target.canonical_url)
        if resolved != target:
            raise failure(
                ReasonCode.REPO_NOT_ENROLLED, "repository identity does not match enrollment"
            )
        return enrollment
