"""Shared candidate pool: identical for every arm on a case (protocol ``candidate_pool``)."""

from __future__ import annotations

from typing import Final

from proofhunter import bayes
from proofhunter.contracts import (
    CandidateBank,
    HypothesisPlan,
    HypothesisPlanPayload,
    ProviderMode,
    ProviderName,
    ProviderProvenance,
    builtin_hypothesis_payload,
)
from research.schemas.models import CaseRecord, ProtocolV1

POOL_GENERATOR: Final = "proofhunter.bayes.generate"
POOL_PLAN: Final = "proofhunter.contracts.builtin_hypothesis_payload"
POOL_SEED_SOURCE: Final = "CaseRecord.candidate_seed"


def as_plan(payload: HypothesisPlanPayload, *, mode: ProviderMode) -> HypothesisPlan:
    """Wrap a validated payload as a production plan without provider identifiers."""
    return HypothesisPlan(
        hypotheses=payload.hypotheses,
        provenance=ProviderProvenance(
            provider=ProviderName.META, mode=mode, model_id=None, request_id=None,
            latency_ms=None, attempts=0, reason_code=None,
        ),
    )


def builtin_plan() -> HypothesisPlan:
    return as_plan(builtin_hypothesis_payload(), mode=ProviderMode.FALLBACK)


def check_pool_policy(protocol: ProtocolV1) -> None:
    pool = protocol.candidate_pool
    if (pool.get("generator"), pool.get("plan"), pool.get("seed_source")) != (
        POOL_GENERATOR, POOL_PLAN, POOL_SEED_SOURCE
    ):
        raise ValueError("protocol candidate_pool differs from the implemented pool policy")


def shared_pool(case: CaseRecord, protocol: ProtocolV1) -> CandidateBank:
    """``bayes.generate(builtin plan, case.candidate_seed)``; never depends on the arm."""
    check_pool_policy(protocol)
    bank = bayes.generate(builtin_plan(), case.candidate_seed)
    if len(bank.candidates) > int(protocol.candidate_pool["max_candidates"]):
        raise ValueError("pool exceeds protocol max_candidates")
    if bank.generator_version != protocol.candidate_pool["generator_version"]:
        raise ValueError("pool generator version differs from protocol")
    return bank
