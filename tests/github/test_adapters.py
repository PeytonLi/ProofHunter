"""HTTP-only GitHub acceptance tests. Target source is never imported or executed."""

import base64
import copy
import hashlib
import hmac
import json
import socket
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import httpx
import jwt
import pytest
from pydantic import SecretStr

from fixtures import catalog
from fixtures.adapters import FIXTURE_EPOCH
from proofhunter.config import load_settings
from proofhunter.contracts import (
    CheckConclusion,
    CheckIntent,
    PrTarget,
    PublishStatus,
    ReasonCode,
    git_blob_sha1,
    sha256_hex,
)
from proofhunter.github import (
    create_check_port,
    create_repository_validator,
    create_source_port,
    create_webhook_verifier,
    parse_pr_url,
)
from proofhunter.github.checks import GithubChecks
from proofhunter.github.client import GithubHTTP, GithubSession, InstallationTokens
from proofhunter.github.source import GithubSource
from proofhunter.github.validation import GithubRepositoryValidator
from proofhunter.github.webhooks import GithubWebhookVerifier
from proofhunter.ports import AdapterError, CheckPort, ConfigError, SourcePort

RUN_ID = "12345678-1234-4567-8123-123456789abc"
APP_ID = 1234
OPAQUE_TOKEN = "opaque-installation-token-not-a-jwt"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("real network access forbidden in GitHub tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class HTTPFixture:
    def __init__(self) -> None:
        self.enrollment = catalog.demo_enrollment()
        self.target = PrTarget(
            owner=self.enrollment.owner,
            repo=self.enrollment.repo,
            number=1,
            repo_id=self.enrollment.repo_id,
            installation_id=self.enrollment.installation_id,
        )
        self.prefix = f"/repos/{self.target.full_name}"
        self.pull = json.loads((catalog.PROVIDERS_DIR / "github_pull.json").read_bytes())
        self.checks: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.token_bodies: list[dict[str, Any]] = []
        self.post_count = 0
        self.patch_count = 0
        self.create_mode = "normal"
        self.mutate: Callable[[str, Any], Any] = lambda _path, value: value
        self.associated: list[dict[str, Any]] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url).startswith("https://api.github.com/")
        assert request.headers["x-github-api-version"] == "2026-03-10"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path.startswith("/app/installations/"):
            assert request.method == "POST"
            assert isinstance(body, dict)
            assert request.headers["authorization"] == "Bearer fixture-app-jwt"
            self.token_bodies.append(body)
            value: Any = {
                "token": OPAQUE_TOKEN,
                "expires_at": (FIXTURE_EPOCH + timedelta(hours=1)).isoformat(),
                "repositories": [self.pull["base"]["repo"]],
                "permissions": body["permissions"],
            }
            return httpx.Response(201, json=self.mutate(path, copy.deepcopy(value)))
        assert request.headers["authorization"] == f"Bearer {OPAQUE_TOKEN}"
        if path == f"{self.prefix}/pulls/1":
            value = self.pull
        elif path.endswith("/pulls"):
            value = self.associated if self.associated is not None else [self.pull]
        elif "/git/commits/" in path:
            commit = path.rsplit("/", 1)[1]
            assert commit in {r.commit_sha for r in catalog.revisions().values()}
            value = {"sha": commit, "tree": {"sha": self.tree_sha(commit)}}
        elif "/git/trees/" in path:
            tree_sha = path.rsplit("/", 1)[1]
            rev = next(
                r for r in catalog.revisions().values() if self.tree_sha(r.commit_sha) == tree_sha
            )
            value = {
                "sha": tree_sha,
                "truncated": False,
                "tree": [
                    {
                        "path": "checkout.py",
                        "mode": "100644",
                        "type": "blob",
                        "sha": git_blob_sha1(rev.source_bytes),
                        "size": len(rev.source_bytes),
                    }
                ],
            }
        elif path == f"{self.prefix}/contents/checkout.py":
            commit = request.url.params["ref"]
            rev = next(r for r in catalog.revisions().values() if r.commit_sha == commit)
            value = {
                "path": "checkout.py",
                "name": "checkout.py",
                "type": "file",
                "sha": git_blob_sha1(rev.source_bytes),
                "size": len(rev.source_bytes),
                "content": base64.b64encode(rev.source_bytes).decode(),
                "encoding": "base64",
            }
        elif path.endswith("/check-runs") and request.method == "GET":
            assert request.url.params["filter"] == "all"
            assert request.url.params["app_id"] == str(APP_ID)
            value = {"total_count": len(self.checks), "check_runs": self.checks}
        elif path.endswith("/check-runs") and request.method == "POST":
            assert isinstance(body, dict)
            self.post_count += 1
            assert body["head_sha"] == self.pull["head"]["sha"]
            assert body["external_id"] == RUN_ID
            value = {**body, "id": 88, "app": {"id": APP_ID}}
            if self.create_mode != "lost":
                self.checks.append(value)
            if self.create_mode in {"accepted_timeout", "lost"}:
                raise httpx.ReadTimeout("fixture lost acknowledgement", request=request)
            if self.create_mode == "malformed":
                return httpx.Response(201, json={"id": 88})
            return httpx.Response(201, json=self.mutate(path, copy.deepcopy(value)))
        elif "/check-runs/" in path and request.method == "PATCH":
            assert isinstance(body, dict)
            self.patch_count += 1
            self.checks[0].update(body)
            value = self.checks[0]
        else:
            raise AssertionError(f"unmocked request: {request.method} {path}")
        return httpx.Response(200, json=self.mutate(path, copy.deepcopy(value)))

    @staticmethod
    def tree_sha(commit: str) -> str:
        return hashlib.sha1(f"fixture-tree-{commit}".encode()).hexdigest()


