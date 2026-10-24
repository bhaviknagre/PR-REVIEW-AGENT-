"""Shared real-service plumbing for the perf (T-45) and load (T-46) tests.

Real: Postgres 16 and Redis 7 containers, a local bare git repository, ARQ worker subprocesses.
Local HTTP servers (the existing fault-server pattern, no mocking of our own code):
- StubLlm: Ollama-protocol server (tests/integration/fault_server.py handler) that answers every
  review prompt with one fixed, schema-valid finding, and can return 500/429/hang for a window.
- StubGitHub: the GitHub REST endpoints the pipeline calls (installation token, pull request,
  diff, reviews list/create, review comments), keeping every posted review for end assertions.
  Can answer 502 for a window and "commit then 502" (response lost) for N review creations.
"""

# ruff: noqa: E402

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent / "integration"), str(HERE.parent / "e2e")]

from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from fault_server import FaultServer, _Handler
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pr_review.api.app import create_app
from pr_review.core.settings import Settings

REPO = "acme/bench"
INSTALLATION = 4242
APP_ID = 1234
LLM_MODEL = "ollama/stub-review"
CHEAP_CONFIG = "tests_disabled: true\ncategories: [correctness]\n"


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


# --- Postgres + Redis containers ---------------------------------------------------------------


@dataclass
class Infra:
    db_url: str
    redis_url: str
    pg_name: str
    redis_name: str
    engine: Engine
    factory: sessionmaker[Session]

    def restart_redis(self) -> None:
        """`docker restart` keeps the published port, so the URL stays valid."""
        assert docker("restart", "-t", "2", self.redis_name).returncode == 0
        for _ in range(60):
            if docker("exec", self.redis_name, "redis-cli", "ping").stdout.strip() == "PONG":
                return
            time.sleep(0.5)
        raise AssertionError("redis did not come back after restart")


def _run_container(name: str, image: str, port: int, inner: int, *args: str) -> None:
    run = docker("run", "-d", "--name", name, *args, "-p", f"127.0.0.1:{port}:{inner}", image)
    if run.returncode != 0:  # no --rm: `rm -fv` also removes the anonymous volume
        pytest.skip(f"cannot start {image}: {run.stderr.strip()}")


@contextmanager
def infra(tag: str) -> Iterator[Infra]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("Docker unavailable")
    suffix = uuid.uuid4().hex[:8]
    pg_name, redis_name = f"pr-review-{tag}-pg-{suffix}", f"pr-review-{tag}-redis-{suffix}"
    pg_port, redis_port = free_port(), free_port()
    engine: Engine | None = None
    try:
        _run_container(pg_name, "postgres:16-alpine", pg_port, 5432, "-e", "POSTGRES_PASSWORD=pw")
        _run_container(redis_name, "redis:7.4-alpine", redis_port, 6379)
        db_url = f"postgresql+psycopg://postgres:pw@127.0.0.1:{pg_port}/postgres"
        engine = create_engine(db_url, pool_size=20, max_overflow=20, pool_pre_ping=True)
        for _ in range(60):
            try:
                engine.connect().close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("postgres did not become ready")
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/pr_review/storage/migrations"))
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        yield Infra(
            db_url, f"redis://127.0.0.1:{redis_port}", pg_name, redis_name, engine,
            sessionmaker(engine, expire_on_commit=False),
        )  # fmt: skip
    finally:
        if engine is not None:
            engine.dispose()
        docker("rm", "-fv", pg_name, redis_name)


# --- local git origin -------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_ALLOW_PROTOCOL": "file",
    }  # fmt: skip
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return r.stdout


@dataclass
class PrData:
    number: int
    base_sha: str
    head_sha: str
    diff: str
    additions: int
    deletions: int
    files: int


