"""Provider failure modes for the Meta and frontier baselines (no network: MockTransport)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from proofhunter import contracts as c
from research.baselines import frontier as f
from research.baselines import meta as m
from research.baselines.ledger import MemoryLedger
from research.baselines.selectors import fixed_choice
from research.baselines.smoke import generator_input, run_episode
from research.schemas.interfaces import EpisodeContext, EpisodeState, PairFeedback, Selector
from research.schemas.models import CostStatus, MeasurementMode, Outcome

MODEL = "gpt-6-sol"
PROVIDERS = Path(__file__).resolve().parents[3] / "fixtures" / "providers"


def state(ctx: EpisodeContext, step: int = 1, history: list[PairFeedback] | None = None,
          available: list[c.Candidate] | None = None) -> EpisodeState:
    return EpisodeState(step=step, remaining_pairs=7 - step,
                        available=available or list(ctx.bank.candidates),
                        missing_coverage=[], history=history or [])


def usage() -> dict[str, Any]:
    return {"input_tokens": 1000, "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0}}


def message(text: str) -> dict[str, Any]:
    return {"status": "completed", "usage": usage(), "output": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def tool_call(candidate_id: str, call_id: str = "call_1") -> dict[str, Any]:
    return {"status": "completed", "usage": usage(), "output": [
        {"type": "function_call", "name": "run_pair", "call_id": call_id,
         "arguments": json.dumps({"candidate_id": candidate_id})}]}


class Script:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=item)


def client(script: Script, ledger: MemoryLedger | None = None,
           key: str | None = "sk-test") -> f.ResponsesClient:
    return f.ResponsesClient(model_id=MODEL, api_key=key, ledger=ledger,
                             transport=httpx.MockTransport(script), sleep=lambda _: None,
                             mode=MeasurementMode.OFFLINE)


# ------------------------------------------------------------------ frontier


def test_models_json_pins() -> None:
    from research.baselines.config import frontier_model_ids, load_models

    cfg = load_models()
    assert frontier_model_ids() == ("gpt-6-sol", "gpt-6-astra")
    assert cfg["frontier"]["reasoning_effort"] == "low"
    assert cfg["frontier"]["pricing_date"] == "2026-11-07"
    assert cfg["meta"]["max_completion_tokens"] == 4096
    assert cfg["meta"]["reasoning_effort"] == "low"
    with pytest.raises(ValueError):
        f.ResponsesClient(model_id="gpt-invented", api_key="x")


def test_reviewer_ranks_once_without_feedback(ctx: EpisodeContext) -> None:
    ids = f.candidate_ids(ctx.bank)
    script = Script(message(json.dumps({"ranking": ["c03", "zz", "c01", "c03"]})))
    ledger = MemoryLedger()
    sel = f.FrontierReviewer(client(script, ledger))
    assert isinstance(sel, Selector)
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == ids["c03"].input_sha256
    fb = PairFeedback(1, ids["c03"].input_sha256, Outcome.MATCH, 1, 1, 1)
    rest = [x for x in ctx.bank.candidates if x is not ids["c03"]]
    assert sel.choose(state(ctx, 2, [fb], rest)) == ids["c01"].input_sha256
    assert len(script.bodies) == 1
    body = script.bodies[0]
    assert body["model"] == MODEL
    assert body["reasoning"] == {"effort": "low"}
    assert body["store"] is False
    assert "tools" not in body
    assert body["text"]["format"]["strict"] is True
    assert "outcome" not in json.dumps(body["input"])
    (entry,) = ledger.entries()
    assert entry.cost_status is CostStatus.ESTIMATED
    assert entry.paid
    assert entry.cost_usd == pytest.approx((1000 * 2 + 100 * 10) / 1e6)


@pytest.mark.parametrize(("response", "reason", "calls"), [
    (httpx.Response(503), "http_503", 3),
    (httpx.Response(429, headers={"retry-after": "1"}), "http_429", 3),
    (httpx.Response(400), "http_400", 1),
    (httpx.ReadTimeout("t"), "provider_timeout", 3),
    (httpx.ConnectError("down"), "provider_unavailable", 3),
    (httpx.Response(200, content=b"not json"), "malformed_response", 1),
    ({"status": "completed", "output": "x"}, "malformed_response", 1),
    (message("not json"), "malformed_output", 1),
    (message(json.dumps({"ranking": "c01"})), "malformed_output", 1),
    (message(json.dumps({"ranking": ["nope"]})), "no_valid_candidates", 1),
    ({"status": "incomplete", "output": [], "usage": usage()}, "status_incomplete", 1),
    ({"status": "completed", "usage": usage(), "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}, "refusal", 1),
])
def test_reviewer_failures_degrade_to_fixed_order(ctx: EpisodeContext, response: Any,
                                                  reason: str, calls: int) -> None:
    script, ledger = Script(response), MemoryLedger()
    sel = f.FrontierReviewer(client(script, ledger))
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == fixed_choice(ctx.bank.candidates)
    assert sel.degraded == reason
    assert len(script.bodies) == calls
    assert len(ledger.entries()) == calls
    sel.choose(state(ctx))
    assert len(script.bodies) == calls


def test_missing_key_never_calls(ctx: EpisodeContext) -> None:
    script, ledger = Script(message("{}")), MemoryLedger()
    sel = f.FrontierAgent(client(script, ledger, key=None))
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == fixed_choice(ctx.bank.candidates)
    assert sel.degraded == "provider_unavailable"
    assert not script.bodies
    assert not ledger.entries()


def test_agent_tool_loop_feedback_and_budget(ctx: EpisodeContext) -> None:
    ids = f.candidate_ids(ctx.bank)
    script = Script(tool_call("c05", "a"), tool_call("c05", "b"), tool_call("c02", "c"))
    sel = f.FrontierAgent(client(script))
    sel.reset(ctx)
    first = sel.choose(state(ctx))
    assert first == ids["c05"].input_sha256
    body = script.bodies[0]
    assert body["tool_choice"] == {"type": "function", "name": "run_pair"}
    assert body["parallel_tool_calls"] is False
    assert [t["name"] for t in body["tools"]] == ["run_pair"]
    assert body["tools"][0]["strict"] is True
    assert "6 paired probes" in body["instructions"]
    fb = PairFeedback(1, first, Outcome.MATCH, 10, 10, 10)
    rest = [x for x in ctx.bank.candidates if x.input_sha256 != first]
    assert sel.choose(state(ctx, 2, [fb], rest)) == ids["c02"].input_sha256
    outputs = [i for i in script.bodies[1]["input"] if i.get("type") == "function_call_output"]
    assert json.loads(outputs[0]["output"]) == {
        "outcome": "match", "expected_cents": 10, "base_cents": 10, "head_cents": 10,
        "remaining_pairs": 5}
    errors = [i for i in script.bodies[2]["input"] if i.get("type") == "function_call_output"]
    assert "error" in json.loads(errors[-1]["output"])
    assert "5 paired probes" in script.bodies[1]["instructions"]


def test_agent_invalid_tool_calls_capped(ctx: EpisodeContext) -> None:
    script = Script(tool_call("c99"))
    sel = f.FrontierAgent(client(script))
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == fixed_choice(ctx.bank.candidates)
    assert sel.degraded == "invalid_tool_calls"
    assert len(script.bodies) == 3


def test_agent_text_without_tool_call(ctx: EpisodeContext) -> None:
    sel = f.FrontierAgent(client(Script(message("I refuse to call tools"))))
    sel.reset(ctx)
    sel.choose(state(ctx))
    assert sel.degraded == "malformed_output"


def test_agent_full_episode_stays_in_pool_and_budget(ctx: EpisodeContext) -> None:
    ids = list(f.candidate_ids(ctx.bank))
    script = Script(*[tool_call(i, f"k{n}") for n, i in enumerate(ids)])
    row = run_episode(f.FrontierAgent(client(script)), ctx, "fixed")
    assert row["pairs"] == 6
    assert row["first_mismatch_step"] is None


def test_free_input_not_combined() -> None:
    with pytest.raises(NotImplementedError):
        f.frontier_agent(MODEL, free_input=True)
    assert isinstance(f.frontier_agent(MODEL, api_key="x"), Selector)
    assert isinstance(f.frontier_reviewer(MODEL, api_key="x"), Selector)


# ------------------------------------------------------------------ meta


def meta_ok() -> httpx.Response:
    return httpx.Response(200, content=(PROVIDERS / "meta_response.json").read_bytes())


async def _no_sleep(_: float) -> None:
    return None


def meta_gen(handler: Any, ledger: MemoryLedger | None = None,
             key: str | None = "mk-test") -> m.MetaGenerator:
    return m.MetaGenerator(api_key=key, ledger=ledger, transport=httpx.MockTransport(handler),
                           sleep=_no_sleep, mode=MeasurementMode.OFFLINE)


def test_meta_success_uses_production_request(ctx: EpisodeContext) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return meta_ok()

    ledger = MemoryLedger()
    gen = meta_gen(handler, ledger)
    plan = gen.propose(generator_input(ctx))
    assert plan.hypotheses
    assert gen.last is not None
    assert gen.last.mode is c.ProviderMode.LIVE
    assert seen[0]["max_completion_tokens"] == 4096
    assert seen[0]["reasoning_effort"] == "low"
    assert seen[0]["model"] == "muse-spark-1.3"
    (entry,) = ledger.entries()
    assert entry.provider == "meta"
    assert entry.cost_status is CostStatus.UNKNOWN


@pytest.mark.parametrize(("handler", "reason", "attempts"), [
    (lambda r: httpx.Response(503), c.ReasonCode.PROVIDER_UNAVAILABLE, 2),
    (lambda r: httpx.Response(200, content=b"{bad"), c.ReasonCode.PROVIDER_INVALID_RESPONSE, 1),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t")), c.ReasonCode.PROVIDER_TIMEOUT, 2),
])
def test_meta_failures_fall_back_to_builtin(ctx: EpisodeContext, handler: Any,
                                            reason: c.ReasonCode, attempts: int) -> None:
    ledger = MemoryLedger()
    gen = meta_gen(handler, ledger)
    assert gen.propose(generator_input(ctx)) == c.builtin_hypothesis_payload()
    assert gen.last is not None
    assert gen.last.mode is c.ProviderMode.FALLBACK
    assert gen.last.reason_code is reason
    assert gen.last.attempts == attempts
    assert ledger.entries()[0].quantity == attempts


def test_meta_without_key_is_offline_fallback(ctx: EpisodeContext) -> None:
    ledger = MemoryLedger()
    gen = meta_gen(lambda r: meta_ok(), ledger, key=None)
    assert gen.propose(generator_input(ctx)) == c.builtin_hypothesis_payload()
    assert not ledger.entries()
    from research.schemas.interfaces import HypothesisGenerator

    assert isinstance(m.meta_generator(api_key=""), HypothesisGenerator)


def test_agent_told_eligible_ids_after_feedback(ctx: EpisodeContext) -> None:
    ids = f.candidate_ids(ctx.bank)
    script = Script(tool_call("c05", "a"), tool_call("c02", "b"))
    sel = f.FrontierAgent(client(script))
    sel.reset(ctx)
    first = sel.choose(state(ctx))
    guarded = [ids["c02"], ids["c07"]]
    fb = PairFeedback(1, first, Outcome.MATCH, 10, 10, 10)
    assert sel.choose(state(ctx, 2, [fb], guarded)) == ids["c02"].input_sha256
    nudge = script.bodies[1]["input"][-1]
    assert nudge["role"] == "user"
    assert "c02, c07" in nudge["content"]
