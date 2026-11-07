from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from fixtures.catalog import DEMO_DIR
from fixtures.catalog import demo_enrollment as fixture_enrollment
from proofhunter import cli, oracle
from proofhunter import contracts as c
from proofhunter.config import ServiceRole, Settings, load_settings, mode_config_fingerprint
from proofhunter.ports import ConfigError
from proofhunter.runner import create_runner_port
from proofhunter.worker import VerificationWorker
from tests.integration.test_pipeline import setup_run


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PROOFHUNTER_PROFILE": "live",
        "PROOFHUNTER_API_TOKEN": "api-token",
        "PROOFHUNTER_OPERATOR_TOKEN": "operator-token",
        "MODEL_API_KEY": "model-test",
        "SUBQ_API_KEY": "snap-test",
        "GITHUB_APP_ID": "123",
        "GITHUB_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nYWJj\n-----END PRIVATE KEY-----",
        "PROOFHUNTER_DB_PATH": str(tmp_path / "runs.db"),
        "PROOFHUNTER_ENROLLMENTS": str(tmp_path / "enrollments.json"),
        "DAYTONA_SNAPSHOT": "runner-unprovisioned",
        "DAYTONA_IMAGE_DIGEST": cli.UNPROVISIONED_IMAGE,
    }


def trusted() -> c.Enrollment:
    return fixture_enrollment().model_copy(
        update={
            "repo_id": 1234,
            "installation_id": 5678,
            "repo_full_name": "team/checkout",
            "snapshot_id": cli.UNPROVISIONED_SNAPSHOT,
            "image_digest": cli.UNPROVISIONED_IMAGE,
        }
    )


def registry(tmp_path: Path) -> None:
    (tmp_path / "enrollments.json").write_text(
        c.EnrollmentRegistry(
            schema_version="1.0",
            enrollments=[trusted()],
        ).model_dump_json()
    )


@pytest.mark.parametrize(
    "field",
    [
        "repo_id",
        "installation_id",
        "repo_full_name",
        "snapshot_id",
        "image_digest",
    ],
)
def test_fixture_refused(tmp_path: Path, field: str) -> None:
    settings = load_settings(environment(tmp_path), role=ServiceRole.WORKER)
    enrollment = trusted().model_copy(update={field: getattr(fixture_enrollment(), field)})
    with pytest.raises(ConfigError):
        cli.live_trust_gate(settings, [enrollment])


def test_empty_registry_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        cli.live_trust_gate(load_settings(environment(tmp_path), role=ServiceRole.WORKER), [])


@pytest.mark.parametrize("flag", ["enable_checks", "enable_webhook"])
def test_publication_refused(tmp_path: Path, flag: str) -> None:
    settings = load_settings(environment(tmp_path), role=ServiceRole.WORKER)
    settings = settings.model_copy(
        update={"github": settings.github.model_copy(update={flag: True})}
    )
    with pytest.raises(ConfigError):
        cli.live_trust_gate(settings, [trusted()])


def test_runner_sentinel_refused(tmp_path: Path) -> None:
    settings = load_settings(environment(tmp_path), role=ServiceRole.WORKER)
    cli.live_trust_gate(settings, [trusted()])
    settings = settings.model_copy(
        update={
            "daytona": settings.daytona.model_copy(
                update={"enable_live_runner": True},
            )
        }
    )
    with pytest.raises(ConfigError):
        cli.live_trust_gate(settings, [trusted()])