def build_origin(git_root: Path, prs: dict[int, dict[str, str]]) -> dict[int, PrData]:
    """A real repo at <git_root>/acme/bench.git: one shared base commit and one head commit per PR
    (each PR's files added on top of the base). Returns what the GitHub stub serves per PR."""
    src = git_root / "src"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    (src / "README.md").write_text("bench\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "base")
    base = _git(src, "rev-parse", "HEAD").strip()
    out: dict[int, PrData] = {}
    for number, files in prs.items():
        _git(src, "checkout", "-q", "-b", f"pr{number}", base)
        for path, content in files.items():
            (src / path).parent.mkdir(parents=True, exist_ok=True)
            (src / path).write_text(content)
        _git(src, "add", ".")
        _git(src, "commit", "-qm", f"pr {number}")
        head = _git(src, "rev-parse", "HEAD").strip()
        diff = _git(src, "diff", "--no-color", base, head)
        stat = _git(src, "diff", "--numstat", base, head).split()
        adds = sum(int(x) for x in stat[0::3])
        dels = sum(int(x) for x in stat[1::3])
        out[number] = PrData(number, base, head, diff, adds, dels, len(files))
        _git(src, "checkout", "-q", "main")
    bare = git_root / "acme" / "bench.git"
    bare.parent.mkdir(parents=True)
    _git(git_root, "clone", "-q", "--bare", str(src), str(bare))
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    shutil.rmtree(src)
    return out


# --- LLM stub (Ollama protocol) -----------------------------------------------------------------


class _LlmHandler(_Handler):
    server: "StubLlm"

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw or b"{}")
        model = str(body.get("model", ""))
        with self.server.lock:
            self.server.hits.append((self.path, model))
        mode = self.server.mode()
        if mode is not None:
            with self.server.lock:
                self.server.faults_served += 1
        if mode == "429":
            self._send(429, b'{"error":"slow down"}', {"Retry-After": "1"})
        elif mode == "500":
            self._send(500, b'{"error":"boom"}')
        elif mode == "hang":
            time.sleep(self.server.hang_s)
        else:
            time.sleep(self.server.delay_s)
            text = body.get("prompt") or "\n".join(
                str(m.get("content", "")) for m in body.get("messages", [])
            )
            reply = {"findings": self.server.findings_for(text)}
            counts = {"prompt_eval_count": 50, "eval_count": 20, "done": True, "model": model}
            content = (
                {"message": {"role": "assistant", "content": json.dumps(reply)}}
                if self.path == "/api/chat"
                else {"response": json.dumps(reply)}
            )
            self._send(200, json.dumps({**content, **counts}).encode())


_FILE = re.compile(r"^File: (.+)\nChanged line numbers: (\d+)", re.M)


class StubLlm(FaultServer):
    """Fixed, valid review JSON: one MINOR finding on the first changed line of the asked file."""

    def __init__(self, delay_s: float = 0.0, hang_s: float = 6.0) -> None:
        super().__init__(upstream="http://127.0.0.1:9")  # never proxied
        self.RequestHandlerClass = _LlmHandler  # before the first request arrives
        self.delay_s, self.hang_s = delay_s, hang_s
        self.lock = threading.Lock()
        self.plan: list[tuple[float, str]] = []  # (monotonic end, mode)
        self.faults_served = 0

    def window(self, *phases: tuple[str, float]) -> None:
        """Consecutive fault phases starting now: e.g. ("500", 8), ("429", 6)."""
        t = time.monotonic()
        plan = []
        for mode, seconds in phases:
            t += seconds
            plan.append((t, mode))
        with self.lock:
            self.plan = plan

    def mode(self) -> str | None:
        now = time.monotonic()
        with self.lock:
            for end, mode in self.plan:
                if now < end:
                    return mode
        return None

    @staticmethod
    def findings_for(prompt: str) -> list[dict[str, Any]]:
        m = _FILE.search(prompt)
        if m is None:
            return []
        line = int(m.group(2))
        return [
            {
                "severity": "MINOR", "category": "correctness", "file_path": m.group(1),
                "line_start": line, "line_end": line, "summary": "Check this change",
                "rationale": "Fixed review used by the perf and load tests.",
                "suggestion": None, "confidence": 0.6,
            }
        ]  # fmt: skip


