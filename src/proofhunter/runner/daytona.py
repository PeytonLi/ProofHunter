"""Stable v0 transport. Every synchronous lifecycle owns its eventual teardown."""

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig
from daytona.common.errors import DaytonaNotFoundError, DaytonaTimeoutError

from proofhunter.config import Settings
from proofhunter.contracts import (
    ExecutionRequest,
    IsolationAttestation,
    IsolationStatus,
    ObservationOutcome,
    ReasonCode,
    RunnerMode,
    SandboxObservation,
    canonical_json,
    canonical_sha256,
    sha256_hex,
)
from proofhunter.runner.approval import (
    CANARY_COMMAND,
    HARNESS_DIR,
    OBSERVE_COMMAND,
    config_fingerprint,
    harness_files,
    harness_sha256,
    local_approval_refusal,
    pinned_image,
    strict_json,
    valid_canary,
)
from proofhunter.runner.approval import (
    CANARY_EXPECTED as CANARY_EXPECTED,
)

DELETION_VERIFY_SECONDS = 30.0
DELETION_POLL_SECONDS = 1.0


class DaytonaRunner:
    def __init__(
        self,
        client: Any,
        *,
        snapshot: str,
        image_digest: str,
        deployment: str,
        state_dir: Path,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self.snapshot = snapshot
        self.image_digest = image_digest
        self.deployment = deployment
        self.state_dir = state_dir
        self.enabled = enabled
        self.files = harness_files()
        self.harness = harness_sha256(self.files)
        self.fingerprint = config_fingerprint(snapshot, image_digest, self.harness, deployment)
        self._attestation: IsolationAttestation | None = None
        self.approval_refusal_reason: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "DaytonaRunner":
        d = settings.daytona
        enabled = bool(
            not settings.offline
            and d.enable_live_runner
            and d.api_key
            and d.snapshot
            and d.image_digest
        )
        client = None
        if enabled:
            assert d.api_key is not None
            client = Daytona(
                DaytonaConfig(
                    api_key=d.api_key.get_secret_value(), api_url=d.api_url, otel_enabled=False
                )
            )
        return cls(
            client,
            snapshot=d.snapshot or "disabled",
            image_digest=d.image_digest or "sha256:" + "0" * 64,
            deployment=canonical_sha256(str(settings.db_path.resolve()))[:24],
            state_dir=settings.db_path.parent,
            enabled=enabled,
        )

    def _ledger(self, event: str, sandbox_id: str, run_id: str) -> None:
        with (self.state_dir / "runner-ledger.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": event,
                        "sandbox_id": sandbox_id,
                        "run_id": run_id,
                        "deployment": self.deployment,
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def _create(self, run_id: str, timeout: int) -> Any:
        name = "proofhunter-" + secrets.token_hex(16)
        self._ledger("allocated", name, run_id)
        params = CreateSandboxFromSnapshotParams(
            name=name,
            snapshot=self.snapshot,
            public=False,
            network_block_all=True,
            env_vars={},
            labels={"proofhunter.deployment": self.deployment, "proofhunter.run": run_id},
            volumes=[],
            secrets={},
            auto_stop_interval=1,
            auto_delete_interval=0,
            ttl_minutes=2,
            ephemeral=True,
        )
        sandbox = self.client.create(params, timeout=timeout)
        try:
            self._ledger("created", sandbox.id, run_id)
        except Exception:
            self.client.delete(sandbox, timeout=5, wait=True)
            raise
        return sandbox

    def _pinned(self) -> bool:
        snapshot = self.client.snapshot.get(self.snapshot)
        return bool(
            snapshot.name == self.snapshot
            and snapshot.image_name == pinned_image(self.image_digest)
            and str(getattr(snapshot.state, "value", snapshot.state)).lower() == "active"
        )

    def _install_harness(self, sandbox: Any, timeout: int) -> None:
        for name, data in self.files.items():
            sandbox.fs.upload_file(data, f"{HARNESS_DIR}/{name}", timeout=timeout)

    def _policy(self, sandbox: Any) -> bool:
        return bool(
            sandbox.public is False
            and sandbox.network_block_all is True
            and sandbox.snapshot == self.snapshot
            and sandbox.cpu == 1
            and sandbox.memory == 1
            and not getattr(sandbox, "env", None)
            and not getattr(sandbox, "volumes", None)
        )

    def _delete(self, sandbox: Any, run_id: str) -> None:
        self.client.delete(sandbox, timeout=5, wait=True)
        self._ledger("deleted", sandbox.id, run_id)
        deadline = time.monotonic() + DELETION_VERIFY_SECONDS
        while True:
            try:
                self.client.get(sandbox.id)
            except DaytonaNotFoundError:
                self._ledger("deletion_verified", sandbox.id, run_id)
                return
            if time.monotonic() >= deadline:
                self._ledger("deletion_unverified", sandbox.id, run_id)
                raise RuntimeError("sandbox deletion unverified")
            time.sleep(DELETION_POLL_SECONDS)

    def _approved(self) -> bool:
        self.approval_refusal_reason = local_approval_refusal(
            self.state_dir, fingerprint=self.fingerprint, image_digest=self.image_digest
        )
        return self.approval_refusal_reason is None

    async def attest(self) -> IsolationAttestation:
        self._attestation = await asyncio.to_thread(self._attest)
        return self._attestation

    def _attest(self) -> IsolationAttestation:
        verified = False
        sandbox = None
        try:
            if self.enabled and self._approved() and self._pinned():
                sandbox = self._create("preflight", 30)
                if self._policy(sandbox):
                    self._install_harness(sandbox, 10)
                    response = sandbox.process.exec(CANARY_COMMAND, cwd="/", timeout=20)
                    if response.exit_code == 0 and len(response.result.encode()) <= 4096:
                        data = strict_json(response.result)
                        verified = valid_canary(data)
        except Exception:
            verified = False
        finally:
            if sandbox is not None:
                try:
                    self._delete(sandbox, "preflight")
                except Exception:
                    verified = False
        return IsolationAttestation(
            runner_mode=RunnerMode.DAYTONA,
            status=IsolationStatus.VERIFIED if verified else IsolationStatus.UNVERIFIED,
            snapshot_id=self.snapshot,
            image_digest=self.image_digest,
            harness_sha256=self.harness,
            network_block_all=True,
            egress_denied=False if verified else None,
            metadata_denied=True if verified else None,
            checked_at=datetime.now(UTC),
            config_fingerprint=self.fingerprint,
            reason_code=None if verified else ReasonCode.ISOLATION_UNVERIFIED,
        )

    async def observe(self, request: ExecutionRequest) -> SandboxObservation:
        return await asyncio.to_thread(self._observe, request)

    def _observe(self, request: ExecutionRequest) -> SandboxObservation:
        started = datetime.now(UTC)
        begin = time.monotonic()
        deadline = begin + request.resources.control_deadline_seconds
        sandbox = None
        outcome = ObservationOutcome.DENIED
        value = None
        exit_code = None
        stdout_hash = None
        truncated = False
        a = self._attestation
        allowed = (
            self.enabled
            and a is not None
            and a.status is IsolationStatus.VERIFIED
            and a.checked_at > started - timedelta(minutes=15)
            and self._approved()
            and request.snapshot_id == self.snapshot
            and request.image_digest == self.image_digest
            and request.harness_sha256 == self.harness
            and request.harness_version == "proofhunter-harness@1"
            and len(request.source_content.encode()) <= 65536
        )

        def remaining() -> int:
            seconds = int(deadline - time.monotonic() - 5)
            if seconds < 1:
                raise TimeoutError
            return seconds

        try:
            if allowed:
                sandbox = self._create(request.run_id, remaining())
                if self._policy(sandbox):
                    self._install_harness(sandbox, remaining())
                    nonce = secrets.token_hex(32)
                    binding = {
                        "attempt_id": request.attempt_id,
                        "side": request.side.value,
                        "commit_sha": request.commit_sha,
                        "source_sha256": request.source_sha256,
                        "input_sha256": request.input_sha256,
                        "nonce": nonce,
                    }
                    payload = canonical_json(
                        {
                            "binding": binding,
                            "input": request.input.model_dump(mode="json"),
                            "resources": request.resources.model_dump(),
                        }
                    )
                    sandbox.fs.upload_file(
                        request.source_content.encode(),
                        "/var/proofhunter/checkout.py",
                        timeout=remaining(),
                    )
                    sandbox.fs.upload_file(
                        payload, "/var/proofhunter/request.json", timeout=remaining()
                    )
                    response = sandbox.process.exec(
                        OBSERVE_COMMAND,
                        cwd="/",
                        timeout=min(remaining(), request.resources.execution_timeout_seconds + 1),
                    )
                    remaining()
                    raw = response.result
                    exit_code = response.exit_code
                    if type(exit_code) is not int:
                        raise ValueError("invalid transport status")
                    truncated = len(raw.encode()) > request.resources.max_output_bytes
                    stdout_hash = sha256_hex(raw.encode()[: request.resources.max_output_bytes])
                    outcome = ObservationOutcome.INVALID
                    if not truncated and exit_code == 0:
                        data = strict_json(raw)
                        if (
                            isinstance(data, dict)
                            and set(data) == {"binding", "outcome", "value", "truncated"}
                            and data["binding"] == binding
                            and type(data["truncated"]) is bool
                        ):
                            status = ObservationOutcome(data["outcome"])
                            v = data["value"]
                            truncated = data["truncated"]
                            if status is ObservationOutcome.OK and not truncated:
                                if type(v) is int and -(2**63) <= v < 2**63:
                                    outcome, value = status, v
                            elif (
                                status
                                in (
                                    ObservationOutcome.TIMEOUT,
                                    ObservationOutcome.NONZERO,
                                    ObservationOutcome.INVALID,
                                )
                                and v is None
                            ):
                                outcome = status
                    elif not truncated:
                        outcome = ObservationOutcome.NONZERO
        except (TimeoutError, DaytonaTimeoutError):
            outcome = ObservationOutcome.TIMEOUT
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            outcome = ObservationOutcome.INVALID
        except Exception:
            outcome = ObservationOutcome.INFRA_ERROR
        finally:
            if sandbox is not None:
                try:
                    self._delete(sandbox, request.run_id)
                except Exception:
                    outcome, value = ObservationOutcome.INFRA_ERROR, None
                    self._attestation = None
        return SandboxObservation(
            attempt_id=request.attempt_id,
            side=request.side,
            commit_sha=request.commit_sha,
            source_sha256=request.source_sha256,
            input_sha256=request.input_sha256,
            runner_mode=RunnerMode.DAYTONA,
            snapshot_id=request.snapshot_id,
            image_digest=request.image_digest,
            harness_sha256=request.harness_sha256,
            sandbox_id=sandbox.id if sandbox is not None else None,
            started_at=started,
            duration_ms=int((time.monotonic() - begin) * 1000),
            outcome=outcome,
            value=value,
            exit_code=exit_code,
            stdout_sha256=stdout_hash,
            stderr_sha256=None,
            truncated=truncated,
        )
