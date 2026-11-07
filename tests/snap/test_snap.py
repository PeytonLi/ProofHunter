import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from proofhunter.config import Settings, SnapSettings, load_settings
from proofhunter.contracts import CandidateBank, DecisionState, Strategy, pool_sha256
from proofhunter.ports import RoutePort
from proofhunter.snap import SnapRoute, create_route_port

ROOT = Path(__file__).resolve().parents[2]
ELIGIBLE = tuple(Strategy)


def fixture(name: str) -> Any:
    return json.loads((ROOT / "fixtures/providers" / f"snap_{name}.json").read_bytes())


def state(**updates: Any) -> DecisionState:
    data = json.loads((ROOT / "schemas/samples/DecisionState.json").read_bytes())
    data.update(updates)
    return DecisionState.model_validate(data)


def settings(key: bool = True) -> Settings:
    return load_settings({"PROOFHUNTER_PROFILE": "offline"}).model_copy(update={
        "snap": SnapSettings(api_key=SecretStr("mock-only-secret") if key else None)})


def port(handler: Callable[[httpx.Request], httpx.Response], *, key: bool = True) -> SnapRoute:
    return create_route_port(settings(key), transport=httpx.MockTransport(handler))


def response(choice: str = "constraint", **fields: Any) -> dict[str, Any]:
    return {"answers": {"next_action": {"type": "choice", "choice": choice, **fields}}}


@pytest.mark.parametrize("eligible", [ELIGIBLE, (Strategy.BOUNDARY, Strategy.CONSTRAINT)])
async def test_request_and_fixture_response(eligible: tuple[Strategy, ...],
                                            caplog: pytest.LogCaptureFixture) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == settings().snap.url
        assert request.headers["Authorization"] == "Bearer mock-only-secret"
        assert request.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 3.0)
        body = json.loads(request.content)
        expected = fixture("request")
        assert body["model"] == expected["model"]
        assert body["questions"]["next_action"] == {
            **expected["questions"]["next_action"],
            "criteria": {s: expected["questions"]["next_action"]["criteria"][s]
                         for s in eligible}}
        assert {k: body["state"][k] for k in expected["state"]} == expected["state"]
        assert set(body["state"]) == {"contract", "coverage", "remaining_pairs", "step",
                                       "missing_coverage", "unseen_candidates", "last_outcome",
                                       "belief"}
        assert set(body["state"]["unseen_candidates"]) == set(eligible)
        assert body["state"]["belief"] == state().belief
        calls.append(request)
        payload = fixture("response")
        if len(eligible) == 2:
            payload["answers"]["next_action"]["probabilities"] = {
                "constraint": 0.75, "boundary": 0.25}
        return httpx.Response(200, json=payload)

    route = port(handle)
    typed: RoutePort = route
    with caplog.at_level(logging.INFO):
        decision = await typed.choose(state(), eligible)
    assert decision.mode == "live"
    assert decision.reason_code == "provider_choice"
    assert decision.choice is Strategy.CONSTRAINT
    assert decision.model_id == settings().snap.model
    assert decision.provider_request_id == "sysone-fixture-0001"
    assert decision.latency_ms is not None
    assert route.requests_spent(state().run_id) == len(calls) == 1
    assert "mock-only-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "belief" not in caplog.text
    for field in ("choice=constraint", "alternatives=", "mode=live", "reason=provider_choice",
                  "status=200", "request_id=sysone-fixture-0001", "latency_ms="):
        assert field in caplog.text


@pytest.mark.parametrize(("updates", "eligible", "key", "reason", "mode"), [
    ({"step": 3}, ELIGIBLE, True, "call_budget", "policy"),
    ({"snap_requests_remaining": 0}, ELIGIBLE, True, "call_budget", "policy"),
    ({}, (Strategy.SEEDED_FUZZ,), True, "single_eligible", "policy"),
    ({}, ELIGIBLE, False, "provider_unavailable", "fallback"),
])
async def test_no_http(updates: dict[str, Any], eligible: tuple[Strategy, ...], key: bool,
                       reason: str, mode: str) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected HTTP")

    route = port(fail, key=key)
    decision = await route.choose(state(**updates), eligible)
    assert decision.reason_code == reason
    assert decision.mode == mode
    assert decision.latency_ms is None
    assert route.requests_spent(state().run_id) == 0
    assert decision.choice == (Strategy.SEEDED_FUZZ if len(eligible) == 1 else Strategy.CONSTRAINT)


