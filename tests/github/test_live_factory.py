"""Live factory wiring, sanitized call evidence and the bounded source smoke (HTTP mocks only)."""

import base64
import hashlib
import json
import socket
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from fixtures import catalog
from fixtures.adapters import FIXTURE_EPOCH
from proofhunter.config import ServiceRole, load_settings
from proofhunter.contracts import ReasonCode, git_blob_sha1
from proofhunter.github import create_check_port, create_source_port
from proofhunter.github.client import (
    CallRecord,
    GithubHTTP,
    GithubSession,
    InstallationTokens,
    route_template,
)
from proofhunter.github.smoke import CallBudget, Case, main_async, run_cases
from proofhunter.github.source import GithubSource
from proofhunter.github.validation import GithubRepositoryValidator
from proofhunter.ports import AdapterError, ConfigError

APP_ID = 4321
REPO_ID = 777000111
INSTALLATION_ID = 888000222
FULL_NAME = "ExampleOrg/Mixed-Case-Demo"
OPAQUE = "opaque-live-factory-fixture-token"
REQUEST_ID = "C0DE:1234:ABCDEF:9876543:6543210F"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("real network access forbidden in GitHub tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def worker_env(**extra: str) -> dict[str, str]:
    return {
        "PROOFHUNTER_PROFILE": "live",
        "MODEL_API_KEY": "fixture-model-key",
        "SUBQ_API_KEY": "fixture-subq-key",
        "GITHUB_APP_ID": str(APP_ID),
        **extra,
    }


def enrollment() -> Any:
    return catalog.demo_enrollment().model_copy(
        update={"repo_id": REPO_ID, "installation_id": INSTALLATION_ID, "repo_full_name": FULL_NAME}
    )


async def test_source_factory_uses_in_memory_pem(pem: str) -> None:
    escaped = pem.replace("\n", "\\n")
    settings = load_settings(worker_env(GITHUB_PRIVATE_KEY=escaped), role=ServiceRole.WORKER)
    source = create_source_port(settings, [enrollment()])
    try:
        tokens = source.session.tokens
        assert tokens._private_key.get_secret_value() == pem.strip()
        assert tokens.app_id == APP_ID
        assert tokens.checks is False
        assert source.session.http.client._trust_env is False
    finally:
        await source.aclose()


async def test_source_factory_path_fallback(pem: str, tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text(pem)
    settings = load_settings(
        worker_env(GITHUB_PRIVATE_KEY_PATH=str(key)), role=ServiceRole.WORKER
    )
    source = create_source_port(settings, [enrollment()])
    try:
        assert source.session.tokens._private_key.get_secret_value() == pem.strip()
    finally:
        await source.aclose()


def test_source_factory_errors_never_echo_path_or_key(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret-location-name.pem"
    settings = load_settings(
        worker_env(GITHUB_PRIVATE_KEY_PATH=str(secret_path)), role=ServiceRole.WORKER
    )
    with pytest.raises(ConfigError) as caught:
        create_source_port(settings, [enrollment()])
    assert "secret-location-name" not in str(caught.value)
    with pytest.raises(ConfigError):
        load_settings(worker_env(GITHUB_PRIVATE_KEY="not a pem"), role=ServiceRole.WORKER)


def test_non_worker_roles_never_receive_github_key(pem: str) -> None:
    env = worker_env(GITHUB_PRIVATE_KEY=pem, PROOFHUNTER_API_TOKEN="a", AGENT_SEED_PHRASE="s")
    settings = load_settings(env, role=ServiceRole.AGENT)
    assert settings.github.private_key is None
    assert settings.github.app_id is None
    with pytest.raises(ConfigError):
        create_source_port(settings, [enrollment()])


async def test_disabled_checks_never_read_key_or_request_checks(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    settings = load_settings(
        worker_env(GITHUB_PRIVATE_KEY_PATH=str(missing)), role=ServiceRole.WORKER
    )
    checks = create_check_port(settings)
    assert checks.session is None
    assert checks.enabled is False


async def test_enabled_checks_use_separate_checks_session(pem: str) -> None:
    settings = load_settings(
        worker_env(GITHUB_PRIVATE_KEY=pem, PROOFHUNTER_ENABLE_GITHUB_CHECKS="true"),
        role=ServiceRole.WORKER,
    )
    checks = create_check_port(settings)
    assert checks.session is not None
    assert checks.session.tokens.checks is True
    await checks.session.aclose()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/app/installations/179675412/access_tokens",
            "/app/installations/{installation_id}/access_tokens",
        ),
        ("/repos/a/b/pulls/12", "/repos/a/b/pulls/{number}"),
        ("/repos/a/b/git/commits/" + "a" * 40, "/repos/a/b/git/commits/{sha}"),
        ("/repos/a/b/git/trees/" + "0" * 40, "/repos/a/b/git/trees/{sha}"),
        ("/repos/a/b/contents/checkout.py", "/repos/a/b/contents/checkout.py"),
    ],
)
def test_route_template_strips_operator_ids(path: str, expected: str) -> None:
    assert route_template(path) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [(REQUEST_ID, REQUEST_ID), ("bad;id<script>", None), ("x" * 5, None), ("", None)],
)
async def test_observer_records_sanitized_calls(header: str, expected: str | None) -> None:
    records: list[CallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("ref") is None
        return httpx.Response(404, headers={"x-github-request-id": header}, json={"secret": 1})

    http = GithubHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      observer=records.append)
    with pytest.raises(AdapterError) as caught:
        await http.request("GET", "/repos/a/b/pulls/3", "credential-not-recorded")
    assert caught.value.code is ReasonCode.REVISION_UNAVAILABLE
    assert len(records) == 1
    record = records[0]
    assert (record.method, record.route, record.status) == ("GET", "/repos/a/b/pulls/{number}", 404)
    assert record.request_id == expected
    assert record.latency_ms >= 0
    assert "credential" not in repr(record)
    assert "secret" not in repr(record)