@pytest.fixture
async def wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[HTTPFixture, GithubSession]]:
    def sign(payload: dict[str, Any], key: str, algorithm: str) -> str:
        assert key == "fixture-key"
        assert algorithm == "RS256"
        assert payload["exp"] - payload["iat"] == 600
        assert payload["iss"] == str(APP_ID)
        return "fixture-app-jwt"

    monkeypatch.setattr(jwt, "encode", sign)
    fixture = HTTPFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
        http = GithubHTTP(client)
        tokens = InstallationTokens(
            http, APP_ID, SecretStr("fixture-key"), now=lambda: FIXTURE_EPOCH
        )
        yield fixture, GithubSession(http, tokens)


def source(wire: tuple[HTTPFixture, GithubSession]) -> GithubSource:
    fixture, session = wire
    return GithubSource(
        session, GithubRepositoryValidator([fixture.enrollment]), now=lambda: FIXTURE_EPOCH
    )


def intent(fixture: HTTPFixture) -> CheckIntent:
    return CheckIntent(
        run_id=RUN_ID,
        owner=fixture.target.owner,
        repo=fixture.target.repo,
        repo_id=fixture.target.repo_id,
        installation_id=fixture.target.installation_id,
        head_sha=fixture.pull["head"]["sha"],
        external_id=RUN_ID,
        name="ProofHunter",
        status="completed",
        conclusion=CheckConclusion.FAILURE,
        title="token=do-not-publish",
        summary="untrusted provider body",
        receipt_sha256="a" * 64,
        execution_mode="live",
    )


def checks(wire: tuple[HTTPFixture, GithubSession]) -> GithubChecks:
    wire[1].tokens.checks = True
    return GithubChecks(
        wire[1], app_id=APP_ID, enabled=True, offline=False, now=lambda: FIXTURE_EPOCH
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/pull/1",
        "https://github.com:443/o/r/pull/1",
        "https://u@github.com/o/r/pull/1",
        "https://github.com.evil/o/r/pull/1",
        "https://github.com/o/r/pull/1?x",
        "https://github.com/o/r/pull/1#x",
        "https://github.com/o/r/pull/1?",
        "https://github.com/o/r/pull/1#",
        "https://github.com/o%2fr/r/pull/1",
        "https://github.com/ö/r/pull/1",
        "https://github.com/./r/pull/1",
        "https://github.com/o/../pull/1",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pull/-1",
        "https://github.com/o/r/pull/01",
        "https://github.com/o/r/pull/2147483648",
        "https://github.com/o/r/pull/999999999999999999999999999999999999",
        "https://github.com/o/r/pull/1/",
        "https://github.com/o/r/issues/1",
        "https://github.com/o/r/pull/1\n",
        " https://github.com/o/r/pull/1",
        "https://github.com/o\\r/r/pull/1",
        "https://github.com/o/r/pull/1 https://github.com/o/r/pull/2",
    ],
)
def test_reject_malformed_urls(url: str) -> None:
    with pytest.raises(AdapterError) as caught:
        parse_pr_url(url)
    assert caught.value.code is ReasonCode.INVALID_PR_URL


