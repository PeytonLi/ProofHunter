from __future__ import annotations

from pathlib import Path

import pytest

from bench.corpus import ROOT, load
from proofhunter.contracts import canonical_sha256, sha256_hex
from research.execution.allowlist import ReviewedSourceAllowlist, from_bench_corpus
from research.schemas.interfaces import CaseSources
from research.schemas.models import CaseRecord, ProtocolV1, Split, Track

SPEC = Path(__file__).resolve().parents[3] / "research" / "spec"


@pytest.fixture(scope="session")
def protocol() -> ProtocolV1:
    return ProtocolV1.model_validate_json((SPEC / "protocol-v1.json").read_text())


@pytest.fixture(scope="session")
def allowlist() -> ReviewedSourceAllowlist:
    return from_bench_corpus()


def case_sources(case_id: str, split: str = "validation") -> CaseSources:
    """Reviewed bench corpus sources wrapped as a research case (execution plumbing only)."""
    case = next(c for c in load(ROOT).cases if c.case_id == case_id)
    base, head = (ROOT / case.base_path).read_bytes(), (ROOT / case.head_path).read_bytes()
    params: dict[str, object] = {}
    record = CaseRecord(
        schema_version="research-case-v1", track=Track.A, case_id=f"bench-{case_id}",
        split=Split(split),
        family_id="bench_plumbing", defect_origin=f"bench:{case_id}",
        near_dup_cluster=f"bench:{case_id}", template_id="bench-corpus",
        template_version="1", template_sha256=sha256_hex(base + head), params=params,
        params_sha256=canonical_sha256(params), contract_id="checkout-rounding@1",
        base_source_sha256=sha256_hex(base), head_source_sha256=sha256_hex(head),
        candidate_seed=1234)
    return CaseSources(case=record, base_source=base, head_source=head)


def request_for(source: bytes, *, timeout: int = 5, side: str = "head", sha: str | None = None):
    """A production ``ExecutionRequest`` for ``source`` bound to the demo enrollment."""
    from fixtures.adapters import SequentialIds
    from fixtures.catalog import demo_enrollment
    from proofhunter.contracts import CandidateInput, ExecutionRequest, ResourcePolicy, Side
    from proofhunter.contracts import input_sha256 as digest

    e, ids = demo_enrollment(), SequentialIds()
    inp = CandidateInput(prices_cents=[199, 1], discount_percent=15)
    return ExecutionRequest(
        run_id=ids.new_run_id(), attempt_id=ids.new_attempt_id(), side=Side(side),
        commit_sha="a" * 40, source_path="checkout.py", source_content=source.decode(),
        source_sha256=sha or sha256_hex(source), input=inp, input_sha256=digest(inp),
        entrypoint=e.entrypoint, resources=ResourcePolicy(execution_timeout_seconds=timeout),
        snapshot_id=e.snapshot_id, image_digest=e.image_digest,
        harness_version=e.harness_version, harness_sha256=e.harness_sha256)


def reviewed(*sources: bytes) -> ReviewedSourceAllowlist:
    return ReviewedSourceAllowlist({sha256_hex(s): s for s in sources}, "test-reviewed")


@pytest.fixture(name="case_sources")
def _case_sources_fixture():
    return case_sources


@pytest.fixture(name="request_for")
def _request_for_fixture():
    return request_for


@pytest.fixture(name="reviewed")
def _reviewed_fixture():
    return reviewed
