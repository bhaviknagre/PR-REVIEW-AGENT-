"""Ephemeral git workspace: exact head/base checkout with hardened git, always destroyed."""

import asyncio
import logging
import os
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pr_review.connectors.base import CloneCredential
from pr_review.core.redaction import redact, register_secrets

log = logging.getLogger(__name__)

PREFIX = "prr-ws-"
BASE_REF = "refs/pr-review/base"
DEFAULT_MAX_BYTES = 1 << 30  # SPEC NFR-14: repos up to 1 GB

# Token lives only in the child's environment; this script holds no secret.
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$PRR_GIT_USER" ;;
  *) printf '%s\\n' "$PRR_GIT_TOKEN" ;;
esac
"""

# Hardening, applied to every git call and persisted in the repo config for later git use.
_HARDENING = (
    "core.hooksPath=/dev/null",
    "core.symlinks=false",  # symlinks become plain files: no escape out of the workspace
    "core.fsmonitor=false",
    "submodule.recurse=false",
    "transfer.fsckObjects=true",
    "fetch.fsckObjects=true",
    "filter.lfs.smudge=",
    "filter.lfs.process=",
    "filter.lfs.clean=",
    "filter.lfs.required=false",
)


class WorkspaceError(Exception):
    """Base error; messages are already redacted."""


class GitCommandError(WorkspaceError):
    pass


class GitTimeoutError(WorkspaceError):
    pass


class HeadMismatchError(WorkspaceError):
    pass


class WorkspaceTooLargeError(WorkspaceError):
    pass


@dataclass(frozen=True)
class Workspace:
    path: Path
    head_sha: str
    base_sha: str
    base_ref: str = BASE_REF


def _size(path: Path) -> int:
    """Total bytes under path; entries git deletes mid-walk are skipped, symlinks not followed."""
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


async def _git(
    cwd: Path,
    env: dict[str, str],
    args: Sequence[str],
    timeout: float,
    *,
    protocol: Sequence[str],
    watch: Path,
    max_bytes: int,
    poll_s: float,
) -> str:
    cmd = ["git", *(a for c in (*_HARDENING, *protocol) for a in ("-c", c)), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group: git's children die with it
    )
    task = asyncio.ensure_future(proc.communicate())
    deadline = time.monotonic() + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise GitTimeoutError(f"git {args[0]} timed out after {timeout}s")
            done, _ = await asyncio.wait({task}, timeout=min(poll_s, left))
            if await asyncio.to_thread(_size, watch) > max_bytes:
                raise WorkspaceTooLargeError(f"workspace exceeds {max_bytes} bytes")
            if done:
                break
    except BaseException:  # timeout, size, cancellation: never leave git running
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid under start_new_session
        except ProcessLookupError:
            pass
        task.cancel()
        await proc.wait()
        raise
    out, err = task.result()
    if proc.returncode:
        msg = redact(err.decode(errors="replace").strip())
        raise GitCommandError(f"git {args[0]} failed ({proc.returncode}): {msg}")
    return out.decode(errors="replace").strip()


def _remove(path: Path) -> None:
    """rmtree that retries: a dying writer can re-create files while we delete."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.05)


@asynccontextmanager
async def workspace(
    url: str,
    head_sha: str,
    base_sha: str,
    *,
    credential: CloneCredential | None = None,
    root: Path | None = None,
    timeout: float = 120.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    size_poll_s: float = 0.5,
    allow_file_protocol: bool = False,  # tests only: lets file:// origins work
) -> AsyncIterator[Workspace]:
    """Yield a checkout of head_sha (detached, verified) with base_sha fetched; removed on exit."""
    ws = Path(tempfile.mkdtemp(prefix=PREFIX, dir=root))  # mkdtemp creates 0700
    try:
        ws.chmod(0o700)
        repo, home = ws / "repo", ws / "home"
        repo.mkdir()
        home.mkdir()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ALLOW_PROTOCOL": "https:file" if allow_file_protocol else "https",
            "LC_ALL": "C",
        }
        if credential:
            register_secrets(credential.token)
            askpass = ws / "askpass.sh"
            askpass.write_text(_ASKPASS)
            askpass.chmod(0o700)
            env |= {
                "GIT_ASKPASS": str(askpass),
                "PRR_GIT_USER": credential.username,
                "PRR_GIT_TOKEN": credential.token,
            }
        proto = [
            "protocol.file.allow=always" if allow_file_protocol else "protocol.file.allow=never"
        ]

        async def git(*args: str) -> str:
            return await _git(
                repo,
                env,
                args,
                timeout,
                protocol=proto,
                watch=ws,
                max_bytes=max_bytes,
                poll_s=size_poll_s,
            )

        log.info("workspace fetch", extra={"url": url, "head": head_sha, "base": base_sha})
        await git("init", "--quiet", "--template=")
        for cfg in _HARDENING:
            await git("config", *cfg.split("=", 1))
        await git("fetch", "--quiet", "--no-tags", "--depth=1", "--no-recurse-submodules",
                  url, head_sha, base_sha)  # fmt: skip
        await git("update-ref", BASE_REF, base_sha)
        await git("checkout", "--quiet", "--detach", "--force", head_sha)
        actual = await git("rev-parse", "HEAD")
        if actual != head_sha:
            raise HeadMismatchError(f"HEAD {actual} != expected {head_sha}")
        yield Workspace(repo, head_sha, base_sha)
    finally:
        _remove(ws)


def cleanup_stale(root: Path, max_age_s: float = 3600.0) -> int:
    """Crash recovery: remove leftover workspaces older than max_age_s. Returns count removed."""
    n = 0
    for d in root.glob(f"{PREFIX}*"):
        if d.is_dir() and time.time() - d.stat().st_mtime >= max_age_s:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
