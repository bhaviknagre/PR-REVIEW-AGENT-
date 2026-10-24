"""End-to-end hostile repos: real git origin -> real workspace() -> real sandbox -> real parsers.

Fixture code under tests/fixtures/hostile executes only inside the container. Secret-looking
values are built at runtime.
"""

import asyncio
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import docker
import pytest

from pr_review.core.redaction import redact
from pr_review.parsers.coverage import CoverageResult, parse_cobertura, parse_lcov
from pr_review.parsers.tests import ParseError, TestRunResult, parse_junit
from pr_review.runner.sandbox import LABEL, SandboxResult, SandboxSpec, run_in_sandbox
from pr_review.runner.workspace import workspace

try:
    CLIENT = docker.from_env()
    CLIENT.ping()
    REASON = ""
except Exception as e:  # noqa: BLE001
    REASON = f"Docker daemon unreachable: {e}"

pytestmark = [pytest.mark.docker, pytest.mark.skipif(bool(REASON), reason=REASON or "x")]

HOSTILE = Path(__file__).parent.parent / "fixtures" / "hostile"
SITE = Path(next(p for p in sys.path if p.endswith("site-packages")))
DEPS = ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments", "py.py")
MARKER = "HOSTMARKER" + "Zq8xLm4Pv9Rt2"
PYTEST = [
    "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "python_files=suite_*.py",
    "--junitxml=/out/junit.xml", "/workspace",
]  # fmt: skip
GIT_ENV = {
    **os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}  # fmt: skip


@dataclass
class Outcome:
    result: SandboxResult
    parsed: TestRunResult | ParseError | None
    notice: str | None


def notice_for(r: SandboxResult, parsed: TestRunResult | ParseError | None) -> str | None:
    """Test-local SandboxResult -> notice mapping. A `runner` notice function is needed in src
    (T-24/T-39); this only drives assertions."""
    if r.timed_out:
        msg = "test run timed out"
    elif r.oom_killed:
        msg = "test run killed: memory limit"
    elif r.output_truncated:
        msg = "test run killed: output limit"
    elif r.artifacts_rejected:
        msg = "test artifacts rejected: size limit"
    elif r.exit_code >= 128:
        msg = f"test run killed by signal {r.exit_code - 128}"
    elif isinstance(parsed, ParseError):
        msg = f"test results unreadable: {parsed.error_class}"
    elif re.search(r"unreachable|Errno 101|Name or service", r.stdout + r.stderr):
        msg = "network access blocked"
    else:
        return None
    return redact(re.sub(r"[^\x20-\x7e]", "", msg))[:200]


def _git(cwd: Path, *a: str) -> str:
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *a], cwd=cwd, env=GIT_ENV,
                       capture_output=True, text=True, check=True)  # fmt: skip
    return r.stdout.strip()


def build_origin(tmp: Path, case: str) -> tuple[str, str, str]:
    src = tmp / "origin-src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "README").write_text("base")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "base")
    base = _git(src, "rev-parse", "HEAD")
    shutil.copytree(HOSTILE / case, src, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__*"))
    if case == "symlinks":
        for n, t in (("plain_passwd", "/etc/passwd"), ("plain_sock", "/var/run/docker.sock")):
            os.symlink(t, src / n)
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "head")
    head = _git(src, "rev-parse", "HEAD")
    bare = tmp / "origin.git"
    _git(tmp, "clone", "-q", "--bare", str(src), str(bare))
    _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return f"file://{bare}", head, base


def run_case(
    tmp: Path, case: str, *, timeout: float = 60, mem: int = 512 << 20, pids: int = 128
) -> Outcome:
    url, head, base = build_origin(tmp, case)
    root = tmp / "wsroot"
    root.mkdir()
    spec = SandboxSpec(
        mem_bytes=mem, pids_limit=pids,
        env={"HOME": "/tmp", "LANG": "C.UTF-8", "CI": "1", "PYTHONPATH": "/workspace/.deps",
             "PYTHONDONTWRITEBYTECODE": "1"},
    )  # fmt: skip

    async def go() -> SandboxResult:
        async with workspace(url, head, base, root=root, allow_file_protocol=True) as w:
            if case == "symlinks":  # real links, as a checkout without the symlink hardening
                home = Path.home()
                for n, t in (("real_passwd", "/etc/passwd"), ("real_ssh", f"{home}/.ssh"),
                             ("real_sock", "/var/run/docker.sock"),
                             ("real_rel", "../../../../../../etc/hostname")):  # fmt: skip
                    os.symlink(t, w.path / n)
            deps = w.path / ".deps"
            deps.mkdir()
            for d in DEPS:
                if d.endswith(".py"):
                    shutil.copy(SITE / d, deps / d)
                else:
                    shutil.copytree(
                        SITE / d, deps / d, ignore=shutil.ignore_patterns("__pycache__")
                    )
            return await asyncio.to_thread(run_in_sandbox, spec, w.path, PYTEST, timeout_s=timeout)

    r = asyncio.run(go())
    assert list(root.iterdir()) == []  # workspace removed
    j = r.artifacts.get("junit.xml")
    parsed = parse_junit(j) if j is not None else None
    return Outcome(r, parsed, notice_for(r, parsed))


@pytest.fixture(autouse=True)
def guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HOSTILE_HOST_SECRET", MARKER)
    tmpdir = Path(tempfile.gettempdir())
    before = {p.name for p in tmpdir.glob("prr-*")}
    yield
    assert {p.name for p in tmpdir.glob("prr-*")} == before  # nothing left on the host
    assert CLIENT.containers.list(all=True, filters={"label": f"{LABEL}=1"}) == []
    assert CLIENT.volumes.list(filters={"label": f"{LABEL}=1"}) == []


