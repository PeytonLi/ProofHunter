import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from daytona import CreateSandboxFromSnapshotParams
from daytona.common.errors import DaytonaNotFoundError, DaytonaProcessExecutionTimeoutError

from fixtures import adapters, catalog
from proofhunter import contracts as c
from proofhunter.config import load_settings
from proofhunter.runner import DaytonaRunner, ReviewedFixtureRunner, create_runner_port
from proofhunter.runner import daytona as daytona_module
from proofhunter.runner.approval import AMENDMENT, HARNESS_DIR, pinned_image
from proofhunter.runner.daytona import CANARY_COMMAND, CANARY_EXPECTED, OBSERVE_COMMAND

HARNESS_UPLOADS = {f"{HARNESS_DIR}/wrapper.py", f"{HARNESS_DIR}/canary.py"}


def request(
    label: str = "buggy", *, runner: DaytonaRunner | None = None, prices: list[int] | None = None
) -> c.ExecutionRequest:
    e = catalog.demo_enrollment()
    revision = catalog.revisions()[label]
    data = c.CandidateInput(
        prices_cents=[15, 15, 15] if prices is None else prices, discount_percent=10
    )
    return c.ExecutionRequest(
        run_id=str(uuid4()),
        attempt_id=str(uuid4()),
        side=c.Side.HEAD,
        commit_sha=revision.commit_sha,
        source_path="checkout.py",
        source_content=revision.source_bytes.decode(),
        source_sha256=revision.source_sha256,
        input=data,
        input_sha256=c.input_sha256(data),
        entrypoint="checkout.total_cents",
        resources=c.ResourcePolicy(),
        snapshot_id=runner.snapshot if runner else e.snapshot_id,
        image_digest=runner.image_digest if runner else e.image_digest,
        harness_version=e.harness_version,
        harness_sha256=runner.harness if runner else e.harness_sha256,
    )


class FakeSandbox:
    def __init__(self, client: "FakeSDK", params: CreateSandboxFromSnapshotParams) -> None:
        self.client = client
        self.id = "sandbox-" + str(len(client.created))
        self.snapshot = params.snapshot
        self.public = params.public
        self.network_block_all = params.network_block_all
        self.cpu = 1
        self.memory = 1
        self.fs = self
        self.process = self
        self.uploads: dict[str, bytes] = {}

    def upload_file(self, file: bytes, remote_path: str, timeout: int = 1800) -> None:
        assert 0 < timeout <= 30
        if self.client.fault == "upload":
            raise RuntimeError("fake transport failure")
        self.uploads[remote_path] = file

    def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Any:
        assert cwd == "/"
        assert env is None
        assert command in (CANARY_COMMAND, OBSERVE_COMMAND)
        assert set(self.uploads) >= HARNESS_UPLOADS
        if command == CANARY_COMMAND:
            assert set(self.uploads) == HARNESS_UPLOADS
            result = json.loads(json.dumps(CANARY_EXPECTED))
            if self.client.fault == "canary":
                result["metadata_denied"] = False
            if self.client.fault in result["probes_denied"]:
                result["probes_denied"][self.client.fault] = False
            return SimpleNamespace(exit_code=0, result=json.dumps(result))
        assert timeout is not None
        assert timeout <= 6
        if self.client.fault == "cancel":
            self.client.entered.set()
            self.client.release.wait(timeout=2)
        if self.client.fault == "timeout":
            raise DaytonaProcessExecutionTimeoutError("fake timeout")
        if self.client.fault == "transport":
            raise RuntimeError("fake transport failure")
        payload = json.loads(self.uploads["/var/proofhunter/request.json"])
        envelope = {"binding": payload["binding"], "outcome": "ok", "value": 42, "truncated": False}
        fault = self.client.fault
        if fault in ("attempt_id", "nonce", "side", "source_sha256", "input_sha256", "commit_sha"):
            envelope["binding"][fault] = "spoof"
        if fault == "extra":
            envelope["extra"] = "fake report"
        if fault == "bool":
            envelope["value"] = True
        if fault == "float":
            envelope["value"] = 42.0
        if fault == "overflow":
            envelope["value"] = 2**63
        if fault == "target_timeout":
            envelope.update(outcome="timeout", value=None)
        if fault == "target_nonzero":
            envelope.update(outcome="nonzero", value=None)
        if fault == "child_oversize":
            envelope.update(outcome="invalid", value=None, truncated=True)
        raw = json.dumps(envelope)
        if fault == "empty":
            raw = ""
        if fault == "malformed":
            raw = "not json"
        if fault == "oversize":
            raw = "x" * 16385
        if fault == "duplicate":
            raw = raw[:-1] + ',"value":41}'
        if fault == "two":
            raw += raw
        return SimpleNamespace(exit_code=1 if fault == "nonzero" else 0, result=raw)


