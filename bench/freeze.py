"""Reviewed execution allowlist and preregistration, checked before evaluation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from proofhunter.contracts import Sha256Hex, canonical_sha256, sha256_hex

REPO = Path(__file__).resolve().parent.parent
POLICY_PATH = Path(__file__).with_name("policy.json")
SOURCE_PATHS = (
    "bench/arms.py", "bench/run.py", "bench/scoring.py", "bench/harness.py",
    "bench/freeze.py", "bench/corpus/__init__.py", "bench/holdout.py",
    "src/proofhunter/bayes/__init__.py", "src/proofhunter/oracle/__init__.py",
    "src/proofhunter/oracle/rounding.py", "src/proofhunter/oracle/evidence.py",
    "src/proofhunter/oracle/verdict.py", "src/proofhunter/contracts.py",
    "src/proofhunter/ports.py", "fixtures/adapters.py", "fixtures/catalog.py",
    "fixtures/providers/meta_plan_payload.json", "fixtures/demo/contract.json",
    "fixtures/demo/harness.json",
)


class FrozenPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    policy_sha256: Sha256Hex
    manifest_sha256: Sha256Hex
    master_seed: int
    replicate_seeds: list[int]
    max_pairs: int
    max_confirmations: int
    reviewed_first_party: bool
    review_rationale: str
    reviewed_source_sha256: list[Sha256Hex]
    source_sha256: dict[str, Sha256Hex]


def load_policy(path: Path = POLICY_PATH) -> FrozenPolicy:
    policy = FrozenPolicy.model_validate_json(path.read_text())
    if canonical_sha256(policy.model_dump(exclude={"policy_sha256"})) != policy.policy_sha256:
        raise ValueError("benchmark policy hash drift")
    if not policy.reviewed_first_party or not policy.review_rationale.strip():
        raise ValueError("first-party source review is required")
    if set(policy.source_sha256) != set(SOURCE_PATHS) or any(
            sha256_hex((REPO / name).read_bytes()) != digest
            for name, digest in policy.source_sha256.items()):
        raise ValueError("benchmark implementation hash drift; explicit refreeze required")
    return policy
