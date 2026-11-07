"""Transport-agnostic chat turn handling: dedup, limits, timeouts, redacted replies.

``ChatService.reply_for(sender, msg_id, text)`` produces the plain-text reply for one
inbound ACP message. The uAgents wiring in ``proofhunter.acp.agent`` acks first,
then sends this reply with end-session content.

Idempotency: each inbound message ID maps to one control API ``Idempotency-Key``
(scoped by a hash of the transport sender), and its final reply is persisted in a
bounded ledger so a redelivered message returns the same text and run ID without a
second API call. Transient failures are not persisted, so a redelivery retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from proofhunter.acp.commands import (
    EvidenceCommand,
    HelpCommand,
    InvalidCommand,
    StatusCommand,
    VerifyCommand,
    parse_command,
)
from proofhunter.acp.render import (
    cap_reply,
    render_error,
    render_evidence,
    render_help,
    render_invalid,
    render_queued,
    render_status,
)
from proofhunter.contracts import ReasonCode, RunRequest
from proofhunter.ports import AdapterError, ControlApiClient

DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 20.0
LEDGER_MAX_ENTRIES: Final = 1024
LEDGER_TTL_SECONDS: Final = 7 * 24 * 3600
_LEDGER_INDEX: Final = "proofhunter-acp:ledger-index"
_LEDGER_PREFIX: Final = "proofhunter-acp:reply:"


class KeyValue(Protocol):
    """Subset of ``uagents.storage.KeyValueStore`` (``ctx.storage``) used for dedup."""

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any) -> None: ...

    def remove(self, key: str) -> None: ...


class MemoryKeyValue:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def remove(self, key: str) -> None:
        self.data.pop(key, None)


def sender_scope(sender: str) -> str:
    return hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]


def idempotency_key(sender: str, msg_id: str) -> str:
    """``acp-<sender hash>-<msg_id>``: ASCII, at most 57 characters."""
    return f"acp-{sender_scope(sender)}-{msg_id}"


class ReplyLedger:
    """Persisted, bounded map of inbound message -> final reply text."""

    def __init__(self, store: KeyValue, *, max_entries: int = LEDGER_MAX_ENTRIES,
                 ttl_seconds: int = LEDGER_TTL_SECONDS,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._max = max_entries
        self._ttl = ttl_seconds
        self._now = wall_clock

    @staticmethod
    def _key(sender: str, msg_id: str) -> str:
        digest = hashlib.sha256(f"{sender}\n{msg_id}".encode()).hexdigest()
        return _LEDGER_PREFIX + digest

    def get(self, sender: str, msg_id: str) -> str | None:
        entry = self._store.get(self._key(sender, msg_id))
        if not isinstance(entry, dict):
            return None
        text, at = entry.get("text"), entry.get("at")
        if not isinstance(text, str) or not isinstance(at, int | float):
            return None
        if self._now() - at > self._ttl:
            return None
        return text

    def put(self, sender: str, msg_id: str, text: str) -> None:
        key = self._key(sender, msg_id)
        raw_index = self._store.get(_LEDGER_INDEX)
        index = [k for k in raw_index if isinstance(k, str) and k != key] if isinstance(
            raw_index, list) else []
        index.append(key)
        while len(index) > self._max:
            self._store.remove(index.pop(0))
        self._store.set(key, {"text": text, "at": self._now()})
        self._store.set(_LEDGER_INDEX, index)


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window`` seconds per bucket."""

    def __init__(self, limit: int, window: float,
                 clock: Callable[[], float] = time.monotonic, max_buckets: int = 4096) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max_buckets = max_buckets
        self._events: dict[str, deque[float]] = {}

    def allow(self, bucket: str) -> int | None:
        """Record an event and return ``None``, or return seconds to wait when limited."""
        now = self._clock()
        events = self._events.get(bucket)
        if events is None:
            if len(self._events) >= self._max_buckets:
                self._evict(now)
            events = self._events.setdefault(bucket, deque())
        while events and now - events[0] >= self._window:
            events.popleft()
        if len(events) >= self._limit:
            return max(1, int(self._window - (now - events[0])) + 1)
        events.append(now)
        return None

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._events.items() if not v or now - v[-1] >= self._window]:
            del self._events[key]
        while len(self._events) >= self._max_buckets:
            del self._events[next(iter(self._events))]


