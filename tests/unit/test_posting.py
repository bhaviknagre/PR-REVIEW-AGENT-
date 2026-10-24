"""T-38: review posting against the real sandbox repo (marker `github`) plus a loopback fault server
for rate-limit and 5xx backoff, which GitHub cannot be made to produce on demand."""

import asyncio
import base64
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from pr_review.connectors.base import PullRequestRef
from pr_review.connectors.github.auth import API, GitHubAppAuth, Unavailable
from pr_review.connectors.github.client import GitHubClient, RateLimited
from pr_review.connectors.github.posting import (
    Posted,
    PostRejected,
    ReviewPoster,
    Skipped,
    marker,
)
from pr_review.review.aggregate import Counts, InlineComment, ReviewPlan

ENV_FILE = Path(os.environ.get("PR_REVIEW_GH_TEST_ENV", "~/.config/pr-review-agent/env.test"))
ENV_FILE = ENV_FILE.expanduser()
SRC = "def avg(xs):\n    return sum(xs) / len(xs)\n"
FIX = "    return sum(xs) / len(xs) if xs else 0.0"


def _env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    pairs = (ln.split("=", 1) for ln in ENV_FILE.read_text().splitlines() if "=" in ln)
    return {k.strip(): v.strip().strip("\"'") for k, v in pairs}


def plan(*inline: InlineComment) -> ReviewPlan:
    zero = dict.fromkeys(
        ("summary_only", "filtered", "filtered_category", "filtered_severity"), 0
    ) | dict.fromkeys(("filtered_confidence", "deduped", "dropped_unsafe"), 0)
    counts = Counts(total=len(inline), inline=len(inline), **zero)
    return ReviewPlan(
        inline=inline,
        summary=(),
        failed_categories=(),
        failed_chunks=(),
        unmatched_tests=(),
        skipped_files=(),
        notices=(),
        truncated=False,
        omitted_chunks=0,
        counts=counts,
    )


def bug(line: int = 2, severity: str = "MAJOR", file: str = "app.py") -> InlineComment:
    return InlineComment(
        file=file,
        line=line,
        severity=severity,  # type: ignore[arg-type]
        category="correctness",
        body=f"**{severity}**: division by zero on empty input",
    )


# --- real GitHub -----------------------------------------------------------------------------

github = pytest.mark.github
needs_env = pytest.mark.skipif(
    not _env().get("GITHUB_TEST_REPO"), reason=f"real GitHub test env absent ({ENV_FILE})"
)


def gh(*args: str, stdin: str | None = None) -> Any:
    out = subprocess.run(
        ["gh", "api", *args], input=stdin, capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out) if out.strip() else None