def test_validator_binds_ids_and_case() -> None:
    e = catalog.demo_enrollment()
    validator = GithubRepositoryValidator([e])
    target, enrollment = validator.resolve(f"https://github.com/{e.repo_full_name.upper()}/pull/1")
    assert target.repo_id == e.repo_id
    assert enrollment == e
    with pytest.raises(AdapterError):
        validator.resolve("https://github.com/elsewhere/repo/pull/1")
    with pytest.raises(AdapterError):
        validator.bind(target.model_copy(update={"installation_id": 1}))
    with pytest.raises(ConfigError):
        GithubRepositoryValidator([e, e])
    with pytest.raises(AdapterError):
        GithubRepositoryValidator([e.model_copy(update={"enabled": False})]).resolve(
            target.canonical_url
        )


async def test_exact_pin_fetch_and_opaque_scoped_token(
    wire: tuple[HTTPFixture, GithubSession],
) -> None:
    f, session = wire
    adapter = source(wire)
    assert isinstance(adapter, SourcePort)
    pin = await adapter.pin(f.target)
    original_sha = pin.head_sha
    f.pull["head"]["sha"] = "f" * 40
    bundle = await adapter.fetch(pin, f.enrollment)
    assert bundle.head.commit_sha == original_sha
    assert bundle.head.content_sha256 == sha256_hex(catalog.revisions()["buggy"].source_bytes)
    assert bundle.context.files[0].text == bundle.head.content
    assert len(bundle.context.canonical_bytes()) < 32768
    assert f.token_bodies == [
        {
            "repository_ids": [f.target.repo_id],
            "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
        }
    ]
    assert await session.tokens.get(f.target) == OPAQUE_TOKEN
    assert len(f.token_bodies) == 1
    assert "download_url" not in " ".join(str(r.url) for r in f.requests)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda p: p["head"]["repo"].update(id=77), ReasonCode.FORK_UNSUPPORTED),
        (lambda p: p["base"]["repo"].update(id=77), ReasonCode.REVISION_UNAVAILABLE),
        (lambda p: p["head"].update(repo=None), ReasonCode.REVISION_UNAVAILABLE),
        (lambda p: p["head"].update(sha="A" * 40), ReasonCode.REVISION_UNAVAILABLE),
        (lambda p: p["base"].update(sha="refs/heads/main"), ReasonCode.REVISION_UNAVAILABLE),
        (lambda p: p["base"]["repo"].update(private=True), ReasonCode.REVISION_UNAVAILABLE),
        (
            lambda p: p["base"]["repo"].update(full_name="evil/repo"),
            ReasonCode.REVISION_UNAVAILABLE,
        ),
        (lambda p: p["head"].update(ref=""), ReasonCode.REVISION_UNAVAILABLE),
        (lambda p: p.update(number=True), ReasonCode.REVISION_UNAVAILABLE),
    ],
)
async def test_reject_untrusted_pin(
    wire: tuple[HTTPFixture, GithubSession], mutation: Callable[[Any], Any], code: ReasonCode
) -> None:
    f, _ = wire
    # Token response stays valid so failures exercise the PR boundary.
    await wire[1].tokens.get(f.target)
    mutation(f.pull)
    with pytest.raises(AdapterError) as caught:
        await source(wire).pin(f.target)
    assert caught.value.code is code


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "120000"),
        ("mode", "100755"),
        ("mode", "160000"),
        ("type", "tree"),
        ("size", 65537),
        ("size", True),
        ("sha", "d" * 40),
        ("path", "other.py"),
    ],
)
async def test_reject_tree_attacks(
    wire: tuple[HTTPFixture, GithubSession], field: str, value: Any
) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)

    def mutate(path: str, data: Any) -> Any:
        if "/git/trees/" in path:
            data["tree"][0][field] = value
        return data

    f.mutate = mutate
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(pin, f.enrollment)
    assert caught.value.code is ReasonCode.SOURCE_INVALID


