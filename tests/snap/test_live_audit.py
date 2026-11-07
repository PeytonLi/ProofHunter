import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from proofhunter.config import SnapSettings
from proofhunter.contracts import CandidateBank, Strategy, pool_sha256
from proofhunter.snap import pool_evidence
from proofhunter.snap.__main__ import main, smoke
from tests.snap.test_snap import ELIGIBLE, port, response, state


@pytest.mark.parametrize("request_id", ["req_proxy_mw6v41e6_9shk1z", "bad id", "x" * 129])
@pytest.mark.parametrize("status", [200, 402, 422, 429, 503])
async def test_header_metadata(status: int, request_id: str) -> None:
    route = port(lambda _: httpx.Response(status, json={**response(), "id": "ignored"},
                    headers={"x-request-id": request_id, "Retry-After": "3600"}))
    decision = await route.choose(state(), ELIGIBLE)
    safe_id = request_id if request_id.startswith("req_proxy") else None
    assert all(a["request_id"] == safe_id for a in route.attempts)
    assert decision.provider_request_id == (safe_id if status == 200 else None)
    assert route.requests_spent(state().run_id) == len(route.attempts)
    assert len(route.attempts) == (2 if status in (429, 503) else 1)


async def test_partial_probabilities_rejected() -> None:
    route = port(lambda _: httpx.Response(200, json=response(probabilities={"constraint": 1})))
    assert (await route.choose(state(), ELIGIBLE)).reason_code == "invalid_probabilities"


def test_cli_refuses_network_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("network must not run")
    monkeypatch.setattr("proofhunter.snap.__main__.smoke", forbidden)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2


@pytest.mark.parametrize("status", [200, 402, 422, 429, 503])
async def test_smoke_mocked(status: int) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "subq-snap-1.1"}]},
                                  headers={"x-request-id": "req_models"})
        offered = json.loads(request.content)["questions"]["next_action"]["criteria"]
        return httpx.Response(status, json=response(next(iter(offered)), confidence=1,
            probabilities={s: float(i == 0) for i, s in enumerate(offered)}),
            headers={"x-request-id": "req_decision"})

    evidence = await smoke(SnapSettings(api_key=SecretStr("fake-key")), 3,
                           transport=httpx.MockTransport(handle))
    assert calls.count("GET") == 1
    assert calls.count("POST") == evidence["decision_attempts"] == 3
    assert evidence["models"]["model_visible"] is True
    assert evidence["models"]["request_id"] == "req_models"
    for row in evidence["decisions"]:
        pools = row["pools"]["alternatives"]
        assert len({p["sha256"] for p in pools.values()}) == 3
        assert row["pools"]["chosen"] == pools[row["choice"]]
    encoded = json.dumps(evidence)
    assert "fake-key" not in encoded
    assert "Authorization" not in encoded
    assert "answers" not in encoded


async def test_empty_models_probe() -> None:
    route = port(lambda _: httpx.Response(200, json={"data": []}))
    probe = await route.probe_models()
    assert probe["status"] == 200
    assert probe["model_visible"] is False
    assert route.requests_spent(state().run_id) == 0


def test_cli_writes_sanitized_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def mocked(settings: SnapSettings, decisions: int) -> dict[str, object]:
        return await smoke(settings, decisions, transport=httpx.MockTransport(
            lambda request: httpx.Response(402, headers={"x-request-id": "req_no_credit"})))

    monkeypatch.setenv("SUBQ_API_KEY", "fake-key")
    monkeypatch.setattr("proofhunter.snap.__main__.smoke", mocked)
    output = tmp_path / "evidence.json"
    assert main(["--live", "--decisions", "1", "--output", str(output)]) == 0
    saved = json.loads(output.read_text())
    assert saved["decision_attempts"] == 1
    assert saved["decisions"][0]["attempts"][0]["request_id"] == "req_no_credit"
    assert "fake-key" not in output.read_text()


def test_empty_pool_evidence() -> None:
    bank = CandidateBank(generator_version="v1", seed=0, candidates=[],
                         bank_sha256=pool_sha256([]))
    assert pool_evidence(bank, Strategy.CONSTRAINT)["chosen"]["size"] == 0