def test_api_worker_fingerprint_and_live_app(tmp_path: Path) -> None:
    registry(tmp_path)
    env = environment(tmp_path)
    api = cli.live_settings(env, ServiceRole.API)
    worker = cli.live_settings(env, ServiceRole.WORKER)
    assert api.github.private_key is None
    assert worker.api_token is None
    assert mode_config_fingerprint(api, [trusted()]) == mode_config_fingerprint(worker, [trusted()])
    with TestClient(cli.live_app(api)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        response = client.post(
            "/v1/runs",
            json={"pr_url": "https://github.com/team/checkout/pull/1"},
            headers={"Authorization": "Bearer api-token", "Idempotency-Key": "x"},
        )
        assert response.status_code == 202


def test_worker_composition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry(tmp_path)
    settings = cli.live_settings(environment(tmp_path), ServiceRole.WORKER)
    pipeline = Mock(settings=settings)
    factory = Mock(return_value=pipeline)
    monkeypatch.setattr(cli, "compose", factory)
    args = argparse.Namespace(retention_apply=False, retention_dry_run=False, retention_hours=24)
    worker = cli.live_worker(settings, args)
    assert worker.pipeline is pipeline
    assert worker.profile is c.Profile.LIVE
    assert worker.retention_dry_run is None
    factory.assert_called_once()


@pytest.mark.parametrize("provisioned", [False, True])
def test_enroll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provisioned: bool) -> None:
    settings = load_settings(environment(tmp_path), role=ServiceRole.WORKER)
    metadata = AsyncMock(return_value=(1234, 5678, "team/checkout"))
    monkeypatch.setattr(cli, "repository_metadata", metadata)
    args = argparse.Namespace(
        out=tmp_path / "enrolled.json",
        repo_url="https://github.com/team/checkout",
        snapshot_id="proven-snapshot" if provisioned else None,
        image_digest="sha256:" + "a" * 64 if provisioned else None,
        unprovisioned_runner=not provisioned,
    )
    cli.enroll(settings, args)
    assert args.out.stat().st_mode & 0o777 == 0o600
    result = c.EnrollmentRegistry.model_validate_json(args.out.read_bytes()).enrollments[0]
    assert result.snapshot_id == ("proven-snapshot" if provisioned else cli.UNPROVISIONED_SNAPSHOT)
    assert result.contract_id == c.CONTRACT_ID
    assert result.harness_sha256 != fixture_enrollment().harness_sha256
    metadata.assert_awaited_once()


@pytest.mark.parametrize("failure", ["fixture", "inside", "missing", "conflict", "sentinel"])
def test_enroll_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    metadata = AsyncMock()
    monkeypatch.setattr(cli, "repository_metadata", metadata)
    args = argparse.Namespace(
        out=tmp_path / "out.json",
        repo_url="https://github.com/team/checkout",
        snapshot_id="real",
        image_digest="sha256:" + "a" * 64,
        unprovisioned_runner=False,
    )
    if failure == "fixture":
        args.image_digest = cli.FIXTURE_IMAGE
    elif failure == "inside":
        args.out = Path(cli.__file__).parent / "out.json"
    elif failure == "missing":
        args.snapshot_id = None
    elif failure == "conflict":
        args.unprovisioned_runner = True
    else:
        args.image_digest = cli.UNPROVISIONED_IMAGE
    with pytest.raises(ValueError):
        cli.enroll(load_settings(environment(tmp_path), role=ServiceRole.WORKER), args)
    metadata.assert_not_called()