@pytest.mark.parametrize("kind", ["truncated", "commit_drift", "tree_drift", "duplicate"])
async def test_tree_binding(wire: tuple[HTTPFixture, GithubSession], kind: str) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)

    def mutate(path: str, data: Any) -> Any:
        if "/git/trees/" in path:
            if kind == "truncated":
                data["truncated"] = True
            elif kind == "tree_drift":
                data["sha"] = "e" * 40
            elif kind == "duplicate":
                data["tree"].append(data["tree"][0])
        if kind == "commit_drift" and "/git/commits/" in path:
            data["sha"] = "e" * 40
        return data

    f.mutate = mutate
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(pin, f.enrollment)
    assert caught.value.code is ReasonCode.SOURCE_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha", "d" * 40),
        ("type", "symlink"),
        ("encoding", "none"),
        ("path", "../checkout.py"),
        ("size", 65537),
        ("content", "invalid base64!"),
        ("content", base64.b64encode(b"unexpected checkout bytes").decode()),
        ("submodule_git_url", "https://evil.invalid"),
        ("target", "../elsewhere"),
    ],
)
async def test_content_drift(
    wire: tuple[HTTPFixture, GithubSession], field: str, value: Any
) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)

    def mutate(path: str, data: Any) -> Any:
        if "/contents/" in path:
            data[field] = value
        return data

    f.mutate = mutate
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(pin, f.enrollment)
    assert caught.value.code is ReasonCode.SOURCE_INVALID


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"hello\x00there",
        b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64,
    ],
)
async def test_consistent_but_invalid_source(
    wire: tuple[HTTPFixture, GithubSession], content: bytes
) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)

    def mutate(path: str, data: Any) -> Any:
        if "/git/trees/" in path:
            data["tree"][0].update(size=len(content), sha=git_blob_sha1(content))
        if "/contents/" in path:
            data.update(
                size=len(content),
                sha=git_blob_sha1(content),
                content=base64.b64encode(content).decode(),
            )
        return data

    f.mutate = mutate
    with pytest.raises(AdapterError) as caught:
        await adapter.fetch(pin, f.enrollment)
    assert caught.value.code is ReasonCode.SOURCE_INVALID


@pytest.mark.parametrize("kind", ["too_broad", "permission", "expired", "bad_token", "wrong_repo"])
async def test_reject_invalid_token_scope(
    wire: tuple[HTTPFixture, GithubSession], kind: str
) -> None:
    f, session = wire

    def mutate(path: str, data: Any) -> Any:
        if path.startswith("/app/"):
            if kind == "too_broad":
                data["repositories"].append(data["repositories"][0])
            elif kind == "permission":
                data["permissions"]["contents"] = "write"
            elif kind == "expired":
                data["expires_at"] = FIXTURE_EPOCH.isoformat()
            elif kind == "bad_token":
                data["token"] = "bad\nheader"
            elif kind == "wrong_repo":
                data["repositories"][0]["id"] = 99
        return data

    f.mutate = mutate
    with pytest.raises(AdapterError) as caught:
        await session.tokens.get(f.target)
    assert caught.value.code is ReasonCode.PROVIDER_INVALID_RESPONSE
    assert OPAQUE_TOKEN not in str(caught.value)


async def test_token_expiry_and_repo_cache_isolation(
    wire: tuple[HTTPFixture, GithubSession],
) -> None:
    f, session = wire
    await session.tokens.get(f.target)
    session.tokens.now = lambda: FIXTURE_EPOCH + timedelta(minutes=59, seconds=10)
    with pytest.raises(AdapterError):
        await session.tokens.get(f.target)
    assert len(f.token_bodies) == 2
    with pytest.raises(AdapterError):
        await session.tokens.get(f.target.model_copy(update={"repo_id": 42}))
    assert f.token_bodies[-1]["repository_ids"] == [42]


async def test_check_publish_idempotent_and_redacted(
    wire: tuple[HTTPFixture, GithubSession],
) -> None:
    f, _ = wire
    adapter = checks(wire)
    assert isinstance(adapter, CheckPort)
    first = await adapter.publish(intent(f))
    second = await adapter.publish(intent(f))
    assert first.status is second.status is PublishStatus.PUBLISHED
    assert first.check_run_id == second.check_run_id == 88
    assert f.post_count == 1
    assert f.token_bodies[0]["permissions"]["checks"] == "write"
    body = json.loads(
        next(
            r.content
            for r in f.requests
            if r.method == "POST" and r.url.path.endswith("/check-runs")
        )
    )
    assert "token=" not in json.dumps(body)
    assert "untrusted provider body" not in json.dumps(body)