# --- GitHub stub --------------------------------------------------------------------------------

_ROUTE = re.compile(
    r"^/repos/(?P<repo>[^/]+/[^/]+)/pulls/(?P<n>\d+)"
    r"(?P<tail>/reviews(?:/(?P<rid>\d+)/comments)?)?$"
)


class StubGitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, prs: dict[int, PrData], latency_s: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _GhHandler)
        self.prs, self.latency_s = prs, latency_s
        self.lock = threading.Lock()
        self.reviews: dict[int, list[dict[str, Any]]] = {}  # PR number -> created reviews
        self.down_until = 0.0
        self.lose_next_reviews = 0  # create the review, then answer 502 (response lost)
        self.faults_served = 0
        self._ids = iter(range(1000, 10**9))
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def outage(self, seconds: float) -> None:
        self.down_until = time.monotonic() + seconds

    def all_reviews(self) -> list[tuple[int, dict[str, Any]]]:
        with self.lock:
            return [(n, r) for n, rs in sorted(self.reviews.items()) for r in rs]

    def next_id(self) -> int:
        return next(self._ids)


class _GhHandler(BaseHTTPRequestHandler):
    server: StubGitHub

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, code: int, payload: Any, ctype: str = "application/json") -> None:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _down(self) -> bool:
        if time.monotonic() < self.server.down_until:
            with self.server.lock:
                self.server.faults_served += 1
            self._send(502, {"message": "bad gateway"})
            return True
        return False

    def _body(self) -> Any:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        return json.loads(raw) if raw else None

    def do_POST(self) -> None:
        body = self._body()
        time.sleep(self.server.latency_s)
        if self._down():
            return
        if re.fullmatch(r"/app/installations/\d+/access_tokens", self.path):
            exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            self._send(201, {"token": "ghs_" + secrets.token_hex(16), "expires_at": exp})
            return
        m = _ROUTE.match(self.path.split("?")[0])
        if m is None or m["tail"] != "/reviews":
            self._send(404, {"message": "not found"})
            return
        number = int(m["n"])
        with self.server.lock:
            rid = self.server.next_id()
            review = {
                "id": rid, "commit_id": body["commit_id"], "body": body["body"],
                "event": body["event"], "state": "COMMENTED",
                "comments": [{**c, "id": self.server.next_id()} for c in body["comments"]],
            }  # fmt: skip
            self.server.reviews.setdefault(number, []).append(review)
            lose = self.server.lose_next_reviews > 0
            if lose:
                self.server.lose_next_reviews -= 1
                self.server.faults_served += 1
        if lose:
            self._send(502, {"message": "bad gateway"})  # created, but the caller never learns it
        else:
            self._send(200, {"id": rid})

    def do_GET(self) -> None:
        time.sleep(self.server.latency_s)
        if self._down():
            return
        m = _ROUTE.match(self.path.split("?")[0])
        pr = self.server.prs.get(int(m["n"])) if m else None
        if m is None or pr is None:
            self._send(404, {"message": "not found"})
            return
        if not m["tail"]:
            if "diff" in self.headers.get("Accept", ""):
                self._send(200, pr.diff, "text/plain")
            else:
                self._send(200, self._pull(pr))
            return
        with self.server.lock:
            reviews = list(self.server.reviews.get(pr.number, []))
        found_page = re.search(r"[?&]page=(\d+)", self.path)  # not the "page" inside "per_page"
        page = int(found_page.group(1)) if found_page else 1
        if m["rid"]:
            found = [r for r in reviews if r["id"] == int(m["rid"])]
            items = [{"id": c["id"]} for r in found for c in r["comments"]]
        else:
            items = [{"id": r["id"], "body": r["body"]} for r in reviews]
        self._send(200, items if page == 1 else [])

    @staticmethod
    def _pull(pr: PrData) -> dict[str, Any]:
        return {
            "title": f"bench {pr.number}", "user": {"login": "dev"}, "draft": False,
            "base": {"sha": pr.base_sha, "ref": "main"},
            "head": {"sha": pr.head_sha, "ref": f"pr{pr.number}"},
            "changed_files": pr.files, "additions": pr.additions, "deletions": pr.deletions,
        }  # fmt: skip


