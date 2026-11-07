from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from fixtures.adapters import FixtureControlApi
from proofhunter.contracts import ReceiptV1
from proofhunter.ports import AdapterError

ROOT = Path(__file__).resolve().parents[2]
PR_URL = "https://github.com/proofhunter-demo/checkout-demo/pull/1"
BASE = "http://control.internal:8080"


def fixture_receipt(name: str = "buggy") -> ReceiptV1:
    path = ROOT / "fixtures" / "demo" / "receipts" / f"{name}.seed17.json"
    return ReceiptV1.model_validate_json(path.read_bytes())


_HTTP = {"not_found": 404, "receipt_pending": 409, "conflicting_idempotency_key": 409,
         "invalid_request": 400, "repository_not_enrolled": 403, "rate_limited": 429}


class FakeControlHttp:
    """HTTP surface of the control API (PRD section 9) over the fixture store."""

    def __init__(self, api: FixtureControlApi | None = None) -> None:
        self.api = api or FixtureControlApi()
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def error(status: int, code: str, retryable: bool = False,
              headers: dict[str, str] | None = None) -> httpx.Response:
        body = {"error": {"code": code, "message": "fixture error", "request_id": "req-1",
                          "retryable": retryable}}
        return httpx.Response(status, json=body, headers=headers)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            response = self.override(request)
            if response is not None:
                return response
        path = request.url.path
        try:
            if request.method == "POST" and path == "/v1/runs":
                key = request.headers.get("idempotency-key")
                if not key:
                    return self.error(400, "invalid_request")
                from proofhunter.contracts import RunRequest

                status = await self.api.create_run(
                    RunRequest.model_validate_json(request.content), key)
                return httpx.Response(202, content=status.model_dump_json())
            parts = path.split("/")
            if request.method == "GET" and len(parts) == 4 and parts[1:3] == ["v1", "runs"]:
                return httpx.Response(
                    200, content=(await self.api.get_status(parts[3])).model_dump_json())
            if request.method == "GET" and len(parts) == 5 and parts[4] == "receipt":
                return httpx.Response(
                    200, content=(await self.api.get_receipt(parts[3])).model_dump_json())
        except AdapterError as error:
            return self.error(_HTTP.get(error.code.value, 500), error.code.value,
                              error.retryable)
        except ValueError:
            return self.error(400, "invalid_request")
        return self.error(404, "not_found")


@pytest.fixture
def fake_http() -> FakeControlHttp:
    return FakeControlHttp()


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)
