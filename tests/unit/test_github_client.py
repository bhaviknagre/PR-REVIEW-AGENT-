import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from pr_review.connectors.base import PullRequestRef
from pr_review.connectors.github.auth import (
    Forbidden,
    NotFound,
    Unauthorized,
    Unavailable,
)
from pr_review.connectors.github.client import (
    API_VERSION,
    DiffTooLarge,
    GitHubClient,
    RateLimited,
)

PR = PullRequestRef(installation_id=7, repo="o/r", number=3)
SECRET = "ghs_supersecret"
NOW = 1000.0
META = {
    "title": "t",
    "user": {"login": "alice"},
    "draft": True,
    "base": {"sha": "b" * 40, "ref": "main"},
    "head": {"sha": "h" * 40, "ref": "feat"},
    "changed_files": 2,
    "additions": 5,
    "deletions": 1,
}


def sync(fn: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(fn)
    def run(*a: Any, **k: Any) -> None:
        asyncio.run(fn(*a, **k))

    return run


class FakeAuth:
    async def token(self, installation_id: int):  # noqa: ANN201
        class T:
            def reveal(self) -> str:
                return SECRET

        return T()


def make(responses, **kw):  # noqa: ANN001, ANN003, ANN201
    reqs: list[httpx.Request] = []
    sleeps: list[float] = []
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        reqs.append(req)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    async def sleep(s: float) -> None:
        sleeps.append(s)

    c = GitHubClient(
        FakeAuth(),  # type: ignore[arg-type]
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
        sleep=sleep,
        **kw,
    )
    return c, reqs, sleeps


def ok(**kw):  # noqa: ANN003, ANN201
    return httpx.Response(200, **kw)


@sync
async def test_metadata_and_head_sha() -> None:
    c, reqs, _ = make([ok(json=META), ok(json=META)])
    pr = await c.fetch_pull_request(PR)
    assert (pr.author, pr.draft, pr.base_ref, pr.head_sha, pr.additions) == (
        "alice", True, "main", "h" * 40, 5,
    )  # fmt: skip
    assert pr.installation_id == 7 and pr.repo == "o/r" and pr.changed_files == 2
    assert await c.head_sha(PR) == "h" * 40
    h = reqs[0].headers
    assert h["x-github-api-version"] == API_VERSION
    assert h["accept"] == "application/vnd.github+json"
    assert h["user-agent"] and h["authorization"] == f"Bearer {SECRET}"
    assert str(reqs[0].url) == "https://api.github.com/repos/o/r/pulls/3"
    assert reqs[0].extensions["timeout"]["read"] == 30.0


@sync
async def test_diff_accept_header_and_limit() -> None:
    c, reqs, _ = make([ok(text="diff --git a b"), ok(text="x" * 21)], max_diff_bytes=20)
    assert await c.fetch_diff(PR) == "diff --git a b"
    assert reqs[0].headers["accept"] == "application/vnd.github.diff"
    with pytest.raises(DiffTooLarge) as e:
        await c.fetch_diff(PR)
    assert "xxx" not in str(e.value)


@sync
async def test_files_pagination_and_limit() -> None:
    page1 = [{"filename": f"f{i}"} for i in range(100)]
    c, reqs, _ = make([ok(json=page1), ok(json=[{"filename": "last"}])])
    assert len(await c.fetch_files(PR)) == 101
    assert "page=2" in str(reqs[1].url)
    c, _, _ = make([ok(json=page1)], max_files=50)
    with pytest.raises(DiffTooLarge):
        await c.fetch_files(PR)


@pytest.mark.parametrize(
    ("status", "exc"), [(401, Unauthorized), (403, Forbidden), (404, NotFound)]
)
@sync
async def test_error_mapping_no_retry(status: int, exc: type[Exception]) -> None:
    c, reqs, sleeps = make([httpx.Response(status)])
    with pytest.raises(exc) as e:
        await c.fetch_pull_request(PR)
    assert SECRET not in str(e.value) and len(reqs) == 1 and not sleeps


@sync
async def test_5xx_and_timeout_retried_then_ok() -> None:
    c, reqs, sleeps = make([httpx.Response(502), httpx.ReadTimeout("slow"), ok(json=META)])
    assert (await c.fetch_pull_request(PR)).title == "t"
    assert sleeps == [0.5, 1.0] and len(reqs) == 3


@sync
async def test_retries_exhausted() -> None:
    c, _, _ = make([httpx.ConnectTimeout("x")] * 3)
    with pytest.raises(Unavailable) as e:
        await c.fetch_pull_request(PR)
    assert SECRET not in str(e.value)


@sync
async def test_retry_after_honored() -> None:
    c, _, sleeps = make([httpx.Response(429, headers={"retry-after": "7"}), ok(json=META)])
    await c.fetch_pull_request(PR)
    assert sleeps == [7.0]


@sync
async def test_primary_limit_uses_reset_clock() -> None:
    hdr = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 20)}
    c, _, sleeps = make([httpx.Response(403, headers=hdr), ok(json=META)])
    await c.fetch_pull_request(PR)
    assert sleeps == [20.0]


@sync
async def test_rate_limit_wait_too_long() -> None:
    c, _, sleeps = make([httpx.Response(403, headers={"retry-after": "3600"})])
    with pytest.raises(RateLimited):
        await c.fetch_pull_request(PR)
    assert not sleeps


@sync
async def test_plain_429_is_retried() -> None:
    c, _, sleeps = make([httpx.Response(429), ok(json=META)])
    await c.fetch_pull_request(PR)
    assert sleeps == [0.5]


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.read = chunks, 0

    async def __aiter__(self):  # noqa: ANN204
        for c in self.chunks:
            self.read += 1
            yield c


@sync
async def test_content_length_rejected_without_reading_body() -> None:
    body = _Body([b"x" * 10])
    c, _, _ = make(
        [httpx.Response(200, headers={"content-length": "999"}, stream=body)], max_diff_bytes=20
    )
    with pytest.raises(DiffTooLarge):
        await c.fetch_diff(PR)
    assert body.read == 0


@sync
async def test_chunked_body_aborted_once_over_limit() -> None:
    body = _Body([b"x" * 15] * 100)
    c, _, _ = make([httpx.Response(200, stream=body)], max_diff_bytes=20)
    with pytest.raises(DiffTooLarge):
        await c.fetch_diff(PR)
    assert body.read == 2
