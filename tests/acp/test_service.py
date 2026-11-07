from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from fixtures.adapters import FixtureControlApi
from proofhunter.acp import ChatService, HttpControlApiClient, Limits, ReplyLedger, idempotency_key
from proofhunter.acp.service import MemoryKeyValue, SlidingWindowLimiter
from proofhunter.contracts import ReasonCode, ReceiptV1, RunRequest, RunStatus
from proofhunter.ports import AdapterError

from .conftest import BASE, PR_URL, FakeControlHttp, fixture_receipt

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
SENDER = "agent1qfakesender000000000000000000000000000000000000000000000"


class CountingApi:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.creates: list[str] = []
        self.status_calls = 0
        self.receipt_calls = 0

    async def create_run(self, request: RunRequest, key: str) -> RunStatus:
        self.creates.append(key)
        return await self.inner.create_run(request, key)  # type: ignore[attr-defined,no-any-return]

    async def get_status(self, run_id: str) -> RunStatus:
        self.status_calls += 1
        return await self.inner.get_status(run_id)  # type: ignore[attr-defined,no-any-return]

    async def get_receipt(self, run_id: str) -> ReceiptV1:
        self.receipt_calls += 1
        return await self.inner.get_receipt(run_id)  # type: ignore[attr-defined,no-any-return]


def http_service(fake: FakeControlHttp, store: MemoryKeyValue | None = None,
                 **kwargs: object) -> tuple[ChatService, CountingApi]:
    api = CountingApi(HttpControlApiClient(BASE, "tok", transport=fake.transport()))
    return ChatService(api, ReplyLedger(store or MemoryKeyValue()), **kwargs), api  # type: ignore[arg-type]


def run_id_of(text: str) -> str:
    match = UUID.search(text)
    assert match is not None, text
    return match.group(0)


def test_idempotency_key_shape() -> None:
    key = idempotency_key(SENDER, "6f1c2b6e-1111-4222-8333-444455556666")
    assert re.fullmatch(r"acp-[0-9a-f]{16}-[0-9a-f-]{36}", key)
    assert key != idempotency_key(SENDER + "x", "6f1c2b6e-1111-4222-8333-444455556666")


async def test_verify_queues_and_polls(fake_http: FakeControlHttp) -> None:
    service, api = http_service(fake_http)
    reply = await service.reply_for(SENDER, "m-1", f"Verify {PR_URL}")
    run_id = run_id_of(reply)
    assert "queued" in reply
    assert f"status {run_id}" in reply
    assert "offline_fixture" in reply
    assert api.creates == [idempotency_key(SENDER, "m-1")]
    status = await service.reply_for(SENDER, "m-2", f"status {run_id}")
    assert run_id in status
    assert "queued" in status
    assert "Attempts" in status
    pending = await service.reply_for(SENDER, "m-3", f"evidence {run_id}")
    assert "no receipt yet" in pending


async def test_redelivery_returns_same_run_without_second_create(
        fake_http: FakeControlHttp) -> None:
    store = MemoryKeyValue()
    service, api = http_service(fake_http, store)
    first = await service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    second = await service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    assert first == second
    assert len(api.creates) == 1
    restarted, api2 = http_service(fake_http, store)
    assert await restarted.reply_for(SENDER, "m-1", f"verify {PR_URL}") == first
    assert api2.creates == []
    await service.reply_for(SENDER, "m-2", f"verify {PR_URL}")
    assert api.creates == [idempotency_key(SENDER, "m-1"), idempotency_key(SENDER, "m-2")]


async def test_concurrent_redelivery_is_serialized(fake_http: FakeControlHttp) -> None:
    service, api = http_service(fake_http)
    replies = await asyncio.gather(*[service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
                                     for _ in range(5)])
    assert len(set(replies)) == 1
    assert len(api.creates) == 1


async def test_ledger_lost_still_idempotent_at_api(fake_http: FakeControlHttp) -> None:
    service, _ = http_service(fake_http)
    first = await service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    other, api = http_service(fake_http)
    again = await other.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    assert run_id_of(first) == run_id_of(again)
    assert len(api.creates) == 1