async def test_stale_head_prevents_check(wire: tuple[HTTPFixture, GithubSession]) -> None:
    f, _ = wire
    request = intent(f)
    f.pull["head"]["sha"] = "b" * 40
    result = await checks(wire).publish(request)
    assert result.status is PublishStatus.STALE_HEAD
    assert f.post_count == 0


@pytest.mark.parametrize("mode", ["accepted_timeout", "malformed", "lost"])
async def test_ambiguous_create_never_blind_retried(
    wire: tuple[HTTPFixture, GithubSession], mode: str
) -> None:
    f, _ = wire
    f.create_mode = mode
    adapter = checks(wire)
    results = [await adapter.publish(intent(f)), await adapter.publish(intent(f))]
    expected = PublishStatus.HELD_AMBIGUOUS if mode == "lost" else PublishStatus.PUBLISHED
    assert all(r.status is expected for r in results)
    assert f.post_count == 1


async def test_check_partial_reconciliation_and_duplicate_hold(
    wire: tuple[HTTPFixture, GithubSession],
) -> None:
    f, _ = wire
    adapter = checks(wire)
    await adapter.publish(intent(f))
    f.checks[0].update(status="in_progress", conclusion=None)
    assert (await adapter.publish(intent(f))).status is PublishStatus.PUBLISHED
    assert f.patch_count == 1
    assert f.post_count == 1
    f.checks.append(copy.deepcopy(f.checks[0]))
    assert (await adapter.publish(intent(f))).status is PublishStatus.HELD_AMBIGUOUS
    assert f.post_count == 1


@pytest.mark.parametrize(
    "associated",
    [
        [],
        [{"number": 1, "state": "closed"}],
        [{"number": 1, "state": "open"}, {"number": 2, "state": "open"}],
    ],
)
async def test_no_unambiguous_current_pr_no_publish(
    wire: tuple[HTTPFixture, GithubSession], associated: list[dict[str, Any]]
) -> None:
    f, _ = wire
    f.associated = associated
    assert (await checks(wire).publish(intent(f))).status is PublishStatus.STALE_HEAD
    assert f.post_count == 0


async def test_factories_default_closed(wire: tuple[HTTPFixture, GithubSession]) -> None:
    f, _ = wire
    settings = load_settings({"PROOFHUNTER_PROFILE": "offline"})
    assert create_repository_validator(settings, [f.enrollment]).resolve(f.target.canonical_url)[0]
    assert not create_webhook_verifier(settings).verify(b"{}", "sha256=" + "0" * 64)
    assert (
        await create_check_port(settings).publish(intent(f))
    ).status is PublishStatus.SKIPPED_OFFLINE
    assert (
        await GithubChecks(None, offline=False).publish(intent(f))
    ).status is PublishStatus.SKIPPED_DISABLED
    with pytest.raises(ConfigError):
        create_source_port(settings, [f.enrollment])
    assert f.requests == []


def webhook() -> tuple[GithubWebhookVerifier, bytes, bytes]:
    fixture = HTTPFixture()
    body = json.dumps(
        {
            "action": "synchronize",
            "number": 1,
            "installation": {"id": fixture.target.installation_id},
            "repository": fixture.pull["base"]["repo"],
            "pull_request": fixture.pull,
        }
    ).encode()
    secret = b"fixture-only-webhook-key"
    return GithubWebhookVerifier(secret, enabled=True), body, secret


def signature(body: bytes, secret: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha1=" + "a" * 40,
        "sha256=" + "a" * 64,
        "sha256=" + "A" * 64,
        "sha256=bad",
        "sha256=" + "0" * 64 + "\n",
    ],
)
def test_wrong_signature_never_parses(header: str | None) -> None:
    verifier, body, _ = webhook()
    assert not verifier.verify(body, header)
    with pytest.raises(AdapterError) as caught:
        verifier.parse("pull_request", "delivery-1", body)
    assert caught.value.code is ReasonCode.INVALID_SIGNATURE


def test_valid_webhook_and_duplicate_fingerprint() -> None:
    verifier, body, secret = webhook()
    assert verifier.verify(body, signature(body, secret))
    first = verifier.parse("pull_request", "delivery-1", body)
    assert first is not None
    assert first.body_sha256 == sha256_hex(body)
    assert verifier.verify(body, signature(body, secret))
    assert verifier.parse("pull_request", "delivery-1", body) == first
    assert verifier.verify(body, signature(body, secret))
    assert verifier.parse("issues", "delivery-1", body) is None
    assert not verifier.verify(body + b" ", signature(body, secret))
    with pytest.raises(AdapterError):
        verifier.parse("pull_request", "bad\n", body)


