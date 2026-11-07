from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from proofhunter.acp import ControlApiError, HttpControlApiClient, create_control_client
from proofhunter.acp.client import MAX_RESPONSE_BYTES, validate_api_base
from proofhunter.config import load_settings
from proofhunter.contracts import ReasonCode, RunRequest, RunState
from proofhunter.ports import ConfigError, ControlApiClient

from .conftest import BASE, PR_URL, FakeControlHttp, body, fixture_receipt

TOKEN = "fixture-service-token-not-a-secret"
RUN = "00000000-05e7-4001-8000-000000000001"


def client(fake: FakeControlHttp, token: str | None = TOKEN) -> HttpControlApiClient:
    return HttpControlApiClient(BASE, token, transport=fake.transport())


def test_protocol_conformance(fake_http: FakeControlHttp) -> None:
    assert isinstance(client(fake_http), ControlApiClient)


async def test_create_status_receipt_flow(fake_http: FakeControlHttp) -> None:
    c = client(fake_http)
    status = await c.create_run(RunRequest(pr_url=PR_URL), "acp-key-1")
    assert status.status is RunState.QUEUED
    request = fake_http.requests[-1]
    assert request.method == "POST"
    assert request.url.path == "/v1/runs"
    assert request.headers["idempotency-key"] == "acp-key-1"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert body(request) == {"pr_url": PR_URL, "seed": None}
    again = await c.create_run(RunRequest(pr_url=PR_URL), "acp-key-1")
    assert again.run_id == status.run_id
    polled = await c.get_status(status.run_id)
    assert polled.run_id == status.run_id
    with pytest.raises(ControlApiError) as info:
        await c.get_receipt(status.run_id)
    assert info.value.code is ReasonCode.RECEIPT_PENDING
    assert info.value.http_status == 409


async def test_sealed_receipt(fake_http: FakeControlHttp) -> None:
    receipt = fixture_receipt()
    fake_http.override = lambda r: httpx.Response(200, content=receipt.model_dump_json())
    got = await client(fake_http).get_receipt(receipt.run_id)
    assert got.receipt_sha256 == receipt.receipt_sha256
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_receipt("00000000-05e7-4001-8000-000000000009")
    assert info.value.code is ReasonCode.PROVIDER_INVALID_RESPONSE


async def test_no_token_sends_no_auth(fake_http: FakeControlHttp) -> None:
    await client(fake_http, None).create_run(RunRequest(pr_url=PR_URL), "k")
    assert "authorization" not in fake_http.requests[-1].headers


async def test_not_found_and_conflict(fake_http: FakeControlHttp) -> None:
    c = client(fake_http)
    with pytest.raises(ControlApiError) as info:
        await c.get_status(RUN)
    assert info.value.code is ReasonCode.NOT_FOUND
    assert not info.value.retryable
    await c.create_run(RunRequest(pr_url=PR_URL), "same")
    with pytest.raises(ControlApiError) as info:
        await c.create_run(RunRequest(pr_url=PR_URL.replace("/1", "/2")), "same")
    assert info.value.code is ReasonCode.CONFLICTING_IDEMPOTENCY_KEY


@pytest.mark.parametrize("key", ["", "x" * 129, "has space", "naïve"])
async def test_bad_idempotency_key_never_sent(fake_http: FakeControlHttp, key: str) -> None:
    with pytest.raises(ControlApiError):
        await client(fake_http).create_run(RunRequest(pr_url=PR_URL), key)
    assert fake_http.requests == []


@pytest.mark.parametrize("run_id", ["../admin", RUN.upper(), "1", RUN + "/receipt"])
async def test_bad_run_id_never_interpolated(fake_http: FakeControlHttp, run_id: str) -> None:
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(run_id)
    assert info.value.code is ReasonCode.INVALID_REQUEST
    assert fake_http.requests == []


async def test_timeout_is_retried_once_then_sanitized(fake_http: FakeControlHttp) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret upstream detail", request=request)

    fake_http.override = boom
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert info.value.code is ReasonCode.PROVIDER_TIMEOUT
    assert info.value.retryable
    assert "secret" not in str(info.value)
    assert len(fake_http.requests) == 2


async def test_connect_error(fake_http: FakeControlHttp) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake_http.override = boom
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert info.value.code is ReasonCode.PROVIDER_UNAVAILABLE


async def test_503_then_success(fake_http: FakeControlHttp) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        return httpx.Response(503, text="<html>upstream token=abc</html>") if calls["n"] == 1 \
            else None

    fake_http.override = flaky
    status = await client(fake_http).create_run(RunRequest(pr_url=PR_URL), "retry-key")
    assert status.status is RunState.QUEUED
    keys = {r.headers["idempotency-key"] for r in fake_http.requests}
    assert keys == {"retry-key"}
    assert len(fake_http.requests) == 2


async def test_rate_limited_retry_after(fake_http: FakeControlHttp) -> None:
    fake_http.override = lambda r: FakeControlHttp.error(
        429, "rate_limited", True, headers={"Retry-After": "30"})
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert info.value.code is ReasonCode.RATE_LIMITED
    assert info.value.retry_after_seconds == 30
    assert len(fake_http.requests) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"run_id": RUN}),
    httpx.Response(200, json={"error": "x"}),
    httpx.Response(200, json=[]),
])
async def test_malformed_success_body(fake_http: FakeControlHttp,
                                      response: httpx.Response) -> None:
    fake_http.override = lambda r: response
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert info.value.code is ReasonCode.PROVIDER_INVALID_RESPONSE


async def test_unknown_error_body_is_not_echoed(fake_http: FakeControlHttp) -> None:
    fake_http.override = lambda r: httpx.Response(418, text="Traceback ... sk-live-123")
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert "sk-live" not in str(info.value)
    assert "Traceback" not in str(info.value)


async def test_oversized_response(fake_http: FakeControlHttp) -> None:
    fake_http.override = lambda r: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ControlApiError) as info:
        await client(fake_http).get_status(RUN)
    assert info.value.code is ReasonCode.PAYLOAD_TOO_LARGE


async def test_redirects_not_followed(fake_http: FakeControlHttp) -> None:
    fake_http.override = lambda r: httpx.Response(
        302, headers={"Location": "https://evil.example/v1/runs"})
    with pytest.raises(ControlApiError):
        await client(fake_http).get_status(RUN)
    assert all(r.url.host == "control.internal" for r in fake_http.requests)


def test_repr_hides_token(fake_http: FakeControlHttp) -> None:
    assert TOKEN not in repr(client(fake_http))


@pytest.mark.parametrize("base", ["ftp://x", "https://u:p@x", "https://x/?q=1", "nohost"])
def test_invalid_base(base: str) -> None:
    with pytest.raises(ConfigError):
        validate_api_base(base, live=False)


def test_live_rules() -> None:
    with pytest.raises(ConfigError):
        validate_api_base("http://control.example.com", live=True)
    assert validate_api_base("http://10.0.0.5:8080/", live=True) == "http://10.0.0.5:8080"
    with pytest.raises(ConfigError):
        HttpControlApiClient("https://control.example.com", None, live=True)


def test_create_control_client_from_offline_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFHUNTER_PROFILE", "offline")
    settings = load_settings()
    settings = settings.model_copy(update={"api_base": BASE, "api_token": SecretStr(TOKEN)})
    made = create_control_client(settings, transport=FakeControlHttp().transport())
    assert isinstance(made, HttpControlApiClient)