async def test_empty_and_duplicate_eligible() -> None:
    route = port(lambda _: httpx.Response(200))
    for eligible in ((), (Strategy.BOUNDARY, Strategy.BOUNDARY)):
        with pytest.raises(ValueError):
            await route.choose(state(), eligible)


@pytest.mark.parametrize("choice", ["seeded_fuzz", "CONSTRAINT", 1, None, True])
async def test_invalid_choice(choice: Any) -> None:
    route = port(lambda _: httpx.Response(200, json=response(choice)))
    decision = await route.choose(state(), (Strategy.BOUNDARY, Strategy.CONSTRAINT))
    assert decision.reason_code == "invalid_choice"
    assert decision.choice is Strategy.CONSTRAINT
    assert decision.provider_request_id is None


@pytest.mark.parametrize("probabilities", [
    {"constraint": float("nan")}, {"constraint": float("inf")},
    {"constraint": -0.1, "boundary": 1.1}, {"constraint": 1.1},
    {"constraint": 0.8}, {"unoffered": 1.0}, {"constraint": True},
    {"constraint": 10**1000}, {}, None, [],
])
async def test_invalid_probabilities(probabilities: Any) -> None:
    payload = response(probabilities=probabilities)
    route = port(lambda _: httpx.Response(200, content=json.dumps(payload)))
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.mode == "fallback"
    assert decision.reason_code == "invalid_probabilities"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"answers": []}, {"answers": {"next_action": []}},
    {"answers": {"next_action": {"type": "text", "choice": "constraint"}}},
    {**response(), "model": "wrong-model"}, {**response(), "model": None},
    response(confidence=True), response(confidence=-0.1), response(confidence=1.1),
    response(confidence=None), response(confidence="0.5"),
])
async def test_malformed(payload: Any) -> None:
    route = port(lambda _: httpx.Response(200, json=payload))
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.reason_code == "invalid_response"
    assert decision.mode == "fallback"
    assert decision.latency_ms is not None


@pytest.mark.parametrize("content", [b"not json", b"\xff", b" " * 16385,
                                     b"[" * 1500 + b"]" * 1500])
async def test_bad_or_oversized_body(content: bytes) -> None:
    route = port(lambda _: httpx.Response(200, content=content))
    assert (await route.choose(state(), ELIGIBLE)).reason_code == "invalid_response"


@pytest.mark.parametrize("confidence", [float("nan"), float("inf")])
async def test_nonfinite_confidence(confidence: float) -> None:
    route = port(lambda _: httpx.Response(200, content=json.dumps(response(confidence=confidence))))
    assert (await route.choose(state(), ELIGIBLE)).reason_code == "invalid_response"


@pytest.mark.parametrize("request_id", [None, "bad\nidentifier", "x" * 129, 15])
async def test_optional_fields_and_sanitization(request_id: object) -> None:
    route = port(lambda _: httpx.Response(200, json={**response(), "id": request_id}))
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.mode == "live"
    assert decision.provider_request_id is None
    assert decision.probabilities is None
    assert decision.confidence is None


async def test_rounding_subset_and_latency() -> None:
    times = iter([1.0, 1.125])
    route = create_route_port(settings(), clock=lambda: next(times), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response(probabilities={"constraint": 0.999},
                                                   confidence=0.123456789012345))))
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.confidence == 0.123456789012
    assert decision.probabilities == {Strategy.CONSTRAINT: 0.999}
    assert decision.latency_ms == 125


@pytest.mark.parametrize(("status", "reason", "attempts"), [
    (403, "preview_unavailable", 1), (404, "preview_unavailable", 1),
    (410, "preview_unavailable", 1), (401, "provider_unavailable", 1),
    (429, "rate_limited", 2), (500, "provider_unavailable", 2),
    (503, "provider_unavailable", 2), (400, "invalid_response", 1),
    (302, "invalid_response", 1),
])
async def test_status_mapping(status: int, reason: str, attempts: int) -> None:
    route = port(lambda _: httpx.Response(status, headers={"Location": "https://example.org"}))
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.reason_code == reason
    assert decision.mode == "fallback"
    assert decision.provider_request_id is None
    assert decision.latency_ms is not None
    assert route.requests_spent(state().run_id) == attempts