@pytest.mark.parametrize(
    "kind",
    [
        "sha",
        "number_bool",
        "install_bool",
        "private",
        "fork",
        "repo_mismatch",
        "number_mismatch",
        "malformed",
        "duplicate_keys",
    ],
)
def test_invalid_authenticated_webhook(kind: str) -> None:
    verifier, body, secret = webhook()
    data = json.loads(body)
    if kind == "sha":
        data["pull_request"]["head"]["sha"] = "bad"
    elif kind == "number_bool":
        data["number"] = True
    elif kind == "install_bool":
        data["installation"]["id"] = True
    elif kind == "private":
        data["repository"]["private"] = True
    elif kind == "fork":
        data["pull_request"]["head"]["repo"]["id"] = 42
    elif kind == "repo_mismatch":
        data["pull_request"]["base"]["repo"]["id"] = 42
    elif kind == "number_mismatch":
        data["pull_request"]["number"] = 2
    body = json.dumps(data).encode()
    if kind == "malformed":
        body = b"not JSON"
    elif kind == "duplicate_keys":
        body = body[:-1] + b', "action":"opened"}'
    assert verifier.verify(body, signature(body, secret))
    with pytest.raises(AdapterError):
        verifier.parse("pull_request", "delivery-1", body)


def test_webhook_limits_and_irrelevant_action() -> None:
    verifier, body, secret = webhook()
    body = body.replace(b"synchronize", b"closed")
    assert verifier.verify(body, signature(body, secret))
    assert verifier.parse("pull_request", "delivery-1", body) is None
    huge = b" " * (256 * 1024 + 1)
    assert not verifier.verify(huge, signature(huge, secret))
    with pytest.raises(AdapterError) as caught:
        verifier.parse("pull_request", "delivery-1", huge)
    assert caught.value.code is ReasonCode.PAYLOAD_TOO_LARGE


@pytest.mark.parametrize("status", [301, 302, 403, 404, 429, 500])
async def test_transport_failures_sanitized(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, text="provider secret body", headers={"location": "https://evil.invalid"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError) as caught:
            await GithubHTTP(client).request("GET", "/repos/o/r", "fixture-token")
    assert "secret" not in str(caught.value)
    assert "fixture-token" not in repr(caught.value)
    assert caught.value.retryable == (status in {429, 500})


@pytest.mark.parametrize(
    "body", [b"not JSON", b'{"id":1,"id":2}', b"[" * 10000, b'"' + b"a" * (1024 * 1024) + b'"']
)
async def test_bounded_unambiguous_http_json(body: bytes) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(AdapterError) as caught:
            await GithubHTTP(client).request("GET", "/repos/o/r", "fixture-token")
    assert caught.value.code is ReasonCode.PROVIDER_INVALID_RESPONSE


async def test_enrollment_mismatch_never_fetches(wire: tuple[HTTPFixture, GithubSession]) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)
    count = len(f.requests)
    for wrong in [
        f.enrollment.model_copy(update={"installation_id": 42}),
        f.enrollment.model_copy(update={"repo_full_name": "elsewhere/checkout"}),
        f.enrollment.model_copy(update={"contract_sha256": "0" * 64}),
    ]:
        with pytest.raises(AdapterError) as caught:
            await adapter.fetch(pin, wrong)
        assert caught.value.code is ReasonCode.REPO_NOT_ENROLLED
    assert len(f.requests) == count


@pytest.mark.parametrize("content", [b"# token = fixture-secret\n", ("# " + "é" * 18000).encode()])
async def test_context_withholds_sensitive_data_and_bounds_unicode(
    wire: tuple[HTTPFixture, GithubSession],
    content: bytes,
) -> None:
    f, _ = wire
    adapter = source(wire)
    pin = await adapter.pin(f.target)

    def mutate(path: str, data: Any) -> Any:
        if "/git/trees/" in path:
            data["tree"][0].update(size=len(content), sha=git_blob_sha1(content))
        if "/contents/" in path:
            data.update(
                size=len(content),
                sha=git_blob_sha1(content),
                content=base64.b64encode(content).decode(),
            )
        return data

    f.mutate = mutate
    bundle = await adapter.fetch(pin, f.enrollment)
    assert bundle.head.content.encode() == content
    context = bundle.context
    assert (
        len(context.contract_text.encode())
        + len(context.diff.encode())
        + sum(len(file.text.encode()) for file in context.files)
    ) <= 32768
    if b"token" in content:
        assert context.files == []
        assert context.diff == ""