def blob(o: Outcome) -> str:
    r = o.result
    return (
        r.stdout + r.stderr + repr(r.artifacts) + (o.parsed.model_dump_json() if o.parsed else "")
    )


def test_env_exfiltration_sees_only_allowlist(tmp_path: Path) -> None:
    o = run_case(tmp_path, "exfil_env")
    assert isinstance(o.parsed, TestRunResult) and o.parsed.failed == 1
    assert MARKER not in blob(o) and getpass.getuser() not in blob(o)
    leak = o.result.artifacts["exfil.txt"].decode()
    assert "'HOME': '/tmp'" in leak and "HOSTILE_HOST_SECRET" not in leak
    assert "PRR_" not in leak


def test_symlinks_never_reach_host(tmp_path: Path) -> None:
    o = run_case(tmp_path, "symlinks")
    links = json.loads(o.result.artifacts["links.json"])
    # core.symlinks=false: checkout turns links into plain files holding the target text
    assert links["plain_passwd"] == ["read", False, "/etc/passwd"]
    assert links["plain_sock"] == ["read", False, "/var/run/docker.sock"]
    # a real link resolves inside the container: its own passwd, or nothing
    assert "root:x:0:0:root:/root:/bin/bash" in links["real_passwd"][2]  # not the host's
    assert getpass.getuser() not in links["real_passwd"][2]
    for n in ("real_ssh", "real_sock"):
        assert links[n][0] == "err", links[n]
    assert links["real_rel"][0] == "err" or socket.gethostname() not in links["real_rel"][2]
    assert MARKER not in blob(o)


def test_privileged_operations_fail(tmp_path: Path) -> None:
    o = run_case(tmp_path, "privileged")
    res = json.loads(o.result.artifacts["priv.json"])
    for k in ("mount", "unshare_user", "unshare_mount", "chmod_suid_ro", "write_sys",
              "write_proc_sys", "write_workspace", "setuid0"):  # fmt: skip
        assert res[k].startswith("errno"), (k, res[k])
    assert res["ptrace_pid1"] != "OK", res
    assert "nosuid" in res["tmp_mount_opts"][0]  # a setuid file in /tmp is inert


def test_malicious_artifacts_rejected_or_safe(tmp_path: Path) -> None:
    o = run_case(tmp_path, "xml_cov")
    a = o.result.artifacts
    for n in ("bomb.xml", "xxe.xml"):
        p = parse_junit(a[n])
        assert isinstance(p, ParseError), n
        assert "root:" not in p.model_dump_json()
    for p2 in (
        parse_cobertura(a["cob.xml"], "/workspace"),
        parse_lcov(a["trav.lcov"], "/workspace"),
    ):
        assert isinstance(p2, CoverageResult)
        assert all(not f.path.startswith(("/", "..")) and ".." not in f.path for f in p2.files)
        assert p2.warnings
    cob = parse_cobertura(a["cob.xml"], "/workspace")
    assert isinstance(cob, CoverageResult) and [f.path for f in cob.files] == ["ok.py"]


def test_background_daemons_die_with_container(tmp_path: Path) -> None:
    o = run_case(tmp_path, "daemon")
    assert isinstance(o.parsed, TestRunResult) and o.parsed.passed == 1
    assert o.notice is None  # and the guard fixture proves no container survives


def test_tiny_file_flood_is_bounded(tmp_path: Path) -> None:
    spec = SandboxSpec()
    o = run_case(tmp_path, "flood", timeout=90)
    r = o.result
    assert r.artifacts_rejected or (
        len(r.artifacts) < 150_000
        and sum(map(len, r.artifacts.values())) <= spec.max_artifact_bytes
    )
    if r.artifacts_rejected:
        assert o.notice == "test artifacts rejected: size limit" and r.artifacts == {}


def test_ten_gb_sparse_file_stopped(tmp_path: Path) -> None:
    o = run_case(tmp_path, "sparse")
    r = o.result
    assert all(len(v) <= 64 << 20 for v in r.artifacts.values())
    assert sum(map(len, r.artifacts.values())) <= SandboxSpec().max_artifact_bytes
    assert o.notice is not None or (isinstance(o.parsed, TestRunResult) and o.parsed.errored == 0)


def test_slow_loris_timed_out(tmp_path: Path) -> None:
    t = time.monotonic()
    o = run_case(tmp_path, "loris", timeout=6)
    assert o.result.timed_out and o.notice == "test run timed out"
    assert time.monotonic() - t < 45


def test_sleeper_timed_out(tmp_path: Path) -> None:
    o = run_case(tmp_path, "sleeper", timeout=5)
    assert o.result.timed_out and o.parsed is None and o.notice == "test run timed out"


def test_memory_bomb_notice(tmp_path: Path) -> None:
    o = run_case(tmp_path, "memory_bomb", mem=128 << 20)
    assert o.result.oom_killed and o.notice == "test run killed: memory limit"


def test_network_attempt_blocked_with_notice(tmp_path: Path) -> None:
    o = run_case(tmp_path, "network")
    assert isinstance(o.parsed, TestRunResult) and o.parsed.failed == 1
    assert o.notice == "network access blocked"


def test_ansi_and_token_sanitized(tmp_path: Path) -> None:
    o = run_case(tmp_path, "ansi_token")
    tok = "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cD1fG3hJ5"
    assert isinstance(o.parsed, TestRunResult) and o.parsed.failed == 1
    msg = o.parsed.failures[0].message
    assert tok not in o.result.stdout + o.parsed.model_dump_json() + o.result.stderr
    assert "[REDACTED]" in msg
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", msg), repr(msg)
