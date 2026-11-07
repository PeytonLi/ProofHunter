"""Bounded SubQ Snap routing; offline startup should bind FixtureRoute instead."""

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from proofhunter.config import Settings, SnapSettings
from proofhunter.contracts import (
    ROUTE_PROBABILITY_TOLERANCE,
    STRATEGY_FALLBACK_ORDER,
    CandidateBank,
    DecisionState,
    RouteDecision,
    RouteMode,
    RouteReasonCode,
    SafeToken,
    Strategy,
    pool_sha256,
)

_LOG = logging.getLogger(__name__)
_TOKEN = TypeAdapter(SafeToken)
_CRITERIA = {
    Strategy.BOUNDARY: "Enumerate discount endpoints and half-cent boundaries",
    Strategy.CONSTRAINT: "Search for inputs on which candidate behaviors disagree",
    Strategy.SEEDED_FUZZ: "Sample remaining legal, uncovered inputs",
}
_LIMIT = 16 * 1024
_TIMEOUT = 3.0


def _token(value: object) -> str | None:
    try:
        return _TOKEN.validate_python(value)
    except ValidationError:
        return None


def _probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not numeric")
    if not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("outside probability range")
    return round(float(value), 12)


def pool_evidence(bank: CandidateBank, choice: Strategy,
                  used_input_sha256: Iterable[str] = ()) -> dict[str, Any]:
    """Describe the exact shared candidate pools without exposing candidate inputs."""
    used = tuple(used_input_sha256)
    pools = {s.value: {"size": len(pool := bank.pool_for(s, used)),
                       "sha256": pool_sha256(pool)} for s in Strategy}
    return {"choice": choice.value, "chosen": pools[choice.value], "alternatives": pools}


