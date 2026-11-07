"""Explicitly opted-in, scoped SubQ operator smoke; never loads live app settings."""

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr

from proofhunter.bayes import make_candidate
from proofhunter.config import SnapSettings
from proofhunter.contracts import (
    CandidateBank,
    CoverageTag,
    DecisionState,
    HypothesisId,
    Strategy,
    pool_sha256,
)
from proofhunter.snap import SnapRoute, pool_evidence


async def smoke(settings: SnapSettings, decisions: int, *,
                transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Run one listing and at most ``decisions`` actual decision attempts."""
    if not 1 <= decisions <= 3:
        raise ValueError("decisions must be between 1 and 3")
    route = SnapRoute(settings, request_limit=decisions, transport=transport)
    candidates = [make_candidate([price], 10, strategy)
                  for price, strategy in zip((15, 25, 35), Strategy, strict=True)]
    bank = CandidateBank(generator_version=candidates[0].generator_version, seed=17,
                         candidates=candidates, bank_sha256=pool_sha256(candidates))
    config = {"model": settings.model, "url": settings.url, "timeout": 3.0,
              "limit": decisions}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    probe = await route.probe_models()
    results = []
    run_id = str(uuid4())
    for index in range(decisions):
        remaining = decisions - route.requests_spent(run_id)
        if remaining <= 0:
            break
        eligible = tuple(Strategy) if index % 2 == 0 else tuple(Strategy)[:2]
        state = DecisionState(
            run_id=run_id, contract_id="checkout-rounding@1", step=1 if index == 0 else 2,
            remaining_pairs=6-index, snap_requests_remaining=remaining,
            observed_coverage=[], missing_coverage=[CoverageTag.HALF_CENT],
            unseen_candidates={s: 1 for s in eligible}, last_outcome=None,
            belief={HypothesisId.H0: 0.7-index*0.1, HypothesisId.OTHER: 0.3+index*0.1})
        before = len(route.attempts)
        decision = await route.choose(state, eligible)
        results.append({"attempts": route.attempts[before:],
                        "mode": decision.mode, "reason": decision.reason_code,
                        "choice": decision.choice, "eligible": decision.eligible,
                        "probabilities": decision.probabilities, "confidence": decision.confidence,
                        "pools": pool_evidence(bank, decision.choice)})
    return {"utc": datetime.now(UTC).isoformat(), "config": config,
            "config_sha256_prefix": fingerprint, "models": probe, "decisions": results,
            "decision_attempts": route.requests_spent(run_id)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--decisions", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--output", type=Path, default=Path("snap-evidence.json"))
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("refusing network calls without --live")
    key = os.environ.get("SUBQ_API_KEY")
    if not key:
        parser.error("SUBQ_API_KEY is required")
    evidence = asyncio.run(smoke(SnapSettings(api_key=SecretStr(key)), args.decisions))
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