def put(repo: str, branch: str, content: str, msg: str, sha: str | None = None) -> str:
    body = {
        "message": msg,
        "branch": branch,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    r = gh("-X", "PUT", f"repos/{repo}/contents/app.py", "--input", "-", stdin=json.dumps(body))
    return str(r["commit"]["sha"])


@dataclass
class Sandbox:
    repo: str
    branch: str
    ref: PullRequestRef
    sha1: str
    poster: ReviewPoster
    auth: GitHubAppAuth
    client: httpx.AsyncClient
    loop: asyncio.AbstractEventLoop

    def reviews(self) -> list[dict[str, Any]]:
        r = gh("--paginate", f"repos/{self.repo}/pulls/{self.ref.number}/reviews")
        return list(r)

    def comments(self) -> list[dict[str, Any]]:
        r = gh("--paginate", f"repos/{self.repo}/pulls/{self.ref.number}/comments")
        return list(r)

    def push_second_commit(self) -> str:
        blob = gh(f"repos/{self.repo}/contents/app.py?ref={self.branch}")["sha"]
        sha = put(self.repo, self.branch, SRC + "\n# more\n", "second", blob)
        for _ in range(30):  # the PR head lags the push by a moment
            if gh(f"repos/{self.repo}/pulls/{self.ref.number}")["head"]["sha"] == sha:
                return sha
            time.sleep(1)
        raise AssertionError("PR head never advanced")


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    env = _env()
    repo = env["GITHUB_TEST_REPO"]
    branch = f"prr-test-{uuid.uuid4().hex[:8]}"
    number = 0
    loop = asyncio.new_event_loop()
    client = httpx.AsyncClient()
    try:
        base = gh(f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
        gh("-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}",
           "-f", f"sha={base}")  # fmt: skip
        sha1 = put(repo, branch, SRC, "add avg")
        pr = gh("-X", "POST", f"repos/{repo}/pulls", "-f", f"head={branch}", "-f", "base=main",
                "-f", f"title=posting test {branch}", "-f", "body=automated test")  # fmt: skip
        number = pr["number"]
        auth = GitHubAppAuth(
            int(env["GITHUB_APP_ID"]),
            SecretStr(Path(env["GITHUB_PRIVATE_KEY_PATH"]).expanduser().read_text()),
            client,
        )
        inst = httpx.get(
            f"{API}/repos/{repo}/installation",
            headers={"Authorization": f"Bearer {auth.app_jwt()}"},
        ).json()["id"]
        ref = PullRequestRef(installation_id=inst, repo=repo, number=number)
        poster = ReviewPoster(
            auth, client, GitHubClient(auth, client), request_changes_on_critical=True
        )
        yield Sandbox(repo, branch, ref, sha1, poster, auth, client, loop)
    finally:
        if number:
            subprocess.run(["gh", "api", "-X", "PATCH", f"repos/{repo}/pulls/{number}",
                            "-f", "state=closed"], capture_output=True)  # fmt: skip
        subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"],
                       capture_output=True)  # fmt: skip
        loop.run_until_complete(client.aclose())
        loop.close()


def post(sb: Sandbox, sha: str, p: ReviewPlan, **kw: Any) -> Posted | Skipped:
    poster = kw.pop("poster", sb.poster)
    return sb.loop.run_until_complete(
        poster.post_review(sb.ref, sha, p, "## Review\nfound issues", **kw)
    )


@github
@needs_env
def test_inline_suggestion_posted_once_and_idempotent(sandbox: Sandbox) -> None:
    sb = sandbox
    first = post(sb, sb.sha1, plan(bug()), suggestions={("app.py", 2): FIX})
    assert isinstance(first, Posted) and first.unanchored == 0 and len(first.comment_ids) == 1
    revs = [r for r in sb.reviews() if marker(sb.sha1) in (r["body"] or "")]
    assert [r["id"] for r in revs] == [first.review_id] and revs[0]["state"] == "COMMENTED"
    (c,) = sb.comments()
    assert c["id"] == first.comment_ids[0] and c["line"] == 2 and c["side"] == "RIGHT"
    assert f"```suggestion\n{FIX}\n```" in c["body"]

    again = post(sb, sb.sha1, plan(bug()), suggestions={("app.py", 2): FIX})
    assert again == Skipped("duplicate", first.review_id)
    assert len(sb.reviews()) == 1 and len(sb.comments()) == 1


@github
@needs_env
def test_stale_sha_skips_and_posts_nothing(sandbox: Sandbox) -> None:
    sb = sandbox
    sb.push_second_commit()
    assert post(sb, sb.sha1, plan(bug())) == Skipped("stale")
    assert sb.reviews() == [] and sb.comments() == []


@github
@needs_env
def test_off_diff_comment_degrades_to_summary(sandbox: Sandbox) -> None:
    sb = sandbox
    res = post(sb, sb.sha1, plan(bug(line=99)))
    assert isinstance(res, Posted) and res.unanchored == 1 and res.comment_ids == ()
    (rev,) = sb.reviews()
    assert rev["id"] == res.review_id and sb.comments() == []
    assert "Could not anchor" in rev["body"] and "app.py:99" in rev["body"]
    assert marker(sb.sha1) in rev["body"]


@github
@needs_env
def test_request_changes_only_on_critical_when_enabled(sandbox: Sandbox) -> None:
    sb = sandbox
    res = post(sb, sb.sha1, plan(bug(severity="CRITICAL")))
    assert isinstance(res, Posted)
    (rev,) = sb.reviews()
    assert rev["state"] == "CHANGES_REQUESTED" and len(sb.comments()) == 1


@github
@needs_env
def test_comment_default_when_disabled_even_if_critical(sandbox: Sandbox) -> None:
    sb = sandbox
    off = ReviewPoster(sb.auth, sb.client, GitHubClient(sb.auth, sb.client))
    assert isinstance(post(sb, sb.sha1, plan(bug(severity="CRITICAL")), poster=off), Posted)
    (rev,) = sb.reviews()
    assert rev["state"] == "COMMENTED"


# --- loopback fault server (backoff) ------------------------------------------------------------

SHA = "a" * 40
TOKEN = "ghs_loopbacksecrettoken1234567890"
PR = PullRequestRef(installation_id=1, repo="o/r", number=3)


class _Fault(ThreadingHTTPServer):
    def __init__(self, post_script: list[tuple[int, dict[str, str], str]], landed: bool) -> None:
        super().__init__(("127.0.0.1", 0), _H)
        self.script = post_script
        self.landed = landed
        self.posts = 0
        self.created = False


class _H(BaseHTTPRequestHandler):
    server: _Fault

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(self, code: int, body: object, headers: dict[str, str] | None = None) -> None:
        raw = body if isinstance(body, str) else json.dumps(body)
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw.encode())

    def do_GET(self) -> None:
        if self.path.endswith("/reviews") or "/reviews?" in self.path:
            made = [{"id": 77, "body": f"x {marker(SHA)}"}] if self.server.created else []
            return self._reply(200, made)
        if "/comments" in self.path:
            return self._reply(200, [])
        d = {"title": "t", "user": {"login": "u"}, "base": {"sha": "b", "ref": "main"},
             "head": {"sha": SHA, "ref": "f"}, "changed_files": 1, "additions": 1,
             "deletions": 0}  # fmt: skip
        self._reply(200, d)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.posts += 1
        code, headers, body = self.server.script.pop(0)
        if code == 200 or self.server.landed:
            self.server.created = True
        self._reply(code, body, headers)


