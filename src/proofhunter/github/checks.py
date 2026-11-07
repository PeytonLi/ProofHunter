"""Advisory publication; the durable outbox must retain held ambiguous creates."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from proofhunter.contracts import (
    CheckIntent,
    PrTarget,
    PublishResult,
    PublishStatus,
    ReasonCode,
)
from proofhunter.github.client import GithubSession, obj, positive, utcnow
from proofhunter.github.source import parse_pull
from proofhunter.ports import AdapterError


class GithubChecks:
    def __init__(
        self,
        session: GithubSession | None,
        *,
        app_id: int | None = None,
        enabled: bool = False,
        offline: bool = True,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.app_id = app_id
        self.enabled = enabled
        self.offline = offline
        self.now = now
        self._held: set[str] = set()
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self.session is not None:
            await self.session.aclose()

    async def publish(self, intent: CheckIntent) -> PublishResult:
        def result(
            status: PublishStatus,
            check_id: int | None = None,
            *,
            attempts: int = 0,
            code: ReasonCode | None = None,
        ) -> PublishResult:
            return PublishResult(
                run_id=intent.run_id,
                status=status,
                check_run_id=check_id,
                attempts=attempts,
                reason_code=code,
                published_at=self.now() if status is PublishStatus.PUBLISHED else None,
            )

        if self.offline or intent.execution_mode != "live":
            return result(PublishStatus.SKIPPED_OFFLINE)
        if not self.enabled:
            return result(PublishStatus.SKIPPED_DISABLED)
        if self.session is None or self.app_id is None:
            return result(PublishStatus.FAILED_PERMANENT, code=ReasonCode.PROVIDER_UNAVAILABLE)
        target = PrTarget(
            owner=intent.owner,
            repo=intent.repo,
            number=1,
            repo_id=intent.repo_id,
            installation_id=intent.installation_id,
        )
        prefix = f"/repos/{target.full_name}"
        attempts = 0
        known_check_id = None
        async with self._lock:
            try:
                # CheckIntent has no PR number: require one unambiguous associated open PR.
                pulls = await self.session.request(
                    target,
                    "GET",
                    f"{prefix}/commits/{intent.head_sha}/pulls",
                    params={"per_page": 100},
                )
                if not isinstance(pulls, list) or len(pulls) >= 100:
                    raise ValueError
                candidates = [obj(p) for p in pulls if obj(p).get("state") == "open"]
                if len(candidates) != 1:
                    return result(PublishStatus.STALE_HEAD, code=ReasonCode.STALE_EVENT)
                target = target.model_copy(update={"number": positive(candidates[0]["number"])})
                # Do not trust the possibly stale commit-to-PR association response.
                current = await self.session.request(
                    target, "GET", f"{prefix}/pulls/{target.number}"
                )
                _, current_head = parse_pull(current, target)
                if obj(current).get("state") != "open" or current_head != intent.head_sha:
                    return result(PublishStatus.STALE_HEAD, code=ReasonCode.STALE_EVENT)
                matches = await self._matches(target, intent)
                if len(matches) > 1:
                    self._held.add(intent.run_id)
                    return result(
                        PublishStatus.HELD_AMBIGUOUS, code=ReasonCode.PROVIDER_INVALID_RESPONSE
                    )
                if matches:
                    self._held.add(intent.run_id)
                    check = matches[0]
                    check_id = positive(check["id"])
                    known_check_id = check_id
                    if (
                        check.get("status") != "completed"
                        or check.get("conclusion") != intent.conclusion
                    ):
                        attempts += 1
                        updated = await self.session.request(
                            target,
                            "PATCH",
                            f"{prefix}/check-runs/{check_id}",
                            body=self._payload(intent, update=True),
                        )
                        self._validate(updated, intent, completed=True)
                    return result(PublishStatus.PUBLISHED, check_id, attempts=attempts)
                if intent.run_id in self._held:
                    return result(
                        PublishStatus.HELD_AMBIGUOUS, code=ReasonCode.PROVIDER_UNAVAILABLE
                    )
            except AdapterError as error:
                return result(
                    PublishStatus.FAILED_RETRYABLE
                    if error.retryable
                    else PublishStatus.FAILED_PERMANENT,
                    known_check_id,
                    attempts=attempts,
                    code=error.code,
                )
            except (KeyError, ValueError, TypeError):
                return result(
                    PublishStatus.HELD_AMBIGUOUS,
                    known_check_id,
                    attempts=attempts,
                    code=ReasonCode.PROVIDER_INVALID_RESPONSE,
                )
            # Tombstone before sending. A lost acknowledgement must never cause another POST.
            self._held.add(intent.run_id)
            try:
                created = await self.session.request(
                    target, "POST", f"{prefix}/check-runs", body=self._payload(intent)
                )
                check_id = self._validate(created, intent, completed=True)
                return result(PublishStatus.PUBLISHED, check_id, attempts=1)
            except (AdapterError, KeyError, ValueError, TypeError):
                try:
                    matches = await self._matches(target, intent)
                    if (
                        len(matches) == 1
                        and matches[0].get("status") == "completed"
                        and matches[0].get("conclusion") == intent.conclusion
                    ):
                        return result(
                            PublishStatus.PUBLISHED, positive(matches[0]["id"]), attempts=1
                        )
                except (AdapterError, KeyError, ValueError, TypeError):
                    pass
                return result(
                    PublishStatus.HELD_AMBIGUOUS, attempts=1, code=ReasonCode.PROVIDER_UNAVAILABLE
                )

    def _validate(self, value: Any, intent: CheckIntent, *, completed: bool = False) -> int:
        check = obj(value)
        if (
            check["external_id"] != intent.run_id
            or check["head_sha"] != intent.head_sha
            or check["name"] != "ProofHunter"
            or positive(obj(check["app"])["id"]) != self.app_id
        ):
            raise ValueError
        if completed and (
            check.get("status") != "completed" or check.get("conclusion") != intent.conclusion
        ):
            raise ValueError
        return positive(check["id"])

    async def _matches(self, target: PrTarget, intent: CheckIntent) -> list[dict[str, Any]]:
        assert self.session is not None
        data = obj(
            await self.session.request(
                target,
                "GET",
                f"/repos/{target.full_name}/commits/{intent.head_sha}/check-runs",
                params={
                    "check_name": "ProofHunter",
                    "filter": "all",
                    "per_page": 100,
                    "app_id": self.app_id or 0,
                },
            )
        )
        count = data["total_count"]
        checks = data["check_runs"]
        if (
            type(count) is not int
            or not isinstance(checks, list)
            or not 0 <= count <= 100
            or count != len(checks)
        ):
            raise ValueError
        matches = []
        for raw in checks:
            check = obj(raw)
            if check.get("external_id") == intent.run_id:
                self._validate(check, intent)
                matches.append(check)
        return matches

    @staticmethod
    def _payload(intent: CheckIntent, *, update: bool = False) -> dict[str, Any]:
        # Only fixed wording and public identifiers; never raw model/source/provider text.
        data: dict[str, Any] = {
            "status": "completed",
            "conclusion": intent.conclusion.value,
            "output": {
                "title": f"ProofHunter advisory: {intent.conclusion.value}",
                "summary": f"Run {intent.run_id}. Receipt SHA-256: {intent.receipt_sha256}. "
                "Applies only to the pinned head and reported test budget.",
            },
        }
        if not update:
            data.update(name="ProofHunter", head_sha=intent.head_sha, external_id=intent.run_id)
        return data
