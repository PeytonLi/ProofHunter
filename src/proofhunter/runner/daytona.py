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
from daytona.common.errors import DaytonaTimeoutError

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

SANDBOX_FILES = Path(__file__).resolve().parents[3] / "ops" / "sandbox"
OBSERVE_COMMAND = "/usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/wrapper.py"
CANARY_COMMAND = "/usr/bin/sudo -n /usr/bin/python3 -I /opt/proofhunter/canary.py"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}
CANARY_EXPECTED: dict[str, Any] = {
    "egress_denied": True,
    "metadata_denied": True,
    "secrets_absent": True,
    "restricted_child": True,
    "probes_denied": dict.fromkeys(
        (
            "github.com",
            "raw.githubusercontent.com",
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
            "1.1.1.1",
            "app.daytona.io",
            "169.254.169.254",
            "169.254.170.2",
        ),
        True,
    ),
}


def valid_canary(data: Any) -> bool:
    if type(data) is not dict or set(data) != set(CANARY_EXPECTED):
        return False
    probes = data["probes_denied"]
    return (
        all(data[key] is True for key in CANARY_EXPECTED if key != "probes_denied")
        and type(probes) is dict
        and set(probes) == set(CANARY_EXPECTED["probes_denied"])
        and all(value is True for value in probes.values())
    )


def harness_sha256() -> str:
    return sha256_hex(
        b"".join((SANDBOX_FILES / name).read_bytes() for name in ("wrapper.py", "canary.py"))
    )


def strict_json(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


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
        self.harness = harness_sha256()
        self.fingerprint = canonical_sha256(
            {
                "snapshot": snapshot,
                "image_digest": image_digest,
                "harness": self.harness,
                "deployment": deployment,
                "network_block_all": True,
                "public": False,
                "env": ENV,
                "cpu": 1,
                "memory_gib": 1,
                "uid": 65534,
            }
        )
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
            env_vars=ENV.copy(),
            labels={"proofhunter.deployment": self.deployment, "proofhunter.run": run_id},
            volumes=[],
            secrets={},
            auto_stop_interval=1,
            auto_delete_interval=0,
            ttl_minutes=2,
            os_user="daytona",
        )
        sandbox = self.client.create(params, timeout=timeout)
        try:
            self._ledger("created", sandbox.id, run_id)
        except Exception:
            self.client.delete(sandbox, timeout=5, wait=True)
            raise
        return sandbox

    def _policy(self, sandbox: Any) -> bool:
        return bool(
            sandbox.public is False
            and sandbox.network_block_all is True
            and sandbox.snapshot == self.snapshot
            and sandbox.cpu == 1
            and sandbox.memory == 1
        )

    def _delete(self, sandbox: Any, run_id: str) -> None:
        self.client.delete(sandbox, timeout=5, wait=True)
        self._ledger("deleted", sandbox.id, run_id)

    def _approved(self) -> bool:
        self.approval_refusal_reason = "approval_invalid"
        try:
            path = self.state_dir / "runner-isolation-approval.json"
            if path.stat().st_size > 4096:
                return False
            approval = strict_json(path.read_text())
            if type(approval) is not dict:
                return False
            tier = approval.get("daytona_account_tier")
            if type(tier) is not int or tier < 1:
                self.approval_refusal_reason = "account_tier_invalid"
                return False
            if tier < 3:
                self.approval_refusal_reason = "account_tier_insufficient"
                return False
            if "canary_result" not in approval:
                self.approval_refusal_reason = "canary_result_missing"
                return False
            if not valid_canary(approval["canary_result"]):
                self.approval_refusal_reason = "canary_result_not_denied"
                return False
            if (
                approval
                != {
                    "config_fingerprint": self.fingerprint,
                    "image_digest": self.image_digest,
                    "effective_policy_reviewed": True,
                    "daytona_account_tier": tier,
                    "canary_result": CANARY_EXPECTED,
                }
                or approval["effective_policy_reviewed"] is not True
            ):
                return False
            self.approval_refusal_reason = None
            return True
        except (OSError, ValueError):
            return False

    async def attest(self) -> IsolationAttestation:
        self._attestation = await asyncio.to_thread(self._attest)
        return self._attestation

    def _attest(self) -> IsolationAttestation:
        verified = False
        sandbox = None
        try:
            if self.enabled and self._approved():
                sandbox = self._create("preflight", 15)
                if self._policy(sandbox):
                    response = sandbox.process.exec(CANARY_COMMAND, cwd="/", env=ENV, timeout=10)
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
            egress_denied=True if verified else None,
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
                        env=ENV.copy(),
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