@pytest.mark.parametrize("kind", ["paginated", "wrong_app", "wrong_head", "wrong_external"])
async def test_reconciliation_identity_and_completeness(
    wire: tuple[HTTPFixture, GithubSession],
    kind: str,
) -> None:
    f, _ = wire
    adapter = checks(wire)
    assert (await adapter.publish(intent(f))).status is PublishStatus.PUBLISHED

    def mutate(path: str, data: Any) -> Any:
        if "/commits/" in path and path.endswith("/check-runs"):
            if kind == "paginated":
                data["total_count"] = 101
            elif kind == "wrong_app":
                data["check_runs"][0]["app"]["id"] = 42
            elif kind == "wrong_head":
                data["check_runs"][0]["head_sha"] = "0" * 40
            elif kind == "wrong_external":
                data["check_runs"].append({**data["check_runs"][0], "id": 99})
        return data

    f.mutate = mutate
    result = await adapter.publish(intent(f))
    assert result.status is PublishStatus.HELD_AMBIGUOUS
    assert f.post_count == 1


async def test_concurrent_publish_serializes_one_create(
    wire: tuple[HTTPFixture, GithubSession],
) -> None:
    import asyncio

    f, _ = wire
    adapter = checks(wire)
    results = await asyncio.gather(*(adapter.publish(intent(f)) for _ in range(5)))
    assert all(r.status is PublishStatus.PUBLISHED for r in results)
    assert f.post_count == 1


def test_signature_authorization_is_single_use_and_bad_verify_revokes() -> None:
    verifier, body, secret = webhook()
    assert verifier.verify(body, signature(body, secret))
    verifier.parse("pull_request", "delivery-1", body)
    with pytest.raises(AdapterError):
        verifier.parse("pull_request", "delivery-2", body)
    assert verifier.verify(body, signature(body, secret))
    assert not verifier.verify(body, "sha256=" + "0" * 64)
    with pytest.raises(AdapterError):
        verifier.parse("pull_request", "delivery-3", body)


def test_deep_webhook_json_rejected_after_authentication() -> None:
    verifier, _, secret = webhook()
    body = b"[" * 10000
    assert verifier.verify(body, signature(body, secret))
    with pytest.raises(AdapterError) as caught:
        verifier.parse("pull_request", "delivery-1", body)
    assert caught.value.code is ReasonCode.INVALID_REQUEST


async def test_real_ephemeral_rsa_app_jwt_and_client_cleanup() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    f = HTTPFixture()

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        payload = jwt.decode(
            token,
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert payload == {
            "iss": str(APP_ID),
            "iat": int(FIXTURE_EPOCH.timestamp()) - 60,
            "exp": int(FIXTURE_EPOCH.timestamp()) + 540,
        }
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "token": "opaque-rsa-fixture",
                "expires_at": (FIXTURE_EPOCH + timedelta(hours=1)).isoformat(),
                "repositories": [f.pull["base"]["repo"]],
                "permissions": body["permissions"],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    http = GithubHTTP(client)
    tokens = InstallationTokens(http, APP_ID, SecretStr(pem), now=lambda: FIXTURE_EPOCH)
    session = GithubSession(http, tokens)
    assert await tokens.get(f.target) == "opaque-rsa-fixture"
    await session.aclose()
    assert client.is_closed


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ConnectError, ReasonCode.PROVIDER_UNAVAILABLE),
        (httpx.ReadTimeout, ReasonCode.PROVIDER_TIMEOUT),
    ],
)
async def test_network_exceptions_are_typed_and_redacted(
    exception: type[httpx.RequestError],
    code: ReasonCode,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception("untrusted transport detail fixture-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError) as caught:
            await GithubHTTP(client).request("GET", "/repos/o/r", "fixture-token")
    assert caught.value.code is code
    assert caught.value.retryable
    assert "fixture-token" not in str(caught.value)