class FakeSnapshots:
    def __init__(self, client: "FakeSDK") -> None:
        self.client = client

    def get(self, name: str) -> Any:
        image = pinned_image("sha256:" + "a" * 64)
        if self.client.fault == "pin":
            image = "python:3.12-slim"
        return SimpleNamespace(name=name, image_name=image, state="active")


class FakeSDK:
    def __init__(self) -> None:
        self.snapshot = FakeSnapshots(self)
        self.created: list[FakeSandbox] = []
        self.deleted: list[FakeSandbox] = []
        self.params: list[CreateSandboxFromSnapshotParams] = []
        self.fault = ""
        self.delete_fails = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def create(
        self, params: CreateSandboxFromSnapshotParams, *, timeout: float = 60
    ) -> FakeSandbox:
        assert isinstance(params, CreateSandboxFromSnapshotParams)
        assert params.public is False
        assert params.network_block_all is True
        assert params.volumes == []
        assert params.secrets == {}
        assert params.env_vars == {}
        assert params.ephemeral is True
        assert params.labels
        assert set(params.labels) == {"proofhunter.deployment", "proofhunter.run"}
        assert params.ttl_minutes == 2
        assert params.auto_delete_interval == 0
        assert 0 < timeout <= 30
        self.params.append(params)
        if self.fault == "create":
            raise RuntimeError("ambiguous allocation")
        sandbox = FakeSandbox(self, params)
        if self.fault == "policy":
            sandbox.public = True
        self.created.append(sandbox)
        return sandbox

    def delete(self, sandbox: FakeSandbox, timeout: float = 60, wait: bool = False) -> None:
        assert timeout == 5
        assert wait is True
        self.deleted.append(sandbox)
        if self.delete_fails:
            raise RuntimeError("fake deletion failure")

    def get(self, sandbox_id: str) -> FakeSandbox:
        if self.fault != "linger":
            for sandbox in self.deleted:
                if sandbox.id == sandbox_id:
                    raise DaytonaNotFoundError("gone")
        return next(s for s in self.created if s.id == sandbox_id)


def configured(tmp_path: Path) -> tuple[DaytonaRunner, FakeSDK]:
    sdk = FakeSDK()
    runner = DaytonaRunner(
        sdk,
        snapshot="reviewed-snapshot",
        image_digest="sha256:" + "a" * 64,
        deployment="test-deployment",
        state_dir=tmp_path,
    )
    (tmp_path / "runner-isolation-approval.json").write_text(
        json.dumps(
            {
                "config_fingerprint": runner.fingerprint,
                "image_digest": runner.image_digest,
                "effective_policy_reviewed": True,
                "daytona_account_tier": 1,
                "amendment": AMENDMENT,
                "canary_result": CANARY_EXPECTED,
            }
        )
    )
    return runner, sdk


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("daytona_account_tier", None, "account_tier_invalid"),
        ("daytona_account_tier", True, "account_tier_invalid"),
        ("daytona_account_tier", "3", "account_tier_invalid"),
        ("daytona_account_tier", 3.0, "account_tier_invalid"),
        ("daytona_account_tier", 0, "account_tier_invalid"),
        ("amendment", None, "account_tier_insufficient"),
        ("amendment", "PRD-A0", "account_tier_insufficient"),
        ("canary_result", None, "canary_result_missing"),
        ("canary_result", {}, "canary_result_not_denied"),
        ("canary_result", "secret-like hostile input", "canary_result_not_denied"),
        ("effective_policy_reviewed", 1, "approval_invalid"),
    ],
)
async def test_approval_refusals(tmp_path: Path, field: str, value: Any, reason: str) -> None:
    runner, sdk = configured(tmp_path)
    path = tmp_path / "runner-isolation-approval.json"
    approval = json.loads(path.read_text())
    if value is None:
        del approval[field]
    else:
        approval[field] = value
    path.write_text(json.dumps(approval))
    assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    assert runner.approval_refusal_reason == reason
    assert (await runner.observe(request(runner=runner))).outcome is c.ObservationOutcome.DENIED
    assert not sdk.created


@pytest.mark.parametrize("host", list(CANARY_EXPECTED["probes_denied"]))
@pytest.mark.parametrize("value", [False, 1, None])
async def test_each_recorded_probe_required(tmp_path: Path, host: str, value: Any) -> None:
    runner, sdk = configured(tmp_path)
    path = tmp_path / "runner-isolation-approval.json"
    approval = json.loads(path.read_text())
    probes = approval["canary_result"]["probes_denied"]
    if value is None:
        del probes[host]
    else:
        probes[host] = value
    path.write_text(json.dumps(approval))
    assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    assert runner.approval_refusal_reason == "canary_result_not_denied"
    assert not sdk.created


