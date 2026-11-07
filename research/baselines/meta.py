"""``meta_muse_spark_eig`` generator: the production Meta port (muse-spark), Snap disabled.

Uses ``proofhunter.meta.create_meta_port`` read-only. Any adapter error falls back to the built-in
plan exactly as the production pipeline does; the fallback reason is kept on ``last``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from fixtures import catalog
from proofhunter import contracts as c
from proofhunter.config import META_DEFAULT_MODEL, MetaSettings
from proofhunter.meta import MAX_COMPLETION_TOKENS, REASONING_EFFORT, MetaError, create_meta_port
from research.baselines.config import load_models
from research.baselines.ledger import model_call_entry
from research.schemas.interfaces import HypothesisGenerator, Ledger
from research.schemas.models import CostStatus, GeneratorInput, LedgerPhase, MeasurementMode

GENERATOR_ID = "meta_muse_spark"


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
        port = create_meta_port(self._settings, transport=self.transport, sleep=self.sleep)
        start = time.monotonic()
        try:
            plan = asyncio.run(port.propose(context))
        except MetaError as error:
            self._record(error.attempts, start)
            self.calls.append(MetaCall(c.ProviderMode.FALLBACK, error.attempts, None,
                                       error.code, self.model))
            return c.builtin_hypothesis_payload()
        self._record(plan.provenance.attempts, start)
        self.calls.append(MetaCall(c.ProviderMode.LIVE, plan.provenance.attempts,
                                   plan.provenance.latency_ms, None, self.model))
        return c.HypothesisPlanPayload(hypotheses=plan.hypotheses)

    def _record(self, attempts: int, start: float) -> None:
        if self.ledger is None or attempts == 0:
            return
        meta = load_models()["meta"]
        self.ledger.append(model_call_entry(
            provider="meta", unit="http_attempts", quantity=float(attempts), cost_usd=None,
            cost_status=CostStatus.UNKNOWN, pricing_reference=meta["pricing_reference"],
            pricing_date=meta["pricing_date"], wall_ms=(time.monotonic() - start) * 1000,
            estimated_job_usd=self.estimated_job_usd, phase=self.phase, mode=self.mode,
            run_id=None, case_id=self.case_id))


def meta_generator(*, api_key: str | None = None, ledger: Ledger | None = None,
                   transport: httpx.AsyncBaseTransport | None = None) -> HypothesisGenerator:
    """Production Meta port; ``api_key`` defaults to ``MODEL_API_KEY``. Snap is never called."""
    import os

    key = api_key if api_key is not None else os.environ.get("MODEL_API_KEY")
    return MetaGenerator(api_key=key, model=load_models()["meta"]["model_id"], ledger=ledger,
                         transport=transport)