async def test_observer_records_transport_failure() -> None:
    records: list[CallRecord] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http = GithubHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      observer=records.append)
    with pytest.raises(AdapterError):
        await http.request("GET", "/repos/a/b/pulls/3", "c")
    assert records[0].status is None
    assert records[0].request_id is None


SOURCES = {
    "a" * 40: b"def total_cents(prices_cents, discount_percent):\n    return 0\n\n\n",
    "b" * 40: b"def total_cents(prices_cents, discount_percent):\n    return 1\n\n\n",
}


def github(pull_state: str = "closed") -> Callable[[httpx.Request], httpx.Response]:
    repo = {"id": REPO_ID, "full_name": FULL_NAME, "private": False}
    prefix = f"/repos/{FULL_NAME.lower()}"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-github-request-id": REQUEST_ID}
        path = request.url.path
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            body = json.loads(request.content)
            assert body == {
                "repository_ids": [REPO_ID],
                "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
            }
            return httpx.Response(201, headers=headers, json={
                "token": OPAQUE, "permissions": body["permissions"], "repositories": [repo],
                "expires_at": (FIXTURE_EPOCH + timedelta(hours=1)).isoformat(),
            })
        assert request.headers["authorization"] == f"Bearer {OPAQUE}"
        if path == f"{prefix}/pulls/7":
            return httpx.Response(200, headers=headers, json={
                "number": 7, "state": pull_state, "merged": True,
                "base": {"ref": "demo-base", "sha": "a" * 40, "repo": repo},
                "head": {"ref": "feature", "sha": "b" * 40, "repo": repo},
            })
        for sha, content in SOURCES.items():
            tree = hashlib.sha1(sha.encode()).hexdigest()
            if path == f"{prefix}/git/commits/{sha}":
                commit = {"sha": sha, "tree": {"sha": tree}}
                return httpx.Response(200, headers=headers, json=commit)
            if path == f"{prefix}/git/trees/{tree}":
                return httpx.Response(200, headers=headers, json={
                    "sha": tree, "truncated": False, "tree": [{
                        "path": "checkout.py", "mode": "100644", "type": "blob",
                        "size": len(content), "sha": git_blob_sha1(content)}],
                })
            if path == f"{prefix}/contents/checkout.py" and request.url.params["ref"] == sha:
                return httpx.Response(200, headers=headers, json={
                    "type": "file", "path": "checkout.py", "name": "checkout.py",
                    "encoding": "base64", "sha": git_blob_sha1(content), "size": len(content),
                    "content": base64.b64encode(content).decode(),
                })
        raise AssertionError(f"unexpected request {path}")

    return handler