class SnapRoute:
    """One worker-owned instance; retain counters for the lifetime of each run."""

    def __init__(self, settings: Settings | SnapSettings, *,
                 client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 request_limit: int = 3,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings if isinstance(settings, SnapSettings) else settings.snap
        self._limit = max(0, min(3, request_limit, 3 if isinstance(settings, SnapSettings)
                                else settings.budget.max_snap_requests))
        self.attempts: list[dict[str, Any]] = []
        self._client = client
        self._transport = transport
        self._clock = clock
        self._spent: dict[str, int] = {}
        self._retried: set[str] = set()
        self._lock = asyncio.Lock()

    def requests_spent(self, run_id: str) -> int:
        """Actual decision HTTP attempts, including unsuccessful calls and retries."""
        return self._spent.get(run_id, 0)

    async def _http(self, method: str, url: str,
                    body: dict[str, object] | None = None) -> tuple[int, object, str | None]:
        key = self._settings.api_key
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}

        async def send(client: httpx.AsyncClient) -> tuple[int, object, str | None]:
            async with asyncio.timeout(_TIMEOUT):
                async with client.stream(method, url, headers=headers, json=body,
                                         timeout=_TIMEOUT, follow_redirects=False) as response:
                    metadata.update(status=response.status_code,
                                    request_id=_token(response.headers.get("x-request-id")))
                    request_id = metadata["request_id"]
                    if response.status_code != 200:
                        return response.status_code, None, request_id
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > _LIMIT:
                            return 200, None, request_id
                        data.extend(chunk)
                    try:
                        return 200, json.loads(data), request_id
                    except (ValueError, UnicodeError, RecursionError):
                        return 200, None, request_id

        started = self._clock()
        metadata: dict[str, Any] = {"method": method, "path": httpx.URL(url).path,
                                    "status": None, "request_id": None}
        try:
            if self._client is not None:
                return await send(self._client)
            async with httpx.AsyncClient(transport=self._transport, trust_env=False) as client:
                return await send(client)
        finally:
            metadata["latency_ms"] = max(0, int((self._clock() - started) * 1000))
            self.attempts.append(metadata)
            _LOG.info("snap attempt=%s", metadata)

    async def check_model_visibility(self) -> bool:
        """Operator-only probe, not charged to decision budgets; never called by choose."""
        return bool((await self.probe_models())["model_visible"])

    async def probe_models(self) -> dict[str, Any]:
        """Sanitized operator probe; never exposes the provider payload."""
        before = len(self.attempts)
        visible = False
        if self._settings.api_key is None:
            return {"status": None, "request_id": None, "latency_ms": 0,
                    "model_visible": False}
        try:
            status, payload, _ = await self._http("GET", self._settings.models_url)
            visible = (status == 200 and isinstance(payload, dict)
                    and isinstance(payload.get("data"), list)
                    and any(isinstance(row, dict) and row.get("id") == self._settings.model
                            for row in payload["data"]))
        except (httpx.HTTPError, TimeoutError):
            pass
        return {**self.attempts[before], "model_visible": visible}

    def _body(self, state: DecisionState, eligible: tuple[Strategy, ...]) -> dict[str, object]:
        structured = state.model_dump(mode="json")
        return {
            "model": self._settings.model,
            "state": {"contract": state.contract_id, "coverage": structured["observed_coverage"],
                      "remaining_pairs": state.remaining_pairs, "step": state.step,
                      "missing_coverage": structured["missing_coverage"],
                      "unseen_candidates": {s.value: state.unseen_candidates.get(s, 0)
                                            for s in eligible},
                      "last_outcome": structured["last_outcome"], "belief": structured["belief"]},
            "questions": {"next_action": {
                "type": "choice",
                "instructions": "Choose among the eligible preparation actions for the next probe.",
                "criteria": {s.value: _CRITERIA[s] for s in eligible}}},
        }

    async def choose(self, state: DecisionState, eligible: tuple[Strategy, ...]) -> RouteDecision:
        if not eligible or len(set(eligible)) != len(eligible):
            raise ValueError("eligible must be non-empty and unique")
        async with self._lock:
            return await self._choose(state, eligible)

    async def _choose(self, state: DecisionState, eligible: tuple[Strategy, ...]) -> RouteDecision:
        fallback = next(s for s in STRATEGY_FALLBACK_ORDER if s in eligible)
        started: float | None = None
        status: int | None = None
        request_id: str | None = None

        def result(reason: RouteReasonCode, mode: RouteMode = RouteMode.FALLBACK,
                   **values: Any) -> RouteDecision:
            decision = RouteDecision(
                choice=values.get("choice", fallback), eligible=list(eligible), mode=mode,
                model_id=self._settings.model if mode is RouteMode.LIVE else None,
                probabilities=values.get("probabilities"), confidence=values.get("confidence"),
                provider_request_id=request_id if mode is RouteMode.LIVE else None,
                latency_ms=None if started is None else max(0, int((self._clock()-started)*1000)),
                reason_code=reason)
            _LOG.info("snap choice=%s alternatives=%s mode=%s reason=%s status=%s "
                      "request_id=%s latency_ms=%s", decision.choice,
                      [s.value for s in eligible], mode, reason, status,
                      decision.provider_request_id, decision.latency_ms)
            return decision

        if len(eligible) == 1:
            return result(RouteReasonCode.SINGLE_ELIGIBLE, RouteMode.POLICY)
        allowance = min(state.snap_requests_remaining,
                        self._limit - self.requests_spent(state.run_id))
        if state.step > 2 or allowance <= 0:
            return result(RouteReasonCode.CALL_BUDGET, RouteMode.POLICY)
        if self._settings.api_key is None:
            return result(RouteReasonCode.PROVIDER_UNAVAILABLE)
        started = self._clock()
        while allowance > 0:
            allowance -= 1
            self._spent[state.run_id] = self.requests_spent(state.run_id) + 1
            status = None
            request_id = None
            try:
                status, payload, request_id = await self._http("POST", self._settings.url,
                                                   self._body(state, eligible))
                if status == 200:
                    return self._parse(payload, eligible, result)
                if status in (403, 404, 410):
                    return result(RouteReasonCode.PREVIEW_UNAVAILABLE)
                if status in (401, 402):
                    return result(RouteReasonCode.PROVIDER_UNAVAILABLE)
                if status == 429:
                    reason = RouteReasonCode.RATE_LIMITED
                elif 500 <= status <= 599:
                    reason = RouteReasonCode.PROVIDER_UNAVAILABLE
                else:
                    return result(RouteReasonCode.INVALID_RESPONSE)
            except (httpx.TimeoutException, TimeoutError):
                reason = RouteReasonCode.PROVIDER_TIMEOUT
            except httpx.HTTPError:
                reason = RouteReasonCode.PROVIDER_UNAVAILABLE
            if state.run_id in self._retried or allowance <= 0:
                return result(reason)
            self._retried.add(state.run_id)
        return result(RouteReasonCode.CALL_BUDGET, RouteMode.POLICY)

    def _parse(self, payload: object, eligible: tuple[Strategy, ...],
               result: Callable[..., RouteDecision]) -> RouteDecision:
        if not isinstance(payload, dict):
            return result(RouteReasonCode.INVALID_RESPONSE)
        if "model" in payload and payload["model"] != self._settings.model:
            return result(RouteReasonCode.INVALID_RESPONSE)
        answers = payload.get("answers")
        answer = answers.get("next_action") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            return result(RouteReasonCode.INVALID_RESPONSE)
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in eligible:
            return result(RouteReasonCode.INVALID_CHOICE)
        probabilities = None
        if "probabilities" in answer:
            raw = answer["probabilities"]
            try:
                if not isinstance(raw, dict) or set(raw) != set(eligible):
                    raise ValueError("invalid probability keys")
                probabilities = {Strategy(k): _probability(v) for k, v in raw.items()}
                if abs(math.fsum(probabilities.values()) - 1) > ROUTE_PROBABILITY_TOLERANCE:
                    raise ValueError("invalid probability sum")
            except (ValueError, TypeError):
                return result(RouteReasonCode.INVALID_PROBABILITIES)
        try:
            confidence = _probability(answer["confidence"]) if "confidence" in answer else None
        except ValueError:
            return result(RouteReasonCode.INVALID_RESPONSE)
        return result(RouteReasonCode.PROVIDER_CHOICE, RouteMode.LIVE,
                      choice=Strategy(choice), probabilities=probabilities,
                      confidence=confidence)


def create_route_port(settings: Settings, *, client: httpx.AsyncClient | None = None,
                      transport: httpx.AsyncBaseTransport | None = None,
                      clock: Callable[[], float] = time.monotonic) -> SnapRoute:
    return SnapRoute(settings, client=client, transport=transport, clock=clock)


__all__ = ["SnapRoute", "create_route_port"]
