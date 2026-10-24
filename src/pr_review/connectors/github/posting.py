"""Idempotent GitHub review post per (repo, PR, head SHA) (FR-20, FR-21, NFR-7, AC-16).

Flow: stale-SHA guard -> duplicate check (store hook + hidden marker in existing reviews) -> one
review with inline comments -> on a 422 anchor failure retry once without inline comments, listing
them under "Could not anchor". A 5xx/rate-limit retry re-checks for the review first, since the
failed POST may have landed.
"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

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
from pr_review.connectors.github.client import (
    API_VERSION,
    MAX_RATE_WAIT,
    TIMEOUT,
    USER_AGENT,
    GitHubClient,
    RateLimited,
)
from pr_review.core.redaction import redact
from pr_review.review.aggregate import InlineComment, ReviewPlan
from pr_review.review.summary import esc

MAX_BODY = 60_000  # GitHub rejects review and comment bodies above 65,536 characters
_ATTEMPTS = 3
_BACKOFF = 1.0
_MAX_BACKOFF = 20.0
_PER_PAGE = 100
_TRUNC = "\n\n_(truncated)_"
_SECONDARY = ("secondary rate limit", "abuse")


def marker(sha: str) -> str:
    return f"<!-- pr-review-agent:sha={sha}:v1 -->"


class PostRejected(GitHubAuthError):
    """GitHub refused the review (422) even without inline comments; not retryable."""


@dataclass(frozen=True)
class Skipped:
    reason: Literal["stale", "duplicate"]
    review_id: int | None = None


@dataclass(frozen=True)
class Posted:
    review_id: int
    comment_ids: tuple[int, ...]
    unanchored: int


class ReviewStore(Protocol):
    """Persistence hook backed by the comments table (kind=review|inline)."""

    async def posted_review(self, repo: str, number: int, sha: str) -> int | None: ...

    async def record(
        self, repo: str, number: int, sha: str, review_id: int, comment_ids: tuple[int, ...]
    ) -> None: ...

    def exclusive(self, repo: str, number: int, sha: str) -> AbstractAsyncContextManager[None]:
        """Cross-process mutual exclusion for one (repo, PR, head sha) post."""
        ...


class _AlreadyPosted(Exception):
    def __init__(self, review_id: int) -> None:
        self.review_id = review_id


def _clip(text: str, limit: int = MAX_BODY) -> str:
    text = redact(text)
    return text if len(text) <= limit else text[: limit - len(_TRUNC)] + _TRUNC


def _fence(code: str) -> str:
    ticks = "`" * max(3, max((len(r) for r in code.replace("`", " ").split()), default=0) + 3)
    return f"{ticks}suggestion\n{code}\n{ticks}"


def _comment_body(c: InlineComment, suggestion: str | None) -> str:
    block = "" if suggestion is None else "\n\n" + _fence(suggestion.removesuffix("\n"))
    return _clip(c.body, MAX_BODY - len(block)) + block


def _unanchored(items: list[InlineComment]) -> str:
    lines = [f"- `{esc(c.file, 200)}:{c.line}` {esc(c.body, 160)}" for c in items]
    return "\n\n### Could not anchor\n" + "\n".join(lines)


class ReviewPoster:
    def __init__(
        self,
        auth: GitHubAppAuth,
        client: httpx.AsyncClient,
        github: GitHubClient,
        *,
        request_changes_on_critical: bool = False,
        store: ReviewStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._auth = auth
        self._client = client
        self._github = github
        self._rc = request_changes_on_critical
        self._store = store
        self._sleep = sleep
        self._jitter = jitter

    async def post_review(
        self,
        pr: PullRequestRef,
        head_sha: str,
        plan: ReviewPlan,
        summary_md: str,
        suggestions: Mapping[tuple[str, int], str] | None = None,
    ) -> Posted | Skipped:
        """`suggestions[(file, line)]` replaces exactly that commented (RIGHT-side) line.

        Serialized per (repo, PR, head sha): a reclaimed job must not post alongside the
        original worker, so the duplicate checks and the create run under one lock."""
        lock = self._store.exclusive(pr.repo, pr.number, head_sha) if self._store else nullcontext()
        async with lock:
            return await self._post_locked(pr, head_sha, plan, summary_md, suggestions)

    async def _post_locked(
        self,
        pr: PullRequestRef,
        head_sha: str,
        plan: ReviewPlan,
        summary_md: str,
        suggestions: Mapping[tuple[str, int], str] | None,
    ) -> Posted | Skipped:
        if await self._github.head_sha(pr) != head_sha:
            return Skipped("stale")
        if self._store and (rid := await self._store.posted_review(pr.repo, pr.number, head_sha)):
            return Skipped("duplicate", rid)
        if (rid := await self._existing(pr, head_sha)) is not None:
            return await self._adopt(pr, head_sha, rid)

        sugg = suggestions or {}
        comments = [
            {
                "path": c.file,
                "line": c.line,
                "side": "RIGHT",
                "body": _comment_body(c, sugg.get((c.file, c.line))),
            }
            for c in plan.inline
        ]
        critical = any(c.severity == "CRITICAL" for c in plan.inline)
        event = "REQUEST_CHANGES" if self._rc and critical else "COMMENT"
        payload = {
            "commit_id": head_sha,
            "event": event,
            "body": self._body(summary_md, head_sha, []),
            "comments": comments,
        }
        unanchored = 0
        try:
            try:
                data = await self._create(pr, head_sha, payload)
            except PostRejected:
                if not comments:
                    raise
                unanchored = len(comments)
                payload = {
                    **payload,
                    "body": self._body(summary_md, head_sha, list(plan.inline)),
                    "comments": [],
                }
                data = await self._create(pr, head_sha, payload)
        except _AlreadyPosted as found:
            return await self._adopt(pr, head_sha, found.review_id)
        review_id = int(data["id"])
        ids = await self._comment_ids(pr, review_id) if comments and not unanchored else ()
        if self._store:
            await self._store.record(pr.repo, pr.number, head_sha, review_id, ids)
        return Posted(review_id, ids, unanchored)

    async def _adopt(self, pr: PullRequestRef, sha: str, review_id: int) -> Skipped:
        """A review found by its marker (e.g. its create response was lost) is recorded once."""
        if self._store:
            ids = await self._comment_ids(pr, review_id)
            await self._store.record(pr.repo, pr.number, sha, review_id, ids)
        return Skipped("duplicate", review_id)

    @staticmethod
    def _body(summary_md: str, sha: str, lost: list[InlineComment]) -> str:
        tail = (_unanchored(lost) if lost else "") + "\n\n" + marker(sha)
        return _clip(summary_md, MAX_BODY - len(tail)) + tail

    async def _existing(self, pr: PullRequestRef, sha: str) -> int | None:
        page = 1
        while True:
            resp = await self._call(pr, "GET", f"/reviews?per_page={_PER_PAGE}&page={page}")
            batch = resp.json()
            for r in batch:
                if marker(sha) in (r.get("body") or ""):
                    return int(r["id"])
            if len(batch) < _PER_PAGE:
                return None
            page += 1

    async def _comment_ids(self, pr: PullRequestRef, review_id: int) -> tuple[int, ...]:
        ids: list[int] = []
        page = 1
        while True:
            path = f"/reviews/{review_id}/comments?per_page={_PER_PAGE}&page={page}"
            batch = (await self._call(pr, "GET", path)).json()
            ids += [int(c["id"]) for c in batch]
            if len(batch) < _PER_PAGE:
                return tuple(ids)
            page += 1

    async def _create(self, pr: PullRequestRef, sha: str, payload: dict[str, Any]) -> Any:
        async def recheck() -> None:
            if (rid := await self._existing(pr, sha)) is not None:
                raise _AlreadyPosted(rid)

        resp = await self._call(pr, "POST", "/reviews", payload, recheck)
        return resp.json()

    async def _call(
        self,
        pr: PullRequestRef,
        method: str,
        suffix: str,
        body: dict[str, Any] | None = None,
        recheck: Callable[[], Awaitable[None]] | None = None,
    ) -> httpx.Response:
        url = f"{API}/repos/{pr.repo}/pulls/{pr.number}{suffix}"
        for attempt in range(_ATTEMPTS):
            token = (await self._auth.token(pr.installation_id)).reveal()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            }
            wait = min(_BACKOFF * 2**attempt, _MAX_BACKOFF) * (0.5 + self._jitter() / 2)
            try:
                resp = await self._client.request(
                    method, url, headers=headers, timeout=TIMEOUT,
                    content=None if body is None else json.dumps(body),
                )  # fmt: skip
            except httpx.HTTPError as exc:
                failure: GitHubAuthError = Unavailable(f"github unreachable: {type(exc).__name__}")
            else:
                if resp.is_success:
                    return resp
                failure = self._map(resp)
                if isinstance(failure, RateLimited):
                    wait = max(wait, self._retry_after(resp))
                    if wait > MAX_RATE_WAIT:
                        raise failure
            if not isinstance(failure, Unavailable | RateLimited) or attempt == _ATTEMPTS - 1:
                raise failure
            await self._sleep(wait)
            if recheck:
                await recheck()
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            return float(resp.headers["retry-after"])
        except (KeyError, ValueError):
            return 0.0  # no hint: the jittered backoff alone applies

    @staticmethod
    def _map(resp: httpx.Response) -> GitHubAuthError:
        s, h = resp.status_code, resp.headers
        text = resp.text.lower()
        limited = (
            "retry-after" in h
            or h.get("x-ratelimit-remaining") == "0"
            or any(m in text for m in _SECONDARY)
        )
        if s == 429 or (s == 403 and limited):
            return RateLimited(f"github rate limited ({s})")
        if s == 401:
            return Unauthorized("github rejected installation token (401)")
        if s == 403:
            return Forbidden("github denied request (403)")
        if s == 404:
            return NotFound("pull request not found (404)")
        if s == 422:
            return PostRejected("github rejected the review (422)")
        if s >= 500:
            return Unavailable(f"github server error ({s})")
        return GitHubAuthError(f"unexpected github status {s}")