def smoke_source(pem: str, budget: CallBudget) -> tuple[GithubSource, GithubRepositoryValidator]:
    http = GithubHTTP(httpx.AsyncClient(transport=httpx.MockTransport(github())),
                      observer=budget)
    tokens = InstallationTokens(http, APP_ID, SecretStr(pem), now=lambda: FIXTURE_EPOCH)
    validator = GithubRepositoryValidator([enrollment()])
    return GithubSource(GithubSession(http, tokens), validator), validator


URL = f"https://github.com/{FULL_NAME}/pull/7"


async def test_smoke_pins_original_merged_pr_and_verifies_blobs(pem: str) -> None:
    budget = CallBudget(8)
    source, validator = smoke_source(pem, budget)
    public, private = await run_cases(source, validator, [Case(URL, "a" * 40, "b" * 40)], budget)
    await source.aclose()
    entry = public[0]
    assert entry["result"] == "pass", (entry.get("reason_code"), entry["calls"])
    assert entry["base_sha"] == "a" * 40
    assert entry["head_sha"] == "b" * 40
    for side, sha in (("base", "a" * 40), ("head", "b" * 40)):
        assert entry["files"][side]["content_sha256"] == hashlib.sha256(SOURCES[sha]).hexdigest()
        assert entry["files"][side]["blob_sha"] == git_blob_sha1(SOURCES[sha])
    assert len(entry["calls"]) == 8
    assert all(c["request_id"] == REQUEST_ID and c["status"] in {200, 201} for c in entry["calls"])
    text = json.dumps(public)
    for forbidden in (OPAQUE, str(INSTALLATION_ID), str(REPO_ID), "def total_cents", "BEGIN"):
        assert forbidden not in text
    assert private == [{
        "pr_url": URL.lower(), "repo_full_name": FULL_NAME, "repo_id": REPO_ID,
        "installation_id": INSTALLATION_ID, "enrollment_fingerprint": private[0][
            "enrollment_fingerprint"],
    }]


async def test_smoke_fails_closed_on_unexpected_head(pem: str) -> None:
    budget = CallBudget(8)
    source, validator = smoke_source(pem, budget)
    public, _ = await run_cases(source, validator, [Case(URL, "a" * 40, "c" * 40)], budget)
    await source.aclose()
    assert public[0]["result"] == "fail"
    assert "files" not in public[0]
    assert public[0]["head_matches_expected"] is False
    assert len(public[0]["calls"]) == 2


async def test_smoke_enforces_call_budget(pem: str) -> None:
    budget = CallBudget(3)
    source, validator = smoke_source(pem, budget)
    public, _ = await run_cases(source, validator, [Case(URL, "a" * 40, "b" * 40)], budget)
    await source.aclose()
    assert public[0]["result"] == "fail"
    assert public[0]["reason_code"] == "GitHub smoke call budget exceeded"


async def test_smoke_cli_refuses_offline_and_enabled_checks(pem: str, tmp_path: Path) -> None:
    args = ["--case", URL, "a" * 40, "b" * 40, "--out", str(tmp_path / "o"),
            "--private-out", str(tmp_path / "p")]
    with pytest.raises(ConfigError):
        await main_async(args, {"PROOFHUNTER_PROFILE": "offline"})
    env = worker_env(GITHUB_PRIVATE_KEY=pem, PROOFHUNTER_ENABLE_GITHUB_CHECKS="true")
    with pytest.raises(ConfigError):
        await main_async(args, env)
    assert not (tmp_path / "o").exists()
