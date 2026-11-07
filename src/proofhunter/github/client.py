"""Fixed-origin bounded HTTP and repository-scoped opaque installation tokens."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from pydantic import SecretStr

from proofhunter.config import GITHUB_API_ORIGIN
from proofhunter.contracts import PrTarget, ReasonCode
from proofhunter.github.validation import failure


def utcnow() -> datetime:
    return datetime.now(UTC)


def obj(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def positive(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class GithubHTTP:
    def __init__(self, client: httpx.AsyncClient, api_version: str = "2026-03-10") -> None:
        self.client = client
        self.api_version = api_version

    async def request(
        self,
        method: str,
        path: str,
        credential: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "?#%\\"):
            raise ValueError("invalid internal GitHub API path")
        try:
            async with (
                asyncio.timeout(15),
                self.client.stream(
                    method,
                    GITHUB_API_ORIGIN + path,
                    params=params,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": self.api_version,
                    },
                    follow_redirects=False,
                    timeout=15,
                ) as response,
            ):
                if response.status_code != (201 if method == "POST" else 200):
                    retryable = response.status_code in {408, 429} or response.status_code >= 500
                    code = (
                        ReasonCode.REVISION_UNAVAILABLE
                        if response.status_code == 404
                        else ReasonCode.PROVIDER_UNAVAILABLE
                    )
                    raise failure(code, "GitHub request rejected", retryable=retryable)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise failure(
                            ReasonCode.PROVIDER_INVALID_RESPONSE, "GitHub response too large"
                        )
                return json.loads(data, object_pairs_hook=unique_object)
        except (httpx.TimeoutException, TimeoutError):
            raise failure(
                ReasonCode.PROVIDER_TIMEOUT, "GitHub request timed out", retryable=True
            ) from None
        except httpx.HTTPError:
            raise failure(
                ReasonCode.PROVIDER_UNAVAILABLE, "GitHub transport unavailable", retryable=True
            ) from None
        except (ValueError, UnicodeError, RecursionError):
            raise failure(ReasonCode.PROVIDER_INVALID_RESPONSE, "invalid GitHub JSON") from None


class InstallationTokens:
    def __init__(
        self,
        http: GithubHTTP,
        app_id: int,
        private_key: SecretStr,
        *,
        checks: bool = False,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.http = http
        self.app_id = app_id
        self._private_key = private_key
        self.checks = checks
        self.now = now
        self._cache: dict[tuple[int, int], tuple[SecretStr, datetime]] = {}
        self._lock = asyncio.Lock()

    async def get(self, target: PrTarget) -> str:
        key = target.installation_id, target.repo_id
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] > self.now() + timedelta(seconds=60):
                return cached[0].get_secret_value()
            permissions = {"contents": "read", "pull_requests": "read", "metadata": "read"}
            if self.checks:
                permissions["checks"] = "write"
            now = self.now()
            try:
                app_jwt = jwt.encode(
                    {
                        "iat": int(now.timestamp()) - 60,
                        "exp": int(now.timestamp()) + 540,
                        "iss": str(self.app_id),
                    },
                    self._private_key.get_secret_value(),
                    algorithm="RS256",
                )
            except (jwt.PyJWTError, ValueError, TypeError):
                raise failure(
                    ReasonCode.PROVIDER_UNAVAILABLE, "GitHub App signing failed"
                ) from None
            data = await self.http.request(
                "POST",
                f"/app/installations/{target.installation_id}/access_tokens",
                app_jwt,
                body={"repository_ids": [target.repo_id], "permissions": permissions},
            )
            try:
                data = obj(data)
                token = data["token"]
                if (
                    not isinstance(token, str)
                    or not token
                    or len(token) > 16384
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
                ):
                    raise ValueError
                expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                if (
                    expires.utcoffset() != timedelta(0)
                    or expires <= now + timedelta(seconds=60)
                    or expires > now + timedelta(hours=1, minutes=1)
                ):
                    raise ValueError
                repos = data["repositories"]
                if (
                    not isinstance(repos, list)
                    or len(repos) != 1
                    or positive(obj(repos[0])["id"]) != target.repo_id
                    or obj(repos[0])["full_name"].lower() != target.full_name.lower()
                    or obj(repos[0])["private"] is not False
                    or obj(data["permissions"]) != permissions
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError, AttributeError):
                raise failure(
                    ReasonCode.PROVIDER_INVALID_RESPONSE,
                    "installation token scope or expiry invalid",
                ) from None
            self._cache[key] = SecretStr(token), expires
            return token


class GithubSession:
    def __init__(self, http: GithubHTTP, tokens: InstallationTokens) -> None:
        self.http = http
        self.tokens = tokens

    async def aclose(self) -> None:
        self.tokens._cache.clear()
        await self.http.client.aclose()

    async def request(
        self,
        target: PrTarget,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        return await self.http.request(
            method, path, await self.tokens.get(target), body=body, params=params
        )
