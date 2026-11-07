"""Private control API with injected repository and webhook boundaries."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException

from proofhunter.contracts import (
    ErrorBody,
    ErrorEnvelope,
    ReasonCode,
    ReceiptV1,
    ReplayRequest,
    RunRequest,
    RunStatus,
)
from proofhunter.ports import AdapterError, ApiDependencies, IdempotencyConflictError


@runtime_checkable
class _DatabaseReadiness(Protocol):
    def ready(self) -> bool: ...


@runtime_checkable
class _RetentionStore(Protocol):
    def purge_expired(
        self, now: datetime, *, batch_limit: int = 500, dry_run: bool = False
    ) -> dict[str, int]: ...


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dry_run: bool = True
    batch_limit: int = Field(default=500, ge=1, le=5000)


def _error(
    status: int,
    code: ReasonCode,
    *,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=code.value.replace("_", " "),
            request_id=uuid.uuid4().hex,
            retryable=retryable,
        )
    )
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status, headers=headers)


def _key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not 1 <= len(key) <= 128 or any(not 32 <= ord(c) <= 126 for c in key):
        raise HTTPException(422)
    return key


def create_app(deps: ApiDependencies) -> FastAPI:
    app = FastAPI(title="ProofHunter", version="1.0")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            if request.url.path.startswith("/v1/") or request.url.path == "/v1":
                api_token = deps.settings.api_token
                operator_token = deps.settings.operator_token
                if (
                    api_token is None
                    or operator_token is None
                    or hmac.compare_digest(
                        api_token.get_secret_value().encode(),
                        operator_token.get_secret_value().encode(),
                    )
                ):
                    return _error(
                        401, ReasonCode.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
                    )
                operator = request.method == "POST" and (
                    request.url.path.rstrip("/").endswith("/replays")
                    or request.url.path.rstrip("/") == "/v1/retention/purge"
                )
                token = deps.settings.operator_token if operator else deps.settings.api_token
                auth = request.headers.get("authorization", "")
                expected = "" if token is None else "Bearer " + token.get_secret_value()
                if not expected or not hmac.compare_digest(auth.encode(), expected.encode()):
                    return _error(
                        401, ReasonCode.UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
                    )
            webhook = request.url.path == "/webhooks/github"
            if webhook and (deps.webhook is None or not deps.settings.github.enable_webhook):
                return _error(404, ReasonCode.NOT_FOUND)
            limit = 256 * 1024 if webhook else 8 * 1024
            chunks = bytearray()
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > limit:
                    return _error(413, ReasonCode.PAYLOAD_TOO_LARGE)
                chunks.extend(chunk)
            request._body = bytes(chunks)
            return await call_next(request)
        except Exception:
            return _error(500, ReasonCode.INTERNAL_ERROR)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        del request
        code = ReasonCode.NOT_FOUND if error.status_code == 404 else ReasonCode.INVALID_REQUEST
        return _error(error.status_code, code, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, error: RequestValidationError) -> JSONResponse:
        del request, error
        return _error(422, ReasonCode.INVALID_REQUEST)

    @app.exception_handler(IdempotencyConflictError)
    async def conflict(request: Request, error: IdempotencyConflictError) -> JSONResponse:
        del request, error
        return _error(409, ReasonCode.CONFLICTING_IDEMPOTENCY_KEY)

    @app.exception_handler(AdapterError)
    async def adapter_error(request: Request, error: AdapterError) -> JSONResponse:
        del request
        statuses = {
            ReasonCode.INVALID_PR_URL: 422,
            ReasonCode.REPO_NOT_ENROLLED: 403,
            ReasonCode.RATE_LIMITED: 429,
            ReasonCode.REPLAY_UNAVAILABLE: 409,
            ReasonCode.RECEIPT_PENDING: 409,
            ReasonCode.NOT_FOUND: 404,
            ReasonCode.FORBIDDEN: 403,
        }
        headers = {"Retry-After": "60"} if error.code is ReasonCode.RATE_LIMITED else None
        return _error(
            statuses.get(error.code, 503), error.code, retryable=error.retryable, headers=headers
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready() -> Response:
        settings = deps.settings
        tokens = [t.get_secret_value() for t in (settings.api_token, settings.operator_token) if t]
        configured = len(tokens) == 2 and tokens[0] != tokens[1]
        if settings.offline:
            configured = configured and not (
                settings.github.enable_checks
                or settings.github.enable_webhook
                or settings.daytona.enable_live_runner
            )
        try:
            if isinstance(deps.store, _DatabaseReadiness):
                database_ready = deps.store.ready()
            else:
                deps.store.get_status("00000000-0000-4000-8000-000000000000")
                database_ready = True
        except Exception:
            database_ready = False
        try:
            heartbeat = (
                deps.heartbeat_store.read_heartbeat(settings.profile, deps.config_fingerprint)
                if deps.heartbeat_store is not None and deps.config_fingerprint is not None
                else None
            )
        except Exception:
            heartbeat = None
        if not configured or not database_ready or heartbeat is None:
            return _error(503, ReasonCode.INTERNAL_ERROR, retryable=True)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/runs", status_code=202, response_model=RunStatus)
    def create(request: Request, body: RunRequest) -> RunStatus:
        key = _key(request)
        target, enrollment = deps.validator.resolve(body.pr_url)
        sender = request.headers.get("x-proofhunter-requester")
        scope = (
            "service:default"
            if not sender
            else "acp:" + hashlib.sha256(sender.encode()).hexdigest()
        )
        return deps.store.create_run(
            body, idempotency_key=key, requester_scope=scope, target=target, enrollment=enrollment
        )

    @app.get("/v1/runs/{run_id}", response_model=RunStatus)
    def status(run_id: str) -> RunStatus:
        result = deps.store.get_status(run_id)
        if result is None:
            raise HTTPException(404)
        return result

    @app.get("/v1/runs/{run_id}/receipt", response_model=ReceiptV1)
    def receipt(run_id: str, request: Request) -> Response:
        result = deps.store.get_receipt(run_id)
        if result is None:
            if deps.store.get_status(run_id) is None:
                raise HTTPException(404)
            raise AdapterError(
                "store", ReasonCode.RECEIPT_PENDING, "receipt pending", retryable=True
            )
        etag = '"' + str(result.receipt_sha256) + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(result.model_dump(mode="json"), headers={"ETag": etag})

    @app.post("/v1/runs/{run_id}/replays", status_code=202, response_model=RunStatus)
    def replay(run_id: str, request: Request, body: ReplayRequest) -> RunStatus:
        return deps.store.create_replay(run_id, body, idempotency_key=_key(request))

    @app.post("/v1/retention/purge")
    def retention(body: RetentionRequest) -> dict[str, bool | dict[str, int]]:
        if not isinstance(deps.store, _RetentionStore):
            raise AdapterError(
                "store", ReasonCode.PROVIDER_UNAVAILABLE, "retention unavailable", retryable=False
            )
        return {
            "dry_run": body.dry_run,
            "counts": deps.store.purge_expired(
                deps.clock.now(), batch_limit=body.batch_limit, dry_run=body.dry_run
            ),
        }

    @app.post("/webhooks/github")
    async def webhook(request: Request) -> Response:
        verifier = deps.webhook
        if verifier is None or not deps.settings.github.enable_webhook:
            raise HTTPException(404)
        raw = await request.body()
        if not verifier.verify(raw, request.headers.get("x-hub-signature-256")):
            return _error(401, ReasonCode.INVALID_SIGNATURE)
        try:
            if not isinstance(json.loads(raw), dict):
                return _error(400, ReasonCode.INVALID_REQUEST)
            event = verifier.parse(
                request.headers.get("x-github-event", ""),
                request.headers.get("x-github-delivery", ""),
                raw,
            )
        except (ValueError, KeyError, TypeError, ValidationError):
            return _error(400, ReasonCode.INVALID_REQUEST)
        if event is None:
            return JSONResponse({"status": "ignored"})
        target, _ = deps.validator.resolve(
            f"https://github.com/{event.owner}/{event.repo}/pull/{event.number}"
        )
        if target.repo_id != event.repo_id or target.installation_id != event.installation_id:
            return _error(403, ReasonCode.REPO_NOT_ENROLLED)
        enqueued = deps.store.record_webhook_delivery(event)
        return JSONResponse(
            {"status": "queued" if enqueued else "duplicate"}, status_code=202 if enqueued else 200
        )

    return app