async def test_transient_failure_not_cached_so_redelivery_retries(
        fake_http: FakeControlHttp) -> None:
    calls = {"n": 0}

    def outage(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectTimeout("down", request=request)
        return None

    fake_http.override = outage
    service, _ = http_service(fake_http)
    failed = await service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    assert UUID.search(failed) is None
    assert "try again" in failed.lower()
    ok = await service.reply_for(SENDER, "m-1", f"verify {PR_URL}")
    assert "queued" in ok


async def test_overall_timeout() -> None:
    class Slow(FixtureControlApi):
        async def get_status(self, run_id: str) -> RunStatus:
            await asyncio.sleep(5)
            raise AssertionError

    service = ChatService(Slow(), ReplyLedger(MemoryKeyValue()), request_timeout=0.05)
    reply = await service.reply_for(SENDER, "m-1",
                                    "status 00000000-05e7-4001-8000-000000000001")
    assert "provider_timeout" in reply or "timed out" in reply.lower()


async def test_unexpected_exception_is_sanitized() -> None:
    class Broken(FixtureControlApi):
        async def get_status(self, run_id: str) -> RunStatus:
            raise RuntimeError("token=sk-secret /tmp/sandbox-123")

    service = ChatService(Broken(), ReplyLedger(MemoryKeyValue()))
    reply = await service.reply_for(SENDER, "m-1",
                                    "status 00000000-05e7-4001-8000-000000000001")
    assert "sk-secret" not in reply
    assert "sandbox-123" not in reply
    assert "internal_error" in reply


async def test_unknown_run_and_unenrolled_repo(fake_http: FakeControlHttp) -> None:
    service, _ = http_service(fake_http)
    reply = await service.reply_for(SENDER, "m-1", "status 00000000-05e7-4001-8000-0000000000ff")
    assert "not_found" in reply
    reply = await service.reply_for(SENDER, "m-2", "verify https://github.com/someone/else/pull/3")
    assert UUID.search(reply) is None


async def test_help_and_malformed_do_not_call_api(fake_http: FakeControlHttp) -> None:
    service, api = http_service(fake_http)
    for i, text in enumerate(["help", "", "\x00\x01", "x" * 5000, "verify http://github.com/a/b/pull/1",
                              "status nope", "random words"]):
        reply = await service.reply_for(SENDER, f"m-{i}", text)
        assert reply
        assert len(reply) <= 3500
    assert api.creates == []
    assert api.status_calls == 0
    assert fake_http.requests == []
    help_text = await service.reply_for(SENDER, "h", "help")
    for word in ("verify", "status", "evidence", "confirmed_regression", "no_defect_found",
                 "inconclusive", "enrolled"):
        assert word in help_text


async def test_evidence_for_confirmed_counterexample() -> None:
    receipt = fixture_receipt("buggy")

    class Done(FixtureControlApi):
        async def get_receipt(self, run_id: str) -> ReceiptV1:
            return receipt

    service = ChatService(Done(), ReplyLedger(MemoryKeyValue()))
    reply = await service.reply_for(SENDER, "m-1", f"evidence {receipt.run_id}")
    assert "confirmed_regression" in reply
    assert "Expected: 41" in reply
    assert "Base: 41" in reply
    assert "Head: 42" in reply
    assert "Repeated head: 42" in reply
    assert receipt.receipt_sha256 is not None
    assert receipt.receipt_sha256 in reply
    assert receipt.pinned_revision is not None
    assert receipt.pinned_revision.head_sha in reply
    assert "offline_fixture" in reply
    assert "replay" in reply.lower()


async def test_evidence_for_no_defect_shows_coverage_and_limits() -> None:
    receipt = fixture_receipt("fixed")

    class Done(FixtureControlApi):
        async def get_receipt(self, run_id: str) -> ReceiptV1:
            return receipt

    reply = await ChatService(Done(), ReplyLedger(MemoryKeyValue())).reply_for(
        SENDER, "m-1", f"evidence {receipt.run_id}")
    assert "no_defect_found" in reply
    assert receipt.receipt_sha256 is not None
    assert receipt.receipt_sha256 in reply
    assert "overage" in reply
    assert "Expected:" not in reply


async def test_rate_limits_are_not_cached() -> None:
    now = [0.0]
    limits = Limits(per_sender_commands=2, per_sender_window_seconds=60)
    service = ChatService(FixtureControlApi(), ReplyLedger(MemoryKeyValue()), limits=limits,
                          clock=lambda: now[0])
    run = "00000000-05e7-4001-8000-0000000000ff"
    await service.reply_for(SENDER, "a", f"status {run}")
    await service.reply_for(SENDER, "b", f"status {run}")
    limited = await service.reply_for(SENDER, "c", f"status {run}")
    assert "rate_limited" in limited
    now[0] = 61
    assert "rate_limited" not in await service.reply_for(SENDER, "c", f"status {run}")


async def test_verify_specific_limit() -> None:
    limits = Limits(per_sender_verifies=1)
    api = CountingApi(FixtureControlApi())
    service = ChatService(api, ReplyLedger(MemoryKeyValue()), limits=limits)
    assert "queued" in await service.reply_for(SENDER, "a", f"verify {PR_URL}")
    assert "rate_limited" in await service.reply_for(SENDER, "b", f"verify {PR_URL}")
    assert len(api.creates) == 1
    await service.reply_for("other-sender", "c", f"verify {PR_URL}")
    assert len(api.creates) == 2


async def test_non_retryable_api_error_is_cached() -> None:
    class Gone(FixtureControlApi):
        calls = 0

        async def get_status(self, run_id: str) -> RunStatus:
            Gone.calls += 1
            raise AdapterError("control_api", ReasonCode.NOT_FOUND, "x", retryable=False)

    service = ChatService(Gone(), ReplyLedger(MemoryKeyValue()))
    text = "status 00000000-05e7-4001-8000-000000000001"
    assert await service.reply_for(SENDER, "m", text) == await service.reply_for(SENDER, "m", text)
    assert Gone.calls == 1


def test_ledger_is_bounded_and_expires() -> None:
    store = MemoryKeyValue()
    now = [1000.0]
    ledger = ReplyLedger(store, max_entries=3, ttl_seconds=10, wall_clock=lambda: now[0])
    for i in range(5):
        ledger.put("s", f"m{i}", f"r{i}")
    assert ledger.get("s", "m0") is None
    assert ledger.get("s", "m4") == "r4"
    assert len(store.data) == 4  # 3 entries + index
    now[0] += 11
    assert ledger.get("s", "m4") is None


def test_ledger_ignores_corrupt_entries() -> None:
    store = MemoryKeyValue()
    ledger = ReplyLedger(store)
    ledger.put("s", "m", "ok")
    for key in list(store.data):
        if key != "proofhunter-acp:ledger-index":
            store.data[key] = "garbage"
    assert ledger.get("s", "m") is None
    store.data["proofhunter-acp:ledger-index"] = {"bad": 1}
    ledger.put("s", "m2", "ok2")
    assert ledger.get("s", "m2") == "ok2"


def test_limiter_bucket_cap() -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=lambda: 0.0, max_buckets=2)
    for bucket in "abcd":
        assert limiter.allow(bucket) is None


@pytest.mark.parametrize("secret", ["sk-live-abc", "ghp_abcdef", "Bearer xyz"])
async def test_redaction_of_api_messages(secret: str) -> None:
    class Leaky(FixtureControlApi):
        async def get_status(self, run_id: str) -> RunStatus:
            raise AdapterError("control_api", ReasonCode.PROVIDER_UNAVAILABLE,
                               f"upstream said {secret}", retryable=True)

    reply = await ChatService(Leaky(), ReplyLedger(MemoryKeyValue())).reply_for(
        SENDER, "m", "status 00000000-05e7-4001-8000-000000000001")
    assert secret not in reply
    assert "provider_unavailable" in reply
