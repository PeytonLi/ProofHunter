"""Model x role grid: arm names, Meta chat reviewer/agent, OpenAI generator (MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from proofhunter import contracts as c
from research.baselines import frontier as f
from research.baselines import grid
from research.baselines.config import load_models
from research.baselines.ledger import MemoryLedger
from research.baselines.selectors import fixed_choice
from research.baselines.smoke import generator_input, run_episode
from research.schemas.interfaces import EpisodeContext, HypothesisGenerator, Selector
from research.schemas.models import CostStatus, MeasurementMode
from tests.research.baselines.test_providers import PROVIDERS, Script, message, state

MODELS = ("gpt-6.1-sol", "gpt-6-astra", "muse-spark-1.3", "muse-wonder-1.0")


def chat(content: str | None, finish: str = "stop",
         calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                      "prompt_tokens_details": {"cached_tokens": 200}}}


def chat_tool(candidate_id: str, call_id: str = "call_1") -> dict[str, Any]:
    return chat(None, "tool_calls", [{"id": call_id, "type": "function", "function": {
        "name": "run_pair", "arguments": json.dumps({"candidate_id": candidate_id})}}])


def meta_client(script: Script, ledger: MemoryLedger | None = None,
                model: str = "muse-spark-1.3") -> f.ChatClient:
    return f.ChatClient(model_id=model, api_key="mk-test", ledger=ledger,
                        transport=httpx.MockTransport(script), sleep=lambda _: None,
                        mode=MeasurementMode.OFFLINE)


def test_grid_has_twelve_stable_arms() -> None:
    assert grid.skipped() == {}
    arms = grid.grid_arms()
    assert len(arms) == 12
    assert arms == tuple(f"{r}:{m}" for r in ("generator", "reviewer", "agent") for m in MODELS)
    for arm in arms:
        role, model = grid.parse_arm(arm)
        if role == "generator":
            gen = grid.make_generator(arm, api_key="k")
            assert isinstance(gen, HypothesisGenerator)
            assert gen.generator_id == arm
            assert grid.make_arm_selector(arm, seed=1).arm_id == arm
        else:
            sel = grid.make_arm_selector(arm, seed=1, api_key="k")
            assert isinstance(sel, Selector)
            assert sel.arm_id == arm
            client = sel.client  # type: ignore[attr-defined]
            assert client.provider == ("meta" if model.startswith("muse") else "openai")
    assert grid.canonical("meta_muse_spark_eig") == "generator:muse-spark-1.3"
    with pytest.raises(ValueError, match="unknown grid arm"):
        grid.parse_arm("reviewer:gpt-6-sol")


def test_meta_reviewer_same_prompt_and_schema(ctx: EpisodeContext) -> None:
    ids = f.candidate_ids(ctx.bank)
    meta_script = Script(chat(json.dumps({"ranking": ["c04", "c01"]})))
    oa_script = Script(message(json.dumps({"ranking": ["c04", "c01"]})))
    ledger = MemoryLedger()
    sel = f.FrontierReviewer(meta_client(meta_script, ledger))
    assert sel.arm_id == "reviewer:muse-spark-1.3"
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == ids["c04"].input_sha256
    oa = f.FrontierReviewer(f.ResponsesClient(model_id="gpt-6.1-sol", api_key="k",
                                              transport=httpx.MockTransport(oa_script)))
    oa.reset(ctx)
    oa.choose(state(ctx))
    body, ob = meta_script.bodies[0], oa_script.bodies[0]
    assert body["messages"][0] == {"role": "system", "content": ob["instructions"]}
    assert body["messages"][1] == ob["input"][0]
    assert (body["max_completion_tokens"], body["reasoning_effort"]) == (4096, "low")
    fmt = load_models()["frontier"]["tools"]["reviewer_output"]
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": fmt["name"], "strict": True, "schema": fmt["schema"]}}
    (entry,) = ledger.entries()
    assert entry.pricing_reference is not None
    assert entry.pricing_reference.startswith(
        "cost_basis=user_supplied_price; price_source=user_supplied_meta_console_2026-11-07")
    assert entry.provider == "meta"
    assert entry.cost_status is CostStatus.ESTIMATED
    assert entry.cost_usd == pytest.approx((800 * 1.25 + 200 * 0.15 + 100 * 4.25) / 1e6)


@pytest.mark.parametrize(("response", "reason", "calls"), [
    (httpx.Response(503), "http_503", 3),
    (httpx.ReadTimeout("t"), "provider_timeout", 3),
    (httpx.Response(200, content=b"{not json"), "malformed_response", 1),
    ({"choices": []}, "malformed_response", 1),
    (chat("{}", "length"), "status_incomplete:max_output_tokens", 1),
    (chat("x", "content_filter"), "refusal", 1),
    (chat("not json"), "malformed_output", 1),
])
def test_meta_reviewer_failures_degrade(ctx: EpisodeContext, response: Any, reason: str,
                                        calls: int) -> None:
    script, ledger = Script(response), MemoryLedger()
    sel = f.FrontierReviewer(meta_client(script, ledger))
    sel.reset(ctx)
    assert sel.choose(state(ctx)) == fixed_choice(ctx.bank.candidates)
    assert sel.degraded == reason
    assert len(ledger.entries()) == len(script.bodies) == calls


def test_meta_agent_native_tool_loop(ctx: EpisodeContext) -> None:
    ids = f.candidate_ids(ctx.bank)
    script = Script(chat_tool("c05", "a"), chat_tool("c02", "b"))
    sel = f.FrontierAgent(meta_client(script, model="muse-wonder-1.0"))
    assert sel.arm_id == "agent:muse-wonder-1.0"
    sel.reset(ctx)
    first = sel.choose(state(ctx))
    assert first == ids["c05"].input_sha256
    body = script.bodies[0]
    assert body["tool_choice"] == "auto"
    (tool,) = body["tools"]
    assert tool["function"]["name"] == "run_pair"
    assert tool["function"]["strict"] is True
    from research.schemas.interfaces import PairFeedback
    from research.schemas.models import Outcome

    fb = PairFeedback(1, first, Outcome.MATCH, 10, 10, 10)
    rest = [x for x in ctx.bank.candidates if x.input_sha256 != first]
    assert sel.choose(state(ctx, 2, [fb], rest)) == ids["c02"].input_sha256
    msgs = script.bodies[1]["messages"]
    assert msgs[-3]["tool_calls"][0]["id"] == "a"
    assert msgs[-2]["role"] == "tool"
    assert msgs[-2]["tool_call_id"] == "a"
    assert json.loads(msgs[-2]["content"])["outcome"] == "match"
    assert msgs[-1]["role"] == "user"
    assert "5 paired probes remain" in msgs[-1]["content"]


def test_meta_agent_failures(ctx: EpisodeContext) -> None:
    for response, reason in ((chat("no tools"), "malformed_output"),
                             (chat_tool("c99"), "invalid_tool_calls"),
                             (httpx.Response(429), "http_429")):
        sel = f.FrontierAgent(meta_client(Script(response)))
        sel.reset(ctx)
        assert sel.choose(state(ctx)) == fixed_choice(ctx.bank.candidates)
        assert sel.degraded == reason


def test_meta_agent_full_episode(ctx: EpisodeContext) -> None:
    ids = list(f.candidate_ids(ctx.bank))
    script = Script(*[chat_tool(i, f"k{n}") for n, i in enumerate(ids)])
    row = run_episode(f.FrontierAgent(meta_client(script)), ctx, "fixed")
    assert row["pairs"] == 6
    assert row["first_mismatch_step"] is None


def _plan_text() -> str:
    body = json.loads((PROVIDERS / "meta_response.json").read_text())
    return str(body["choices"][0]["message"]["content"])


def test_openai_generator_uses_production_prompt(ctx: EpisodeContext) -> None:
    script, ledger = Script(message(_plan_text())), MemoryLedger()
    gen = grid.make_generator("generator:gpt-6.1-sol", api_key="k", ledger=ledger,
                              transport=httpx.MockTransport(script))
    assert isinstance(gen, f.ResponsesGenerator)
    gen.client.sleep = lambda _: None
    plan = gen.propose(generator_input(ctx))
    assert gen.degraded is None
    assert plan.hypotheses
    assert plan != c.builtin_hypothesis_payload() or len(plan.hypotheses) > 0
    body = script.bodies[0]
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["input"][0]["role"] == "user"
    assert len(ledger.entries()) == 1
    assert gen.usage.calls == 1


@pytest.mark.parametrize(("response", "reason", "calls"), [
    (httpx.Response(500), "http_500", 2),
    (message("not a plan"), "malformed_output", 1),
])
def test_openai_generator_falls_back(ctx: EpisodeContext, response: Any, reason: str,
                                     calls: int) -> None:
    script = Script(response)
    gen = grid.make_generator("generator:gpt-6-astra", api_key="k",
                              transport=httpx.MockTransport(script))
    assert isinstance(gen, f.ResponsesGenerator)
    gen.client.sleep = lambda _: None
    assert gen.propose(generator_input(ctx)) == c.builtin_hypothesis_payload()
    assert gen.degraded == reason
    assert len(script.bodies) == calls


def test_agent_tool_choice_identical_across_providers(ctx: EpisodeContext) -> None:
    from tests.research.baselines.test_providers import tool_call

    oa, mt = Script(tool_call("c05")), Script(chat_tool("c05"))
    for sel in (f.FrontierAgent(f.ResponsesClient(model_id="gpt-6-astra", api_key="k",
                                                  transport=httpx.MockTransport(oa))),
                f.FrontierAgent(meta_client(mt))):
        sel.reset(ctx)
        sel.choose(state(ctx))
    for body in (oa.bodies[0], mt.bodies[0]):
        assert body["tool_choice"] == "auto"
        assert "parallel_tool_calls" not in body
    assert oa.bodies[0]["instructions"] == mt.bodies[0]["messages"][0]["content"]


def test_unknown_meta_rows_keep_unknown_basis(ctx: EpisodeContext) -> None:
    ledger = MemoryLedger()
    sel = f.FrontierReviewer(meta_client(Script(httpx.Response(400)), ledger))
    sel.reset(ctx)
    sel.choose(state(ctx))
    (entry,) = ledger.entries()
    assert entry.cost_status is CostStatus.UNKNOWN
    assert entry.pricing_reference is not None
    assert entry.pricing_reference.startswith("cost_basis=unknown")


def test_relabel_meta_rows(tmp_path: Any) -> None:
    from research.baselines.ledger import JsonlLedger, model_call_entry
    from research.baselines.smoke import relabel_meta_rows
    from research.schemas.models import LedgerPhase

    path = tmp_path / "ledger.jsonl"
    led = JsonlLedger(path)
    for unit, cost, status in (("tokens", 0.01, CostStatus.ESTIMATED),
                               ("http_attempts", 0.02, CostStatus.ESTIMATED),
                               ("tokens", None, CostStatus.UNKNOWN)):
        led.append(model_call_entry(
            provider="meta", unit=unit, quantity=1.0, cost_usd=cost, cost_status=status,
            pricing_reference="old; model=muse-wonder-1.0; upper bound note",
            pricing_date="2026-11-07", wall_ms=1.0, estimated_job_usd=1.0,
            phase=LedgerPhase.PILOT, mode=MeasurementMode.OFFLINE, run_id=None, case_id=None))
    assert relabel_meta_rows(path) == 2
    refs = [e.pricing_reference or "" for e in led.entries()]
    assert refs[0].startswith("cost_basis=user_supplied_price;")
    assert "model=muse-wonder-1.0" in refs[0]
    assert refs[1].startswith("cost_basis=upper_bound_estimate;")
    assert refs[1].endswith("upper bound note")
    assert refs[2] == "old; model=muse-wonder-1.0; upper bound note"
    assert [e.cost_usd for e in led.entries()] == [0.01, 0.02, None]