# --- webhook app + worker pool -------------------------------------------------------------------


@dataclass
class Stack:
    infra: Infra
    settings: Settings
    secret: str
    pem: str


def make_stack(inf: Infra) -> Stack:
    secret, pem = secrets.token_hex(16), private_key_pem()
    settings = Settings(
        github_app_id=APP_ID,
        github_private_key=SecretStr(pem),
        github_webhook_secret=SecretStr(secret),
        database_url=SecretStr(inf.db_url),
        redis_url=SecretStr(inf.redis_url),
    )
    return Stack(inf, settings, secret, pem)


@contextmanager
def webhook_client(st: Stack) -> Iterator[TestClient]:
    os.environ["REDIS_URL"] = st.infra.redis_url  # create_enqueuer reads it when the app starts
    try:
        with TestClient(create_app(st.settings, engine=st.infra.engine)) as client:
            yield client
    finally:
        os.environ.pop("REDIS_URL", None)


def deliver(
    client: TestClient, secret: str, number: int, sha: str, *, delivery: str | None = None
) -> Any:
    """POST a correctly HMAC-signed pull_request webhook the way GitHub sends it."""
    payload = {
        "action": "opened",
        "number": number,
        "pull_request": {"number": number, "head": {"sha": sha}},
        "repository": {"full_name": REPO},
        "installation": {"id": INSTALLATION},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/github",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": delivery or str(uuid.uuid4()),
            "x-hub-signature-256": sig,
        },
    )


@dataclass
class WorkerPool:
    """N real `arq` worker subprocesses. `ensure()` respawns any that exited (a supervisor's job
    in production), so SIGKILL and Redis restarts are survivable."""

    env: dict[str, str]
    size: int
    logdir: Path
    procs: list["subprocess.Popen[bytes]"] = field(default_factory=list)
    spawned: int = 0
    killed: int = 0

    def _spawn(self) -> "subprocess.Popen[bytes]":
        self.spawned += 1
        with (self.logdir / f"worker-{self.spawned}.log").open("wb") as log:
            return subprocess.Popen(
                [sys.executable, "-m", "arq", "worker_entry.WorkerSettings"],
                env=self.env, stdout=log, stderr=subprocess.STDOUT, cwd=HERE,
                start_new_session=True,
            )  # fmt: skip

    def start(self) -> None:
        self.procs = [self._spawn() for _ in range(self.size)]

    def ensure(self) -> int:
        revived = 0
        for i, p in enumerate(self.procs):
            if p.poll() is not None:
                self.procs[i] = self._spawn()
                revived += 1
        return revived

    def sigkill(self, index: int) -> None:
        p = self.procs[index]
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
            self.killed += 1

    def log_tail(self, chars: int = 2000) -> str:
        return "\n".join(
            f.name + ":\n" + f.read_text()[-chars:] for f in sorted(self.logdir.glob("*.log"))
        )

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=10)


def worker_env(
    st: Stack, llm: StubLlm, gh: StubGitHub, git_root: Path, workspaces: Path, **extra: str
) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "LLM_"))}
    return {
        **base,
        "DATABASE_URL": st.infra.db_url,
        "REDIS_URL": st.infra.redis_url,
        "GITHUB_APP_ID": str(APP_ID),
        "GITHUB_PRIVATE_KEY": st.pem,
        "GITHUB_WEBHOOK_SECRET": st.secret,
        "LLM_REVIEW_MODEL": LLM_MODEL,
        "LLM_TRIAGE_MODEL": LLM_MODEL,
        "OLLAMA_API_BASE": llm.url,
        "WORKSPACE_ROOT": str(workspaces),
        "STUB_GITHUB_API": gh.url,
        "STUB_GIT_ROOT": str(git_root),
        **extra,
    }