async def test_approval_revocation_after_attestation(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    assert (await runner.attest()).status is c.IsolationStatus.VERIFIED
    path = tmp_path / "runner-isolation-approval.json"
    approval = json.loads(path.read_text())
    del approval["amendment"]
    path.write_text(json.dumps(approval))
    assert (await runner.observe(request(runner=runner))).outcome is c.ObservationOutcome.DENIED
    assert runner.approval_refusal_reason == "account_tier_insufficient"
    assert len(sdk.created) == 1


@pytest.mark.parametrize("host", list(CANARY_EXPECTED["probes_denied"]))
async def test_live_probe_failure_overrides_approval(tmp_path: Path, host: str) -> None:
    runner, sdk = configured(tmp_path)
    sdk.fault = host
    assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    assert sdk.created == sdk.deleted
    assert not any(set(s.uploads) - HARNESS_UPLOADS for s in sdk.created)


async def test_fresh_instances_and_no_oracle_payload(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    assert (await runner.attest()).status is c.IsolationStatus.VERIFIED
    req = request(runner=runner)
    one = await runner.observe(req)
    two = await runner.observe(req.model_copy(update={"attempt_id": str(uuid4())}))
    assert one.usable
    assert one.value == two.value == 42
    assert one.sandbox_id != two.sandbox_id
    assert sdk.created == sdk.deleted
    assert len(sdk.created) == 3
    payload = json.loads(sdk.created[1].uploads["/var/proofhunter/request.json"])
    assert set(payload) == {"binding", "input", "resources"}
    assert set(payload["input"]) == {"prices_cents", "discount_percent"}
    assert set(sdk.created[1].uploads) == {
        "/var/proofhunter/request.json",
        "/var/proofhunter/checkout.py",
        *HARNESS_UPLOADS,
    }
    harness = {name.rsplit("/", 1)[1]: data for name, data in sdk.created[1].uploads.items()}
    assert runner.harness == daytona_module.harness_sha256(harness)
    rows = [json.loads(x) for x in (tmp_path / "runner-ledger.jsonl").read_text().splitlines()]
    verified = {r["sandbox_id"] for r in rows if r["event"] == "deletion_verified"}
    assert verified == {s.id for s in sdk.created}
    assert one.source_sha256 == req.source_sha256
    assert one.stdout_sha256


@pytest.mark.parametrize(
    "fault",
    [
        "malformed",
        "empty",
        "oversize",
        "bool",
        "float",
        "overflow",
        "attempt_id",
        "nonce",
        "side",
        "source_sha256",
        "commit_sha",
        "input_sha256",
        "extra",
        "duplicate",
        "two",
        "child_oversize",
    ],
)
async def test_hostile_output(tmp_path: Path, fault: str) -> None:
    runner, sdk = configured(tmp_path)
    await runner.attest()
    sdk.fault = fault
    observed = await runner.observe(request(runner=runner))
    assert observed.outcome is c.ObservationOutcome.INVALID
    assert observed.value is None
    assert observed.truncated is (fault in ("oversize", "child_oversize"))
    assert sdk.created == sdk.deleted


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("timeout", c.ObservationOutcome.TIMEOUT),
        ("target_timeout", c.ObservationOutcome.TIMEOUT),
        ("nonzero", c.ObservationOutcome.NONZERO),
        ("target_nonzero", c.ObservationOutcome.NONZERO),
        ("transport", c.ObservationOutcome.INFRA_ERROR),
        ("upload", c.ObservationOutcome.INFRA_ERROR),
        ("policy", c.ObservationOutcome.DENIED),
        ("create", c.ObservationOutcome.INFRA_ERROR),
    ],
)
async def test_failure_and_teardown(
    tmp_path: Path, fault: str, expected: c.ObservationOutcome
) -> None:
    runner, sdk = configured(tmp_path)
    await runner.attest()
    sdk.fault = fault
    observed = await runner.observe(request(runner=runner))
    assert observed.outcome is expected
    assert observed.value is None
    assert sdk.created == sdk.deleted
    if fault == "create":
        rows = [
            json.loads(line) for line in (tmp_path / "runner-ledger.jsonl").read_text().splitlines()
        ]
        assert rows[-1]["event"] == "allocated"


