"""HMAC authentication before parsing, with no queue or delivery-store side effects."""

import hashlib
import hmac
import json
import re
from collections import OrderedDict

from pydantic import ValidationError

from proofhunter.contracts import GithubWebhookEvent, PrTarget, ReasonCode, WebhookAction
from proofhunter.github.client import obj, positive, unique_object
from proofhunter.github.source import parse_pull, verify_repo
from proofhunter.github.validation import failure, parse_pr_url


class GithubWebhookVerifier:
    def __init__(self, secret: bytes | None, *, enabled: bool = False) -> None:
        self._secret = secret
        self.enabled = enabled
        self._authenticated: OrderedDict[str, None] = OrderedDict()

    def verify(self, raw_body: bytes, signature_header: str | None) -> bool:
        if len(raw_body) > 256 * 1024:
            return False
        self._authenticated.pop(hashlib.sha256(raw_body).hexdigest(), None)
        if (
            not self.enabled
            or not self._secret
            or not isinstance(signature_header, str)
            or re.fullmatch(r"sha256=[0-9a-f]{64}", signature_header) is None
        ):
            return False
        expected = "sha256=" + hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, signature_header)
        if valid:
            self._authenticated[hashlib.sha256(raw_body).hexdigest()] = None
            if len(self._authenticated) > 128:
                self._authenticated.popitem(last=False)
        return valid

    def parse(self, event: str, delivery_id: str, raw_body: bytes) -> GithubWebhookEvent | None:
        if not self.enabled:
            raise failure(ReasonCode.FEATURE_DISABLED, "GitHub webhooks are disabled")
        if len(raw_body) > 256 * 1024:
            raise failure(ReasonCode.PAYLOAD_TOO_LARGE, "webhook body too large")
        fingerprint = hashlib.sha256(raw_body).hexdigest()
        if fingerprint not in self._authenticated:
            raise failure(ReasonCode.INVALID_SIGNATURE, "webhook body was not authenticated")
        self._authenticated.pop(fingerprint)
        if (
            not isinstance(delivery_id, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,64}", delivery_id) is None
        ):
            raise failure(ReasonCode.INVALID_REQUEST, "invalid webhook delivery ID")
        if event != "pull_request":
            return None
        try:
            data = obj(json.loads(raw_body, object_pairs_hook=unique_object))
            if data.get("action") not in {a.value for a in WebhookAction}:
                return None
            repo, pull = obj(data["repository"]), obj(data["pull_request"])
            ref = parse_pr_url(
                f"https://github.com/{repo['full_name']}/pull/{positive(data['number'])}"
            )
            target = PrTarget(
                **ref.model_dump(),
                repo_id=positive(repo["id"]),
                installation_id=positive(obj(data["installation"])["id"]),
            )
            verify_repo(repo, target)
            base_sha, head_sha = parse_pull(pull, target)
            return GithubWebhookEvent(
                delivery_id=delivery_id,
                event="pull_request",
                action=WebhookAction(data["action"]),
                installation_id=target.installation_id,
                repo_id=target.repo_id,
                owner=target.owner,
                repo=target.repo,
                number=target.number,
                head_sha=head_sha,
                base_sha=base_sha,
                body_sha256=fingerprint,
            )
        except (KeyError, ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
            raise failure(ReasonCode.INVALID_REQUEST, "invalid pull request webhook") from None
