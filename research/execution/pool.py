"""Shared candidate pool: identical for every arm on a (case, replicate seed)."""

from __future__ import annotations

from proofhunter import bayes
from proofhunter.contracts import (
    CandidateBank,
    HypothesisPlan,
    HypothesisPlanPayload,
    ProviderMode,
    ProviderName,
    ProviderProvenance,
    builtin_hypothesis_payload,
    canonical_sha256,
)
from research.schemas.models import CaseRecord, ProtocolV1


def plan_from_payload(payload: HypothesisPlanPayload, mode: ProviderMode) -> HypothesisPlan:
    return HypothesisPlan(hypotheses=payload.hypotheses, provenance=ProviderProvenance(
        provider=ProviderName.META, mode=mode, model_id=None, request_id=None,
        latency_ms=None, attempts=0 if mode is ProviderMode.NOT_CALLED else 1, reason_code=None))


def builtin_plan() -> HypothesisPlan:
    return plan_from_payload(builtin_hypothesis_payload(), ProviderMode.NOT_CALLED)


def pool_seed(candidate_seed: int, replicate_seed: int) -> int:
    """``CaseRecord.candidate_seed`` combined with the replicate seed (RESEARCH-SPEC 3.2)."""
    digest = canonical_sha256({"candidate_seed": candidate_seed, "replicate_seed": replicate_seed})
    return int(digest[:8], 16)


def shared_pool(case: CaseRecord, protocol: ProtocolV1, replicate_seed: int | None = None
                ) -> CandidateBank:
    """Production generator over the built-in plan; never depends on the arm."""
    if protocol.candidate_pool.get("generator") != "proofhunter.bayes.generate":
        raise ValueError("protocol names an unsupported candidate generator")
    seed = case.candidate_seed if replicate_seed is None else pool_seed(case.candidate_seed,
                                                                         replicate_seed)
    bank = bayes.generate(builtin_plan(), seed)
    if len(bank.candidates) > protocol.candidate_pool.get("max_candidates", 64):
        raise ValueError("pool exceeds protocol max_candidates")
    return bank
