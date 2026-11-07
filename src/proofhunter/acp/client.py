"""HTTP client for the private control API (PRD section 6, HTTP interface).

The ACP process receives only ``PROOFHUNTER_API_BASE`` and ``PROOFHUNTER_API_TOKEN``.
Every response is size-capped and validated against the frozen DTOs; error bodies
are parsed as ``ErrorEnvelope`` but their free-text message is never surfaced.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from typing import Final, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from proofhunter.acp.commands import RUN_ID_PATTERN
from proofhunter.config import Settings
from proofhunter.contracts import ErrorEnvelope, ReasonCode, ReceiptV1, RunRequest, RunStatus
from proofhunter.ports import AdapterError, ConfigError

PROVIDER: Final = "control_api"
MAX_RESPONSE_BYTES: Final = 512 * 1024
DEFAULT_TIMEOUT: Final = httpx.Timeout(10.0, connect=3.0)
#: Transient failures retried once (POST is safe: the idempotency key dedupes).
MAX_ATTEMPTS: Final = 2
_RETRYABLE_STATUS: Final = frozenset({502, 503, 504})
_IDEMPOTENCY_KEY_CHARS: Final = frozenset(chr(c) for c in range(0x21, 0x7F))

_STATUS_CODES: Final[Mapping[int, ReasonCode]] = {
    400: ReasonCode.INVALID_REQUEST,
    401: ReasonCode.UNAUTHORIZED,
    403: ReasonCode.FORBIDDEN,
    404: ReasonCode.NOT_FOUND,
    409: ReasonCode.CONFLICTING_IDEMPOTENCY_KEY,
    413: ReasonCode.PAYLOAD_TOO_LARGE,
    422: ReasonCode.INVALID_REQUEST,
    429: ReasonCode.RATE_LIMITED,
}

ModelT = TypeVar("ModelT", bound=BaseModel)


class ControlApiError(AdapterError):
    """``AdapterError`` from the control API, with HTTP status and ``Retry-After``."""

    def __init__(self, code: ReasonCode, message: str, *, retryable: bool,
                 http_status: int | None = None, retry_after_seconds: int | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(PROVIDER, code, message, retryable=retryable, request_id=request_id)
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


def _private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(
            (".internal", ".local", ".localhost", ".svc", ".cluster.local"))
    return address.is_loopback or address.is_private


def validate_api_base(api_base: str, *, live: bool) -> str:
    """Fixed private origin check. Live plain HTTP is allowed only to private hosts."""
    parts = urlsplit(api_base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError("PROOFHUNTER_API_BASE must be an http(s) origin")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("PROOFHUNTER_API_BASE must not carry credentials, query or fragment")
    if live and parts.scheme == "http" and not _private_host(parts.hostname):
        raise ConfigError("live PROOFHUNTER_API_BASE must use https unless the host is private")
    return api_base.rstrip("/")


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return max(1, min(int(value), 3600))
    return None


class HttpControlApiClient:
    """``ControlApiClient`` over HTTPX with bearer service authentication."""

    def __init__(self, api_base: str, token: str | None, *, live: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        if live and not token:
            raise ConfigError("live ACP requires PROOFHUNTER_API_TOKEN")
        self._base = validate_api_base(api_base, live=live)
        headers = {"Accept": "application/json", "User-Agent": "proofhunter-acp/1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=timeout,
                                         transport=transport, follow_redirects=False,
                                         trust_env=False)

    def __repr__(self) -> str:
        return f"HttpControlApiClient(base={self._base!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_run(self, request: RunRequest, idempotency_key: str) -> RunStatus:
        if not 1 <= len(idempotency_key) <= 128 or not set(idempotency_key) <= (
                _IDEMPOTENCY_KEY_CHARS):
            raise ControlApiError(ReasonCode.INVALID_REQUEST, "invalid idempotency key",
                                  retryable=False)
        body = request.model_dump_json().encode("utf-8")
        return await self._call("POST", "/v1/runs", RunStatus, content=body,
                                headers={"Idempotency-Key": idempotency_key,
                                         "Content-Type": "application/json"},
                                accepted=frozenset({200, 202}))

    async def get_status(self, run_id: str) -> RunStatus:
        status = await self._call("GET", f"/v1/runs/{_checked_run_id(run_id)}", RunStatus)
        if status.run_id != run_id:
            raise ControlApiError(ReasonCode.PROVIDER_INVALID_RESPONSE,
                                  "status for a different run", retryable=False)
        return status

    async def get_receipt(self, run_id: str) -> ReceiptV1:
        receipt = await self._call("GET", f"/v1/runs/{_checked_run_id(run_id)}/receipt",
                                   ReceiptV1)
        if receipt.run_id != run_id or receipt.receipt_sha256 is None:
            raise ControlApiError(ReasonCode.PROVIDER_INVALID_RESPONSE,
                                  "receipt is not the sealed receipt of this run",
                                  retryable=False)
        return receipt

    async def _call(self, method: str, path: str, model: type[ModelT], *,
                    content: bytes | None = None, headers: Mapping[str, str] | None = None,
                    accepted: frozenset[int] = frozenset({200})) -> ModelT:
        last: ControlApiError | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                status, raw, response = await self._send(method, path, content, headers)
            except ControlApiError as error:
                if not error.retryable:
                    raise
                last = error
                continue
            if status in accepted:
                return _parse(model, raw)
            failure = _error_from(status, raw, response)
            if status in _RETRYABLE_STATUS:
                last = failure
                continue
            raise failure
        assert last is not None
        raise last

    async def _send(self, method: str, path: str, content: bytes | None,
                    headers: Mapping[str, str] | None) -> tuple[int, bytes, httpx.Response]:
        try:
            request = self._client.build_request(method, path, content=content,
                                                 headers=dict(headers or {}))
            response = await self._client.send(request, stream=True)
            try:
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise ControlApiError(ReasonCode.PAYLOAD_TOO_LARGE,
                                          "control API response too large", retryable=False)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ControlApiError(ReasonCode.PAYLOAD_TOO_LARGE,
                                              "control API response too large",
                                              retryable=False)
                    chunks.append(chunk)
            finally:
                await response.aclose()
        except httpx.TimeoutException as error:
            raise ControlApiError(ReasonCode.PROVIDER_TIMEOUT, "control API timed out",
                                  retryable=True) from error
        except httpx.HTTPError as error:
            raise ControlApiError(ReasonCode.PROVIDER_UNAVAILABLE, "control API unreachable",
                                  retryable=True) from error
        return response.status_code, b"".join(chunks), response


def _checked_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
        raise ControlApiError(ReasonCode.INVALID_REQUEST, "run_id must be a lowercase UUIDv4",
                              retryable=False)
    return run_id


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise ControlApiError(ReasonCode.PROVIDER_INVALID_RESPONSE,
                              f"control API returned an invalid {model.__name__}",
                              retryable=False) from error


def _error_from(status: int, raw: bytes, response: httpx.Response) -> ControlApiError:
    retry_after = _retry_after(response) if status in {429, 503} else None
    try:
        envelope = ErrorEnvelope.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError, ValueError):
        envelope = None
    if envelope is not None:
        body = envelope.error
        return ControlApiError(body.code, f"control API error {body.code.value}",
                               retryable=body.retryable or status in _RETRYABLE_STATUS,
                               http_status=status, retry_after_seconds=retry_after,
                               request_id=body.request_id)
    code = _STATUS_CODES.get(status, ReasonCode.PROVIDER_UNAVAILABLE if status >= 500
                             else ReasonCode.PROVIDER_INVALID_RESPONSE)
    return ControlApiError(code, f"control API HTTP {status}",
                           retryable=status in _RETRYABLE_STATUS or status == 429,
                           http_status=status, retry_after_seconds=retry_after)


def create_control_client(settings: Settings, *,
                          transport: httpx.AsyncBaseTransport | None = None
                          ) -> HttpControlApiClient:
    """Build the control API client from the ACP's two settings (base and token)."""
    token = settings.api_token.get_secret_value() if settings.api_token else None
    return HttpControlApiClient(settings.api_base, token, live=not settings.offline,
                                transport=transport)


__all__ = [
    "MAX_RESPONSE_BYTES",
    "ControlApiError",
    "HttpControlApiClient",
    "create_control_client",
    "validate_api_base",
]
