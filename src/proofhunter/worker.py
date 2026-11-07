"""Single-claim worker and bounded idle polling."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

from pydantic import TypeAdapter

from proofhunter import contracts as c
from proofhunter import oracle
from proofhunter.pipeline import VerificationPipeline
from proofhunter.ports import HeartbeatStore, LeaseLostError


class VerificationWorker:
    def __init__(
        self,
        pipeline: VerificationPipeline,
        worker_id: str | None = None,
        *,
        heartbeat_store: HeartbeatStore | None = None,
        config_fingerprint: str | None = None,
        profile: c.Profile | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.worker_id = TypeAdapter(c.SafeToken).validate_python(
            worker_id or "worker-" + uuid4().hex
        )
        self.heartbeat_store = heartbeat_store
        self.config_fingerprint = config_fingerprint
        self.profile = profile or (
            c.Profile.OFFLINE if pipeline.settings.offline else c.Profile.LIVE
        )
        self._heartbeat_failed = False

    def heartbeat(self) -> None:
        if self.heartbeat_store is None or self.config_fingerprint is None:
            return
        try:
            now = self.pipeline.clock.now()
            self.heartbeat_store.write_heartbeat(
                c.WorkerHeartbeat(
                    worker_id=self.worker_id,
                    profile=self.profile,
                    config_fingerprint=self.config_fingerprint,
                    observed_at=now,
                    expires_at=now + timedelta(seconds=c.HEARTBEAT_TTL_SECONDS),
                )
            )
        except Exception:
            if not self._heartbeat_failed:
                logging.getLogger(__name__).warning("Worker heartbeat write failed")
                self._heartbeat_failed = True

    async def _refresh_heartbeat(self) -> None:
        while True:
            await asyncio.sleep(c.HEARTBEAT_INTERVAL_SECONDS)
            self.heartbeat()

    async def run_once(self) -> bool:
        self.heartbeat()
        task = asyncio.create_task(self._refresh_heartbeat())
        try:
            return await self._run_once()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run_once(self) -> bool:
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
                await asyncio.sleep(min(interval, c.HEARTBEAT_INTERVAL_SECONDS / 2))
