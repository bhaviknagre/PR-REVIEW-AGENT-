"""GitHub App auth: app JWT -> short-lived installation tokens, cached in memory only."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import jwt
from pydantic import SecretStr

from pr_review.core.redaction import register_secrets

API = "https://api.github.com"
REFRESH_MARGIN = 300.0  # refresh when < 5 min of life remain
_ATTEMPTS = 3
_BACKOFF = 0.5


class GitHubAuthError(Exception):
    """Base error; messages never contain tokens or JWTs."""


class Unauthorized(GitHubAuthError): ...


class Forbidden(GitHubAuthError): ...


class NotFound(GitHubAuthError): ...


class Unavailable(GitHubAuthError):
    """5xx, timeout or transport failure (after retries)."""


@dataclass(frozen=True)
class InstallationToken:
    _value: str = field(repr=False)
    expires_at: float

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"InstallationToken(token='***', expires_at={self.expires_at})"

    __str__ = __repr__


class GitHubAppAuth:
    def __init__(
        self,
        app_id: int,
        private_key: SecretStr,
        client: httpx.AsyncClient,
        *,
        permissions: dict[str, str] | None = None,  # least privilege, e.g. {"contents": "read"}
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._client = client
        self._permissions = permissions
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[int, InstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)}
        return jwt.encode(claims, self._key.get_secret_value(), algorithm="RS256")

    async def token(self, installation_id: int) -> InstallationToken:
        cached = self._cache.get(installation_id)
        if cached and cached.expires_at - self._clock() > REFRESH_MARGIN:
            return cached
        lock = self._locks.setdefault(installation_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(installation_id)  # another caller may have minted
            if cached and cached.expires_at - self._clock() > REFRESH_MARGIN:
                return cached
            fresh = await self._mint(installation_id)
            self._cache[installation_id] = fresh
            return fresh

    async def _mint(self, installation_id: int) -> InstallationToken:
        resp = await self._post(installation_id)
        data = resp.json()
        value = str(data["token"])
        register_secrets(value)
        expires = datetime.fromisoformat(data["expires_at"]).timestamp()
        return InstallationToken(value, expires)

    async def _post(self, installation_id: int) -> httpx.Response:
        url = f"{API}/app/installations/{installation_id}/access_tokens"
        body = {"permissions": self._permissions} if self._permissions else None
        for attempt in range(_ATTEMPTS):
            headers = {
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
            }
            try:
                resp = await self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                failure: GitHubAuthError = Unavailable(f"github unreachable: {type(exc).__name__}")
            else:
                if resp.status_code == 201:
                    return resp
                failure = _map(resp.status_code)
            if not isinstance(failure, Unavailable) or attempt == _ATTEMPTS - 1:
                raise failure
            await self._sleep(_BACKOFF * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover


def _map(status: int) -> GitHubAuthError:
    if status == 401:
        return Unauthorized("github rejected app credentials (401)")
    if status == 403:
        return Forbidden("github denied token request (403)")
    if status == 404:
        return NotFound("installation not found (404)")
    if status >= 500:
        return Unavailable(f"github server error ({status})")
    return GitHubAuthError(f"unexpected github status {status}")