async def test_pin_mismatch_precedes_fetch_and_providers(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.expected_pins = ("a" * 40, "b" * 40)
    source = Mock(wraps=pipeline.source)
    source.pin = AsyncMock(wraps=pipeline.source.pin)
    source.fetch = AsyncMock()
    pipeline.source = source
    pipeline.meta = Mock(propose=AsyncMock())
    assert await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.reason_code is c.ReasonCode.REVISION_UNAVAILABLE
    assert store.load_run(run_id).checkpoint.pinned_revision is not None
    source.fetch.assert_not_called()
    pipeline.meta.propose.assert_not_called()


async def test_disabled_live_runner_never_observes(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.settings = pipeline.settings.model_copy(update={"profile": c.Profile.LIVE})
    runner = create_runner_port(pipeline.settings)
    pipeline.runner = runner
    propose = AsyncMock(wraps=pipeline.meta.propose)
    pipeline.meta = Mock(propose=propose)
    choose = AsyncMock(wraps=pipeline.route.choose)
    pipeline.route = Mock(choose=choose, spec=["choose"])
    observe = AsyncMock()
    runner.observe = observe  # type: ignore[method-assign]
    assert await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.execution_mode is c.ExecutionMode.LIVE
    assert receipt.reason_code is c.ReasonCode.ISOLATION_UNVERIFIED
    assert receipt.budget.used.sandbox_invocations == 0
    assert receipt.pinned_revision is not None
    assert receipt.source_file_sha256 is not None
    propose.assert_awaited_once()
    choose.assert_awaited_once()
    observe.assert_not_awaited()


@pytest.mark.parametrize("apply", [False, True])
def test_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Mock()
    store.purge_expired.return_value = {"receipts": 3}
    monkeypatch.setattr(cli, "create_store", Mock(return_value=store))
    monkeypatch.setenv("PROOFHUNTER_DB_PATH", str(tmp_path / "db"))
    assert cli.main(["retention", *(["--apply"] if apply else [])]) == 0
    assert json.loads(capsys.readouterr().out) == {"receipts": 3}
    assert store.purge_expired.call_args.kwargs == {"dry_run": not apply}


def test_live_error_redaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Settings:
        raise RuntimeError("secret provider body")

    monkeypatch.setattr(cli, "live_settings", fail)
    assert cli.main(["worker", "--profile", "live"]) == 1
    assert capsys.readouterr().out == "live refused: RuntimeError\n"


async def test_supervisor_exits_when_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    registry(tmp_path)
    server = Mock(serve=AsyncMock(side_effect=lambda: asyncio.sleep(100)))

    async def serve() -> None:
        await asyncio.sleep(100)

    server.serve = serve
    monkeypatch.setattr(uvicorn, "Server", Mock(return_value=server))
    with pytest.raises(RuntimeError, match="stack component stopped"):
        await cli.supervise(
            load_settings(environment(tmp_path), role=ServiceRole.API), Mock(poll=AsyncMock())
        )


@pytest.mark.parametrize("isolation", [False, True])
def test_live_replay_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolation: bool,
) -> None:
    receipt = c.ReceiptV1.model_validate_json(
        (DEMO_DIR / "receipts/fixed.seed17.json").read_bytes()
    )

    receipt = oracle.seal(
        receipt.model_copy(
            update={
                "execution_mode": c.ExecutionMode.LIVE,
                "probes": [],
                "coverage": oracle.compute_coverage([], False),
                "verdict": None,
                "reason_code": c.ReasonCode.ISOLATION_UNVERIFIED
                if isolation
                else c.ReasonCode.BUDGET_EXHAUSTED,
            }
        )
    )
    path = tmp_path / "receipt.json"
    path.write_text(receipt.model_dump_json())
    registry(tmp_path)
    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    factory = Mock(side_effect=AssertionError("no composition or provider I/O"))
    monkeypatch.setattr(cli, "compose", factory)
    assert cli.main(["replay", "--profile", "live", "--receipt", str(path)]) == 1
    reason = "isolation_unverified" if isolation else "replay_unavailable"
    assert capsys.readouterr().out == f"live refused: {reason}\n"
    factory.assert_not_called()


async def test_metadata_uses_production_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jwt

    from proofhunter import github

    metadata = {"id": 1234, "full_name": "team/checkout", "private": False, "archived": False}
    session = Mock()
    session.http.request = AsyncMock(
        side_effect=[
            {"id": 5678},
            {"token": "bootstrap-test"},
            metadata,
        ]
    )
    session.request = AsyncMock(return_value=metadata)
    session.aclose = AsyncMock()
    monkeypatch.setattr(github, "_session", Mock(return_value=session))
    monkeypatch.setattr(jwt, "encode", Mock(return_value="app-test"))
    assert await cli.repository_metadata(
        load_settings(environment(tmp_path), role=ServiceRole.WORKER),
        "https://github.com/team/checkout",
    ) == (1234, 5678, "team/checkout")
    assert session.request.call_args.args[0].repo_id == 1234
    session.aclose.assert_awaited_once()


@pytest.mark.parametrize("active", [False, True])
async def test_retention_scheduler_idle_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    pipeline, store, _ = setup_run(tmp_path)
    purge = Mock()
    monkeypatch.setattr(store, "purge_expired", purge)
    worker = VerificationWorker(pipeline, retention_dry_run=True)
    monkeypatch.setattr(worker, "run_once", AsyncMock(return_value=active))
    if active:
        monkeypatch.setattr(
            worker,
            "run_once",
            AsyncMock(
                side_effect=[True, asyncio.CancelledError()],
            ),
        )

    async def stop(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await worker.poll()
    if active:
        purge.assert_not_called()
    else:
        assert purge.call_args.kwargs == {"batch_limit": 500, "dry_run": True}
