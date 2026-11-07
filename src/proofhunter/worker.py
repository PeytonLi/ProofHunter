"""Single-claim worker and bounded idle polling."""

import asyncio
from contextlib import suppress

from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.pipeline import VerificationPipeline
from proofhunter.ports import LeaseLostError


class VerificationWorker:
    def __init__(self, pipeline: VerificationPipeline, worker_id: str = "worker") -> None:
        self.pipeline = pipeline
        self.worker_id = worker_id

    async def run_once(self) -> bool:
        store = self.pipeline.store
        lease = store.claim(self.worker_id, 240)
        if lease is None:
            return False
        with suppress(LeaseLostError):
            try:
                await self.pipeline.run(store.load_run(lease.run_id), lease)
            except LeaseLostError:
                raise
            except Exception:
                record = store.load_run(lease.run_id)
                receipt = c.ReceiptV1(
                    schema_version=c.SCHEMA_VERSION,
                    run_id=record.run_id,
                    run_kind=record.run_kind,
                    parent_receipt_sha256=record.parent_receipt_sha256,
                    pinned_revision=None,
                    source_file_sha256=None,
                    enrollment_fingerprint=None,
                    execution_mode=c.ExecutionMode.OFFLINE_FIXTURE
                    if self.pipeline.settings.offline
                    else c.ExecutionMode.LIVE,
                    providers={},
                    budget=record.status.budget,
                    seed=record.seed,
                    policy_versions=c.PolicyVersions(),
                    hypotheses=None,
                    probes=[],
                    unpaired_attempt_summaries=[
                        c.UnpairedAttemptSummary(
                            attempt_id=a.attempt_id,
                            side=a.side,
                            input_sha256=a.input_sha256,
                            status=a.status,
                            outcome=a.observation.outcome if a.observation else None,
                            charged=a.charged,
                            reason_code=a.reason_code,
                        )
                        for a in record.attempts
                    ],
                    coverage=oracle.compute_coverage([], False),
                    verdict=None,
                    reason_code=c.ReasonCode.INTERNAL_ERROR,
                    counterexample_probe_no=None,
                    replay=None,
                    created_at=record.status.created_at,
                    completed_at=self.pipeline.clock.now(),
                    receipt_sha256=None,
                )
                store.finish(lease, oracle.seal(receipt), None)
        return True

    async def poll(self, interval: float = 1.0) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(interval)
