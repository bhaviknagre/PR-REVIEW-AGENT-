"""GitHub REST client: PR metadata, head SHA, unified diff and file list (FR-5, NFR-9)."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from pr_review.connectors.base import PullRequestRef
from pr_review.connectors.github.auth import (
    API,
    Forbidden,
    GitHubAppAuth,
    GitHubAuthError,
    NotFound,
    Unauthorized,
    Unavailable,
)

API_VERSION = "2022-11-28"
USER_AGENT = "pr-review/0.1"
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
MAX_DIFF_BYTES = 2_000_000
MAX_FILES = 300
MAX_RATE_WAIT = 60.0  # longest we will sleep for a rate limit before raising
_ATTEMPTS = 3
_BACKOFF = 0.5
_PER_PAGE = 100


class RateLimited(GitHubAuthError):
    """Rate limit hit and the required wait exceeds MAX_RATE_WAIT (or attempts ran out)."""


class DiffTooLarge(Exception):
    """Diff or file list exceeds the configured limit; callers degrade explicitly (NFR-10)."""


class PullRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    installation_id: int
    repo: str
    number: int
    title: str
    author: str
    draft: bool
    base_sha: str
    base_ref: str
    head_sha: str
    head_ref: str
    changed_files: int
    additions: int
    deletions: int


async def _read(resp: httpx.Response, limit: int | None) -> bytes:
    """Read the body, aborting as soon as it exceeds `limit` (never fully buffering it)."""
    if limit is None:
        return await resp.aread()
    too_big = DiffTooLarge(f"diff exceeds {limit} bytes")
    if int(resp.headers.get("content-length", 0)) > limit:
        raise too_big
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise too_big
    return bytes(buf)


class GitHubClient:
    def __init__(
        self,
        auth: GitHubAppAuth,
        client: httpx.AsyncClient,
        *,
        max_diff_bytes: int = MAX_DIFF_BYTES,
        max_files: int = MAX_FILES,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._auth = auth
        self._client = client
        self._max_diff = max_diff_bytes
        self._max_files = max_files
        self._clock = clock
        self._sleep = sleep

    async def fetch_pull_request(self, pr: PullRequestRef) -> PullRequest:
        d = json.loads(await self._get(pr, "", "application/vnd.github+json"))
        return PullRequest(
            installation_id=pr.installation_id,
            repo=pr.repo,
            number=pr.number,
            title=d["title"],
            author=d["user"]["login"],
            draft=d.get("draft", False),
            base_sha=d["base"]["sha"],
            base_ref=d["base"]["ref"],
            head_sha=d["head"]["sha"],
            head_ref=d["head"]["ref"],
            changed_files=d["changed_files"],
            additions=d["additions"],
            deletions=d["deletions"],
        )

    async def head_sha(self, pr: PullRequestRef) -> str:
        return (await self.fetch_pull_request(pr)).head_sha

    async def fetch_diff(self, pr: PullRequestRef) -> str:
        body = await self._get(pr, "", "application/vnd.github.diff", self._max_diff)
        return body.decode("utf-8", errors="replace")

    async def fetch_files(self, pr: PullRequestRef) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = json.loads(
                await self._get(
                    pr, f"/files?per_page={_PER_PAGE}&page={page}", "application/vnd.github+json"
                )
            )
            files += batch
            if len(files) > self._max_files:
                raise DiffTooLarge(f"more than {self._max_files} changed files")
            if len(batch) < _PER_PAGE:
                return files
            page += 1

    async def _get(
        self, pr: PullRequestRef, suffix: str, accept: str, limit: int | None = None
    ) -> bytes:
        url = f"{API}/repos/{pr.repo}/pulls/{pr.number}{suffix}"
        for attempt in range(_ATTEMPTS):
            token = (await self._auth.token(pr.installation_id)).reveal()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": accept,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            }
            wait = _BACKOFF * 2**attempt
            failure: Exception
            try:
                async with self._client.stream(
                    "GET", url, headers=headers, timeout=TIMEOUT
                ) as resp:
                    if resp.status_code == 200:
                        return await _read(resp, limit)
                    failure = self._map(resp)
            except httpx.HTTPError as exc:
                failure = Unavailable(f"github unreachable: {type(exc).__name__}")
            else:
                if isinstance(failure, RateLimited):
                    wait = self._rate_wait(resp)
                    if wait > MAX_RATE_WAIT:
                        raise failure
            if not isinstance(failure, Unavailable | RateLimited) or attempt == _ATTEMPTS - 1:
                raise failure
            await self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _rate_wait(self, resp: httpx.Response) -> float:
        h = resp.headers
        if "retry-after" in h:
            return float(h["retry-after"])
        return max(0.0, float(h["x-ratelimit-reset"]) - self._clock())

    def _map(self, resp: httpx.Response) -> Exception:
        s, h = resp.status_code, resp.headers
        limited = "retry-after" in h or (
            h.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in h
        )
        if s == 429 or (s == 403 and limited):
            if limited:
                return RateLimited(f"github rate limited ({s})")
            return Unavailable("github rate limited (429)")
        if s == 401:
            return Unauthorized("github rejected installation token (401)")
        if s == 403:
            return Forbidden("github denied request (403)")
        if s == 404:
            return NotFound("pull request not found (404)")
        if s >= 500:
            return Unavailable(f"github server error ({s})")
        return GitHubAuthError(f"unexpected github status {s}")
