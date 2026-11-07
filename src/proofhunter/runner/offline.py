"""Execute only hash-pinned, reviewed first-party demo files, never request bytes."""

import runpy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from fixtures import catalog
from proofhunter.contracts import (
    ExecutionRequest,
    IsolationAttestation,
    IsolationStatus,
    ObservationOutcome,
    RunnerMode,
    SandboxObservation,
    canonical_sha256,
    sha256_hex,
)

REVIEWED = {
    "base": "67ad174f81050136a42c6ffe92b823d771c8f9ad530cc5dcc731abea160f42ff",
    "fixed": "07de083d13f35e97fd9bc24503b0490bceba65d1b3fbdd62df71cb16e6b1968f",
    "buggy": "69918784e41323fa70c59d353f1acdfdf50cf6ddde098fee2c44b83d9c3e96b8",
    "half_even": "d476b05c347be02059185d834d48b611ca195a1287445cac7952eac33f4ba7d3",
}


class ReviewedFixtureRunner:
    async def attest(self) -> IsolationAttestation:
        e = catalog.demo_enrollment()
        return IsolationAttestation(
            runner_mode=RunnerMode.OFFLINE_FIXTURE,
            status=IsolationStatus.OFFLINE_FIXTURE,
            snapshot_id=e.snapshot_id,
            image_digest=e.image_digest,
            harness_sha256=e.harness_sha256,
            network_block_all=True,
            egress_denied=None,
            metadata_denied=None,
            checked_at=datetime.now(UTC),
            config_fingerprint=canonical_sha256(
                {"runner": "offline_fixture", "snapshot": e.snapshot_id}
            ),
            reason_code=None,
        )

    async def observe(self, request: ExecutionRequest) -> SandboxObservation:
        e = catalog.demo_enrollment()
        value = None
        outcome = ObservationOutcome.DENIED
        for label, digest in REVIEWED.items():
            if (
                request.source_sha256 == digest
                and request.snapshot_id == e.snapshot_id
                and request.image_digest == e.image_digest
                and request.harness_sha256 == e.harness_sha256
                and request.harness_version == e.harness_version
            ):
                revision = catalog.revisions()[label]
                if sha256_hex(revision.source_bytes) != digest:
                    break
                fn = cast(
                    Callable[[list[int], int], int],
                    runpy.run_path(str(revision.path))["total_cents"],
                )
                value = fn(list(request.input.prices_cents), request.input.discount_percent)
                outcome = ObservationOutcome.OK
                break
        return SandboxObservation(
            attempt_id=request.attempt_id,
            side=request.side,
            commit_sha=request.commit_sha,
            source_sha256=request.source_sha256,
            input_sha256=request.input_sha256,
            runner_mode=RunnerMode.OFFLINE_FIXTURE,
            snapshot_id=request.snapshot_id,
            image_digest=request.image_digest,
            harness_sha256=request.harness_sha256,
            sandbox_id=None,
            started_at=datetime.now(UTC),
            duration_ms=0,
            outcome=outcome,
            value=value,
            exit_code=0 if value is not None else None,
            stdout_sha256=None,
            stderr_sha256=None,
            truncated=False,
        )
