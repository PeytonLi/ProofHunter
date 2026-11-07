"""``meta_muse_spark_eig`` generator: the production Meta port (muse-spark), Snap disabled.

Uses ``proofhunter.meta.create_meta_port`` read-only. Any adapter error falls back to the built-in
plan exactly as the production pipeline does; the fallback reason is kept on ``last``. A delegating
transport reads token usage from each HTTP attempt for the cost ledger (bodies are not retained).
Arms (both on by default, same settings): ``meta_muse_spark_eig`` (muse-spark-1.3) and
``meta_muse_wonder_eig`` (muse-wonder-1.0).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from pydantic import SecretStr

from fixtures import catalog
from proofhunter import contracts as c
from proofhunter.config import META_DEFAULT_MODEL, MetaSettings
from proofhunter.meta import (
    MAX_COMPLETION_TOKENS,
    REASONING_EFFORT,
    RESPONSE_BYTES,
    MetaError,
    create_meta_port,
)
from research.baselines.config import load_models
from research.baselines.ledger import Usage, model_call_entry
from research.schemas.interfaces import HypothesisGenerator, Ledger
from research.schemas.models import CostStatus, GeneratorInput, LedgerPhase, MeasurementMode

GENERATOR_ID = "meta_muse_spark"
GENERATOR_IDS = {"muse-spark-1.3": GENERATOR_ID, "muse-wonder-1.0": "meta_muse_wonder"}
_HOP_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


@dataclass
class AttemptUsage:
    request_bytes: int
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def complete(self) -> bool:
        return self.prompt_tokens is not None and self.completion_tokens is not None


def _count(value: object) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if ok else None  # type: ignore[return-value]


def parse_usage(body: bytes) -> tuple[int | None, int | None, int | None]:
    """``(prompt, cached, completion)`` tokens from a chat-completions body, ``None`` if absent."""
    try:
        outer = json.loads(body)
    except ValueError:
        return None, None, None
    usage = outer.get("usage") if isinstance(outer, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return _count(usage.get("prompt_tokens")), cached, _count(usage.get("completion_tokens"))


class UsageTransport(httpx.AsyncBaseTransport):
    """Delegates every request and records per-attempt token usage; caps the body it buffers."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self.attempts: list[AttemptUsage] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        usage = AttemptUsage(request_bytes=len(request.content))
        self.attempts.append(usage)
        response = await self._inner.handle_async_request(request)
        raw = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > RESPONSE_BYTES:
                    break
        finally:
            await response.aclose()
        body = bytes(raw)
        usage.prompt_tokens, usage.cached_tokens, usage.completion_tokens = parse_usage(body)
        headers = [(k, v) for k, v in response.headers.multi_items()
                   if k.lower() not in _HOP_HEADERS]
        return httpx.Response(response.status_code, headers=headers, content=body,
                              request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def attempt_cost(usage: AttemptUsage, price: dict[str, float]) -> float:
    """Exact from usage; otherwise an upper bound (request bytes / 3 in, max completion out)."""
    if usage.complete:
        assert usage.prompt_tokens is not None
        assert usage.completion_tokens is not None
        cached = min(usage.cached_tokens or 0, usage.prompt_tokens)
        tokens = ((usage.prompt_tokens - cached) * price["input"]
                  + cached * price["cached_input"] + usage.completion_tokens * price["output"])
    else:
        tokens = (math.ceil(usage.request_bytes / 3) * price["input"]
                  + MAX_COMPLETION_TOKENS * price["output"])
    return tokens / 1_000_000


@dataclass(frozen=True)
class _MetaOnlySettings:
    """Duck-typed ``Settings`` subset read by ``MetaAdapter`` (``offline`` and ``meta``)."""

    meta: MetaSettings
    offline: bool = False


@dataclass
class MetaCall:
    mode: c.ProviderMode
    attempts: int
    latency_ms: int | None
    reason_code: c.ReasonCode | None
    model_id: str


def run_uuid(prompt: GeneratorInput) -> str:
    digest = hashlib.sha256(prompt.model_dump_json().encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def planning_context(prompt: GeneratorInput, run_id: str | None = None) -> c.PlanningContext:
    head = next(f for f in prompt.context_files if f.side == "head")
    return c.PlanningContext(
        run_id=run_id or run_uuid(prompt), contract_id="checkout-rounding@1",
        entrypoint="checkout.total_cents",
        contract_text=catalog.contract_text(),
        files=[c.ContextFile(path="checkout.py", start_line=1, text=head.content)],
        diff=prompt.unified_diff, supported_modes=list(c.HypothesisMode),
        max_hypotheses=c.MAX_PROPOSALS)


USER_SUPPLIED: Final = "user_supplied_price"
UPPER_BOUND: Final = "upper_bound_estimate"


def meta_pricing_reference(model: str, basis: str, note: str = "") -> str:
    """Ledger ``pricing_reference`` for Meta rows: ``cost_basis=<label>; price_source=...``."""
    cfg = load_models()["meta"]
    ref = f"cost_basis={basis}; price_source={cfg['price_source']}; model={model}"
    return f"{ref}; {note}" if note else ref


@dataclass
class MetaGenerator:
    api_key: str | None
    model: str = META_DEFAULT_MODEL
    ledger: Ledger | None = None
    transport: httpx.AsyncBaseTransport | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    phase: LedgerPhase = LedgerPhase.PILOT
    mode: MeasurementMode = MeasurementMode.LIVE
    estimated_job_usd: float = 1.0
    case_id: str | None = None
    generator_id: str = GENERATOR_ID
    calls: list[MetaCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        assert MAX_COMPLETION_TOKENS == 4096
        assert REASONING_EFFORT == "low"
        self._settings: Any = _MetaOnlySettings(meta=MetaSettings(
            api_key=SecretStr(self.api_key) if self.api_key else None, model=self.model))

    @property
    def last(self) -> MetaCall | None:
        return self.calls[-1] if self.calls else None

    def propose(self, prompt: GeneratorInput) -> c.HypothesisPlanPayload:
        context = planning_context(GeneratorInput.model_validate(prompt.model_dump()))
        usage = UsageTransport(self.transport)
        port = create_meta_port(self._settings, transport=usage, sleep=self.sleep)
        start = time.monotonic()
        try:
            plan = asyncio.run(port.propose(context))
        except MetaError as error:
            self._record(usage.attempts, start)
            self.calls.append(MetaCall(c.ProviderMode.FALLBACK, error.attempts, None,
                                       error.code, self.model))
            return c.builtin_hypothesis_payload()
        self._record(usage.attempts, start)
        self.calls.append(MetaCall(c.ProviderMode.LIVE, plan.provenance.attempts,
                                   plan.provenance.latency_ms, None, self.model))
        return c.HypothesisPlanPayload(hypotheses=plan.hypotheses)

    @property
    def degraded(self) -> str | None:
        last = self.last
        return None if last is None or last.reason_code is None else last.reason_code.value

    def _record(self, attempts: list[AttemptUsage], start: float) -> None:
        meta = load_models()["meta"]
        price = meta["pricing_usd_per_1m_tokens"][self.model]
        self.usage.calls += len(attempts)
        self.usage.wall_ms += (time.monotonic() - start) * 1000
        for a in attempts:
            self.usage.input_tokens += a.prompt_tokens or 0
            self.usage.cached_tokens += a.cached_tokens or 0
            self.usage.output_tokens += a.completion_tokens or 0
            self.usage.cost_usd += attempt_cost(a, price)
        if self.ledger is None or not attempts:
            return
        complete = all(a.complete for a in attempts)
        if complete:
            unit, quantity = "tokens", float(sum(
                (a.prompt_tokens or 0) + (a.completion_tokens or 0) for a in attempts))
            note = "usage from responses"
        else:
            unit, quantity = "http_attempts", float(len(attempts))
            bounded = sum(not a.complete for a in attempts)
            note = (f"{bounded}/{len(attempts)} attempts without usage priced at upper bound "
                    "(input=ceil(request_bytes/3), output=max_completion_tokens)")
        status = (CostStatus.MEASURED if complete and price["verified"]
                  else CostStatus.ESTIMATED)
        self.ledger.append(model_call_entry(
            provider="meta", unit=unit, quantity=quantity,
            cost_usd=sum(attempt_cost(a, price) for a in attempts), cost_status=status,
            pricing_reference=meta_pricing_reference(
                self.model, USER_SUPPLIED if complete else UPPER_BOUND, note),
            pricing_date=meta["pricing_date"], wall_ms=(time.monotonic() - start) * 1000,
            estimated_job_usd=self.estimated_job_usd, phase=self.phase, mode=self.mode,
            run_id=None, case_id=self.case_id))


def meta_generator(*, api_key: str | None = None, ledger: Ledger | None = None,
                   transport: httpx.AsyncBaseTransport | None = None,
                   model_id: str | None = None) -> HypothesisGenerator:
    """Production Meta port; ``api_key`` defaults to ``MODEL_API_KEY``. Snap is never called.

    ``model_id`` defaults to muse-spark-1.3; any model in ``models.json`` meta ``arms``
    (muse-wonder-1.0) uses the same adapter settings.
    """
    import os

    cfg = load_models()["meta"]
    model = model_id or cfg["model_id"]
    if model not in cfg["arms"].values():
        raise ValueError(f"unknown Meta model: {model}")
    key = api_key if api_key is not None else os.environ.get("MODEL_API_KEY")
    return MetaGenerator(api_key=key, model=model, ledger=ledger, transport=transport,
                         generator_id=GENERATOR_IDS[model])


def meta_arms() -> dict[str, str]:
    """Selector arm id -> Meta model id, for every enabled Meta arm."""
    return dict(load_models()["meta"]["arms"])