@pytest.mark.parametrize(("error", "reason"), [
    (httpx.ReadTimeout, "provider_timeout"), (httpx.ConnectError, "provider_unavailable"),
    (httpx.RemoteProtocolError, "provider_unavailable"), (TimeoutError, "provider_timeout"),
])
async def test_transport_errors(error: type[Exception], reason: str) -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        raise error("secret provider details")

    route = port(handle)
    decision = await route.choose(state(), ELIGIBLE)
    assert decision.reason_code == reason
    assert route.requests_spent(state().run_id) == 2


async def test_retry_once_per_run_and_budget() -> None:
    statuses = iter([429, 200, 503])
    route = port(lambda _: httpx.Response(next(statuses), json=response()))
    assert (await route.choose(state(), ELIGIBLE)).mode == "live"
    assert route.requests_spent(state().run_id) == 2
    decision = await route.choose(state(step=2), ELIGIBLE)
    assert decision.reason_code == "provider_unavailable"
    assert route.requests_spent(state().run_id) == 3
    assert (await route.choose(state(step=2), ELIGIBLE)).reason_code == "call_budget"
    assert route.requests_spent(state().run_id) == 3


async def test_single_remaining_request_and_run_isolation() -> None:
    route = port(lambda _: httpx.Response(429))
    assert (await route.choose(state(snap_requests_remaining=1), ELIGIBLE)).reason_code == (
        "rate_limited")
    assert route.requests_spent(state().run_id) == 1
    other = state(run_id=str(uuid4()))
    assert (await route.choose(other, ELIGIBLE)).reason_code == "rate_limited"
    assert route.requests_spent(other.run_id) == 2
    assert route.requests_spent(state().run_id) == 1


async def test_fallback_order_without_constraint() -> None:
    route = port(lambda _: httpx.Response(401))
    decision = await route.choose(state(), (Strategy.SEEDED_FUZZ, Strategy.BOUNDARY))
    assert decision.choice is Strategy.BOUNDARY


@pytest.mark.parametrize("payload", [fixture("models"), {"data": []}, {}, [],
                                    {"data": [None, {"id": "other"}]}])
async def test_visibility(payload: Any) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == settings().snap.models_url
        assert request.headers["Authorization"] == "Bearer mock-only-secret"
        return httpx.Response(200, json=payload)

    route = port(handle)
    assert await route.check_model_visibility() is (payload == fixture("models"))
    assert route.requests_spent(state().run_id) == 0


async def test_visibility_failures() -> None:
    assert not await port(lambda _: httpx.Response(403)).check_model_visibility()
    assert not await port(lambda _: httpx.Response(200), key=False).check_model_visibility()

    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable")

    assert not await port(fail).check_model_visibility()


async def test_real_pinned_bank_route_changes_candidate_pool() -> None:
    bank = CandidateBank.model_validate_json(
        (ROOT / "fixtures/demo/candidate_bank.seed17.json").read_bytes())
    eligible = bank.eligible_strategies(set())
    pools = []
    for choice in ("constraint", "boundary"):
        def handle(_: httpx.Request, choice: str = choice) -> httpx.Response:
            return httpx.Response(200, json=response(choice))

        route = port(handle)
        decision = await route.choose(state(), eligible)
        assert decision.mode == "live"
        assert decision.choice.value == choice
        pools.append(bank.pool_for(decision.choice, set()))
    assert pools[0] != pools[1]
    assert pool_sha256(pools[0]) != pool_sha256(pools[1])


async def test_injected_client_not_closed() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response()))) as client:
        route = create_route_port(settings(), client=client)
        assert (await route.choose(state(), ELIGIBLE)).mode == "live"
        assert not client.is_closed


async def test_concurrent_calls_cannot_overspend() -> None:
    route = port(lambda _: httpx.Response(503))
    decisions = await asyncio.gather(*(route.choose(state(), ELIGIBLE) for _ in range(5)))
    assert route.requests_spent(state().run_id) == 3
    assert [d.reason_code for d in decisions] == [
        "provider_unavailable", "provider_unavailable", "call_budget", "call_budget", "call_budget"]


async def test_configured_budget_applies() -> None:
    config = settings()
    config = config.model_copy(update={"budget": config.budget.model_copy(
        update={"max_snap_requests": 1})})
    route = create_route_port(config, transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    assert (await route.choose(state(), ELIGIBLE)).reason_code == "provider_unavailable"
    assert route.requests_spent(state().run_id) == 1
    assert (await route.choose(state(), ELIGIBLE)).reason_code == "call_budget"