async def test_delete_failure_invalidates_evidence(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    await runner.attest()
    sdk.delete_fails = True
    observed = await runner.observe(request(runner=runner))
    assert observed.outcome is c.ObservationOutcome.INFRA_ERROR
    assert observed.value is None
    assert (await runner.observe(request(runner=runner))).outcome is c.ObservationOutcome.DENIED
    assert sdk.created == sdk.deleted


@pytest.mark.parametrize("reason", ["no_attest", "no_approval", "canary", "policy", "delete"])
async def test_isolation_denies_before_source(tmp_path: Path, reason: str) -> None:
    runner, sdk = configured(tmp_path)
    if reason == "no_approval":
        (tmp_path / "runner-isolation-approval.json").unlink()
    if reason in ("canary", "policy"):
        sdk.fault = reason
    if reason == "delete":
        sdk.delete_fails = True
    if reason != "no_attest":
        assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    observed = await runner.observe(request(runner=runner))
    assert observed.outcome is c.ObservationOutcome.DENIED
    assert not any(set(s.uploads) - HARNESS_UPLOADS for s in sdk.created)


async def test_provenance_mismatch_and_deadline(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    await runner.attest()
    req = request(runner=runner)
    for field, value in (
        ("snapshot_id", "other"),
        ("image_digest", "sha256:" + "b" * 64),
        ("harness_sha256", "b" * 64),
    ):
        result = await runner.observe(req.model_copy(update={field: value}))
        assert result.outcome is c.ObservationOutcome.DENIED
    assert len(sdk.created) == 1
    short = req.model_copy(update={"resources": c.ResourcePolicy(control_deadline_seconds=1)})
    assert (await runner.observe(short)).outcome is c.ObservationOutcome.TIMEOUT
    assert len(sdk.created) == 1


async def test_cancellation_retains_teardown(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    await runner.attest()
    sdk.fault = "cancel"
    task = asyncio.create_task(runner.observe(request(runner=runner)))
    assert await asyncio.to_thread(sdk.entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sdk.release.set()
    # A queue barrier on the SDK thread is not available; signal confirmed deletion.
    await asyncio.to_thread(wait_deleted, sdk)
    assert sdk.created == sdk.deleted


def wait_deleted(sdk: FakeSDK) -> None:
    import time

    deadline = time.monotonic() + 2
    while len(sdk.deleted) != len(sdk.created) and time.monotonic() < deadline:
        threading.Event().wait(0.01)


@pytest.mark.parametrize(
    ("label", "expected"), [("base", 41), ("buggy", 42), ("fixed", 41), ("half_even", 40)]
)
async def test_reviewed_offline(label: str, expected: int) -> None:
    runner = create_runner_port(load_settings({"PROOFHUNTER_PROFILE": "offline"}))
    assert isinstance(runner, ReviewedFixtureRunner)
    assert (await runner.attest()).status is c.IsolationStatus.OFFLINE_FIXTURE
    req = request(label)
    observed = await runner.observe(req)
    assert observed.value == expected
    assert observed.runner_mode is c.RunnerMode.OFFLINE_FIXTURE
    assert observed.value == (await adapters.FixtureRunner().observe(req)).value
    assert (await runner.observe(request(label, prices=[]))).value == 0


async def test_unknown_bytes_denied_offline() -> None:
    req = request()
    source = "raise RuntimeError('never execute this')"
    req = c.ExecutionRequest.model_validate(
        req.model_dump()
        | {
            "source_content": source,
            "source_sha256": c.sha256_hex(source.encode()),
        }
    )
    assert (await ReviewedFixtureRunner().observe(req)).outcome is c.ObservationOutcome.DENIED


def test_disabled_factory_does_not_construct_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SDK must not be constructed")

    monkeypatch.setattr("proofhunter.runner.daytona.Daytona", forbidden)
    settings = load_settings({"PROOFHUNTER_PROFILE": "offline"})
    create_runner_port(settings)
    runner = DaytonaRunner.from_settings(settings)
    assert not runner.enabled
    assert runner.client is None


async def test_unpinned_snapshot_refused_before_allocation(tmp_path: Path) -> None:
    runner, sdk = configured(tmp_path)
    sdk.fault = "pin"
    assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    assert not sdk.created


async def test_lingering_sandbox_fails_deletion_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daytona_module, "DELETION_VERIFY_SECONDS", 0.05)
    monkeypatch.setattr(daytona_module, "DELETION_POLL_SECONDS", 0.01)
    runner, sdk = configured(tmp_path)
    sdk.fault = "linger"
    assert (await runner.attest()).status is c.IsolationStatus.UNVERIFIED
    rows = [json.loads(x) for x in (tmp_path / "runner-ledger.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "deletion_unverified"
    assert (await runner.observe(request(runner=runner))).outcome is c.ObservationOutcome.DENIED


async def test_attestation_reports_restricted_egress(tmp_path: Path) -> None:
    runner, _ = configured(tmp_path)
    attestation = await runner.attest()
    assert attestation.status is c.IsolationStatus.VERIFIED
    assert attestation.egress_denied is False
    assert attestation.metadata_denied is True
