from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from proofhunter import cli
from proofhunter import contracts as c
from proofhunter.config import ServiceRole, load_settings
from proofhunter.github.client import CallRecord
from proofhunter.pipeline import _GithubCalls, compose
from proofhunter.ports import AdapterError, ConfigError
from proofhunter.worker import VerificationWorker
from tests.integration.test_live_cli import environment, registry, trusted
from tests.integration.test_pipeline import setup_run


def test_compose_wires_github_observer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from proofhunter import github, meta, runner, snap

    registry(tmp_path)
    factory = Mock()
    monkeypatch.setattr(github, "create_source_port", factory)
    monkeypatch.setattr(github, "create_check_port", Mock())
    monkeypatch.setattr(meta, "create_meta_port", Mock())
    monkeypatch.setattr(snap, "create_route_port", Mock())
    monkeypatch.setattr(runner, "create_runner_port", Mock())
    pipeline = compose(cli.live_settings(environment(tmp_path), ServiceRole.WORKER), Mock())
    assert pipeline.github_calls is not None
    assert pipeline.github_calls() == 0
    factory.call_args.kwargs["observer"](None)
    assert pipeline.github_calls() == 1


def test_serve_intake_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    registry(tmp_path)
    for key, value in environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    application = Mock()
    monkeypatch.setattr(cli, "live_app", application)
    monkeypatch.setattr(uvicorn, "run", Mock())
    assert cli.main(["serve", "--profile", "live", "--intake-disabled"]) == 0
    assert not application.call_args.args[0].enable_intake
    assert "runner disabled: isolation_unverified; intake disabled" in capsys.readouterr().out


@pytest.mark.parametrize("failed", [False, True])
async def test_github_usage_actual_attempts(tmp_path: Path, failed: bool) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    counter = _GithubCalls()
    pipeline.github_calls = lambda: counter.total
    counter.observe(None)  # An earlier run's call must not leak into this receipt.
    original = pipeline.source.pin

    async def pin(target: c.PrTarget) -> c.PinnedRevision:
        for status in (201, 200):
            counter.observe(CallRecord("GET", "/redacted", status, 1, None, pipeline.clock.now()))
        if failed:
            counter.observe(CallRecord("GET", "/redacted", None, 1, None, pipeline.clock.now()))
            raise AdapterError("github", c.ReasonCode.PROVIDER_TIMEOUT, "timeout", retryable=False)
        return await original(target)

    pipeline.source = Mock(pin=pin, fetch=pipeline.source.fetch)
    assert await VerificationWorker(pipeline).run_once()
    receipt = store.get_receipt(run_id)
    assert receipt.providers[c.ProviderName.GITHUB].calls == (3 if failed else 2)
    if failed:
        assert receipt.reason_code is c.ReasonCode.PROVIDER_TIMEOUT


async def test_github_usage_no_io(tmp_path: Path) -> None:
    pipeline, store, run_id = setup_run(tmp_path)
    pipeline.github_calls = lambda: 7
    pipeline.source = Mock(
        pin=AsyncMock(
            side_effect=AdapterError(
                "github",
                c.ReasonCode.REVISION_UNAVAILABLE,
                "unavailable",
                retryable=False,
            )
        )
    )
    assert await VerificationWorker(pipeline).run_once()
    assert store.get_receipt(run_id).providers[c.ProviderName.GITHUB].calls == 0


def test_inline_enrollment_atomic_private_file(tmp_path: Path) -> None:
    env = environment(tmp_path)
    raw = c.EnrollmentRegistry(schema_version="1.0", enrollments=[trusted()]).model_dump_json()
    env["PROOFHUNTER_ENROLLMENTS_JSON"] = raw
    settings = cli.live_settings(env, ServiceRole.WORKER)
    assert settings.enrollments_path.stat().st_mode & 0o777 == 0o600
    assert c.EnrollmentRegistry.model_validate_json(
        settings.enrollments_path.read_bytes()
    ).enrollments == [trusted()]
    settings.enrollments_path.chmod(0o644)
    cli.live_settings(env, ServiceRole.API)
    assert settings.enrollments_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".enrollment-*"))


@pytest.mark.parametrize("raw", ["", "{}", '{"schema_version":"1.0","enrollments":[]}'])
def test_inline_enrollment_invalid_preserves_existing(tmp_path: Path, raw: str) -> None:
    registry(tmp_path)
    env = environment(tmp_path)
    path = Path(env["PROOFHUNTER_ENROLLMENTS"])
    before = path.read_bytes()
    env["PROOFHUNTER_ENROLLMENTS_JSON"] = raw
    with pytest.raises((ValueError, ConfigError)):
        cli.live_settings(env, ServiceRole.API)
    assert path.read_bytes() == before


def test_inline_enrollment_refuses_repo_path(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["PROOFHUNTER_ENROLLMENTS"] = str(Path(cli.__file__).parent / "enrollments.json")
    env["PROOFHUNTER_ENROLLMENTS_JSON"] = c.EnrollmentRegistry(
        schema_version="1.0",
        enrollments=[trusted()],
    ).model_dump_json()
    with pytest.raises((ValueError, ConfigError)):
        cli.live_settings(env, ServiceRole.API)


@pytest.mark.parametrize("path", ["/v1/runs", "/v1/runs/", "/v1/runs/opaque/replays"])
def test_intake_disabled_no_store_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    registry(tmp_path)
    env = environment(tmp_path)
    env["PROOFHUNTER_ENABLE_INTAKE"] = "false"
    settings = cli.live_settings(env, ServiceRole.API)
    store = Mock()
    monkeypatch.setattr(cli, "create_store", Mock(return_value=store))
    with TestClient(cli.live_app(settings)) as client:
        response = client.post(path, json={})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "intake_disabled"
        assert client.get("/healthz").status_code == 200
    assert store.mock_calls == []


async def test_supervisor_exits_when_api_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    registry(tmp_path)
    server = Mock(serve=AsyncMock())
    monkeypatch.setattr(uvicorn, "Server", Mock(return_value=server))

    async def poll() -> None:
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="stack component stopped"):
        await cli.supervise(
            load_settings(environment(tmp_path), role=ServiceRole.API), Mock(poll=poll)
        )