@dataclass(frozen=True, slots=True)
class Limits:
    """Local chat limits. The control API still enforces quotas and the hard cap."""

    per_sender_commands: int = 20
    per_sender_window_seconds: float = 60.0
    per_sender_verifies: int = 3
    per_sender_verify_window_seconds: float = 600.0
    global_commands: int = 240
    global_window_seconds: float = 60.0
    max_in_flight: int = 16


@dataclass(frozen=True, slots=True)
class _Outcome:
    text: str
    final: bool


class ChatService:
    def __init__(self, client: ControlApiClient, ledger: ReplyLedger, *,
                 limits: Limits | None = None,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        limits = limits or Limits()
        self._client = client
        self._ledger = ledger
        self._timeout = request_timeout
        self._sender_limit = SlidingWindowLimiter(
            limits.per_sender_commands, limits.per_sender_window_seconds, clock)
        self._verify_limit = SlidingWindowLimiter(
            limits.per_sender_verifies, limits.per_sender_verify_window_seconds, clock)
        self._global_limit = SlidingWindowLimiter(
            limits.global_commands, limits.global_window_seconds, clock)
        self._in_flight = asyncio.Semaphore(limits.max_in_flight)
        self._locks: dict[str, asyncio.Lock] = {}

    async def reply_for(self, sender: str, msg_id: str, text: str) -> str:
        """Reply text for one inbound message. Never raises for user or API problems."""
        key = idempotency_key(sender, msg_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                cached = self._ledger.get(sender, msg_id)
                if cached is not None:
                    return cached
                outcome = await self._fresh(sender, key, text)
                reply = cap_reply(outcome.text)
                if outcome.final:
                    self._ledger.put(sender, msg_id, reply)
                return reply
        finally:
            if not lock.locked():
                self._locks.pop(key, None)

    async def _fresh(self, sender: str, key: str, text: str) -> _Outcome:
        command = parse_command(text)
        if isinstance(command, HelpCommand):
            return _Outcome(render_help(), final=True)
        if isinstance(command, InvalidCommand):
            return _Outcome(render_invalid(command.reason), final=True)
        scope = sender_scope(sender)
        for limiter, bucket in ((self._global_limit, "global"), (self._sender_limit, scope)):
            wait = limiter.allow(bucket)
            if wait is not None:
                return _Outcome(render_error(ReasonCode.RATE_LIMITED,
                                             retry_after_seconds=wait), final=False)
        if isinstance(command, VerifyCommand):
            wait = self._verify_limit.allow(scope)
            if wait is not None:
                return _Outcome(render_error(ReasonCode.RATE_LIMITED,
                                             retry_after_seconds=wait), final=False)
        if self._in_flight.locked():
            return _Outcome(render_error(ReasonCode.RATE_LIMITED, retryable=True), final=False)
        async with self._in_flight:
            try:
                return await asyncio.wait_for(self._call(command, key), self._timeout)
            except TimeoutError:
                return _Outcome(render_error(ReasonCode.PROVIDER_TIMEOUT, retryable=True),
                                final=False)
            except AdapterError as error:
                retry_after = getattr(error, "retry_after_seconds", None)
                return _Outcome(render_error(error.code, retryable=error.retryable,
                                             retry_after_seconds=retry_after),
                                final=not error.retryable
                                and error.code is not ReasonCode.RATE_LIMITED)
            except Exception:  # unexpected bug: answer without leaking details
                return _Outcome(render_error(ReasonCode.INTERNAL_ERROR, retryable=True),
                                final=False)

    async def _call(self, command: VerifyCommand | StatusCommand | EvidenceCommand,
                    key: str) -> _Outcome:
        if isinstance(command, VerifyCommand):
            status = await self._client.create_run(RunRequest(pr_url=command.pr_url), key)
            return _Outcome(render_queued(status), final=True)
        if isinstance(command, StatusCommand):
            return _Outcome(render_status(await self._client.get_status(command.run_id)),
                            final=True)
        try:
            receipt = await self._client.get_receipt(command.run_id)
        except AdapterError as error:
            if error.code is not ReasonCode.RECEIPT_PENDING:
                raise
            status = await self._client.get_status(command.run_id)
            return _Outcome(f"The run is still active, so there is no receipt yet.\n"
                            f"{render_status(status)}", final=False)
        return _Outcome(render_evidence(receipt), final=True)


__all__ = [
    "ChatService",
    "KeyValue",
    "Limits",
    "MemoryKeyValue",
    "ReplyLedger",
    "SlidingWindowLimiter",
    "idempotency_key",
    "sender_scope",
]