class _Loopback(httpx.AsyncHTTPTransport):
    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return await super().handle_async_request(request)


class _Auth:
    async def token(self, installation_id: int) -> Any:
        return type("T", (), {"reveal": lambda self: TOKEN})()


def run_fault(
    script: list[tuple[int, dict[str, str], str]], landed: bool = False
) -> tuple[Any, list[float], _Fault]:
    srv = _Fault(script, landed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sleeps: list[float] = []

    async def go() -> Any:
        async def sleep(s: float) -> None:
            sleeps.append(s)

        async with httpx.AsyncClient(transport=_Loopback(srv.server_port)) as c:
            auth: Any = _Auth()
            poster = ReviewPoster(auth, c, GitHubClient(auth, c), sleep=sleep, jitter=lambda: 1.0)
            try:
                return await poster.post_review(PR, SHA, plan(bug()), "s")
            except Exception as exc:  # returned so callers can assert on its type
                return exc

    try:
        return asyncio.run(go()), sleeps, srv
    finally:
        srv.shutdown()
        srv.server_close()


OK = (200, {}, '{"id": 77}')


def test_backoff_retries_5xx_and_secondary_limit_then_posts() -> None:
    script = [
        (503, {}, "boom"),
        (403, {"Retry-After": "2"}, '{"message":"You have exceeded a secondary rate limit"}'),
        OK,
    ]
    res, sleeps, srv = run_fault(script)
    assert isinstance(res, Posted) and res.review_id == 77 and srv.posts == 3
    assert len(sleeps) == 2 and sleeps[0] == 1.0 and sleeps[1] == 2.0  # jitter pinned at max


def test_5xx_that_actually_landed_is_not_reposted() -> None:
    res, _, srv = run_fault([(502, {}, "bad gateway"), OK], landed=True)
    assert res == Skipped("duplicate", 77) and srv.posts == 1


def test_persistent_5xx_raises_typed_error_without_token() -> None:
    res, sleeps, srv = run_fault([(500, {}, "x")] * 3)
    assert isinstance(res, Unavailable) and srv.posts == 3 and len(sleeps) == 2
    assert TOKEN not in str(res)


def test_rate_limit_wait_beyond_cap_raises_without_sleeping() -> None:
    res, sleeps, srv = run_fault([(429, {"Retry-After": "3600"}, "slow down")])
    assert isinstance(res, RateLimited) and sleeps == [] and srv.posts == 1


def test_unanchorable_and_rejected_everywhere_raises_post_rejected() -> None:
    res, _, srv = run_fault([(422, {}, "{}"), (422, {}, "{}")])
    assert isinstance(res, PostRejected) and srv.posts == 2


def test_422_on_inline_retries_once_without_comments() -> None:
    res, _, srv = run_fault([(422, {}, "{}"), OK])
    assert isinstance(res, Posted) and res.unanchored == 1 and srv.posts == 2
