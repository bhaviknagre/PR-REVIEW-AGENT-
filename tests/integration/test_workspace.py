import asyncio
import logging
import os
import subprocess
from pathlib import Path

import pytest

from pr_review.connectors.base import CloneCredential
from pr_review.runner import workspace as wsmod
from pr_review.runner.workspace import (
    GitCommandError,
    GitTimeoutError,
    HeadMismatchError,
    WorkspaceTooLargeError,
    cleanup_stale,
    workspace,
)

TOKEN = "ghs_SuperSecretTokenValue1234567890abcd"
CRED = CloneCredential("x-access-token", TOKEN)


def sh(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_ALLOW_PROTOCOL": "file"}  # fmt: skip
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, str]:
    src = tmp_path / "src"
    src.mkdir()
    sh(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("one")
    sh(src, "add", ".")
    sh(src, "commit", "-qm", "base")
    base = sh(src, "rev-parse", "HEAD")
    (src / "a.txt").write_text("two")
    (src / "model.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 5\n"
    )
    (src / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (src / ".githooks").mkdir()
    marker = tmp_path / "hook-ran"
    hook = src / ".githooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    sub = "160000 " + base + "\tvendor/sub"
    sh(src, "update-index", "--add", "--cacheinfo", sub.replace("\t", ",").replace(" ", ",", 1))
    (src / ".gitmodules").write_text('[submodule "s"]\n\tpath = vendor/sub\n\turl = file:///nope\n')
    sh(src, "add", ".")
    sh(src, "commit", "-qm", "head")
    head = sh(src, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    sh(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    sh(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
    return {"url": f"file://{bare}", "head": head, "base": base, "marker": str(marker)}


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def open_ws(o: dict[str, str], root: Path, **kw):  # type: ignore[no-untyped-def]
    return workspace(o["url"], o["head"], o["base"], root=root, allow_file_protocol=True, **kw)


def test_checkout_exact_sha_base_and_cleanup(origin: dict[str, str], tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    async def go() -> Path:
        async with open_ws(origin, root, credential=CRED) as w:
            assert sh(w.path, "rev-parse", "HEAD") == origin["head"]
            assert sh(w.path, "rev-parse", w.base_ref) == origin["base"]
            assert (w.path / "a.txt").read_text() == "two"
            assert w.path.parent.stat().st_mode & 0o777 == 0o700
            return w.path.parent

    ws = run(go())
    assert not ws.exists()
    assert list(root.iterdir()) == []


def test_token_not_persisted_or_logged(
    origin: dict[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def go() -> None:
        async with open_ws(origin, tmp_path, credential=CRED) as w:
            for f in w.path.parent.rglob("*"):
                if f.is_file() and f.name != "askpass.sh":
                    assert TOKEN.encode() not in f.read_bytes(), f
            cfg = (w.path / ".git" / "config").read_text()
            assert "hooksPath" in cfg and "extraheader" not in cfg.lower()

    run(go())
    assert TOKEN not in caplog.text


def test_askpass_script_reads_env(tmp_path: Path) -> None:
    p = tmp_path / "ap.sh"
    p.write_text(wsmod._ASKPASS)
    env = {"PRR_GIT_USER": "u", "PRR_GIT_TOKEN": "tok"}
    out = [subprocess.run(["sh", p, q], env=env, capture_output=True, text=True).stdout
           for q in ("Username for 'x': ", "Password for 'x': ")]  # fmt: skip
    assert out == ["u\n", "tok\n"]


def test_error_text_has_no_token(origin: dict[str, str], tmp_path: Path) -> None:
    async def go() -> None:
        async with workspace(
            "file:///does/not/exist/" + TOKEN, origin["head"], origin["base"],
            credential=CRED, root=tmp_path, allow_file_protocol=True,
        ):  # fmt: skip
            pass

    with pytest.raises(GitCommandError) as e:
        run(go())
    assert TOKEN not in str(e.value)
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_head_mismatch(
    origin: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = wsmod._git

    async def fake(cwd, env, args, timeout, **kw):  # type: ignore[no-untyped-def]
        out = await real(cwd, env, args, timeout, **kw)
        return origin["base"] if args[0] == "rev-parse" else out

    monkeypatch.setattr(wsmod, "_git", fake)
    with pytest.raises(HeadMismatchError):
        run(open_ws(origin, tmp_path).__aenter__())
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_hooks_submodules_lfs_inert(origin: dict[str, str], tmp_path: Path) -> None:
    async def go() -> None:
        async with open_ws(origin, tmp_path) as w:
            assert not Path(origin["marker"]).exists()
            assert not (w.path / "vendor" / "sub").is_file()
            assert not (w.path / "vendor" / "sub" / ".git").exists()  # not initialised
            assert "git-lfs.github.com" in (w.path / "model.bin").read_text()  # pointer intact
            assert not (w.path / ".git" / "modules").exists()

    run(go())
    assert not Path(origin["marker"]).exists()


def test_removed_on_exception(origin: dict[str, str], tmp_path: Path) -> None:
    async def go() -> None:
        async with open_ws(origin, tmp_path):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run(go())
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_removed_on_cancellation(origin: dict[str, str], tmp_path: Path) -> None:
    async def go() -> None:
        entered = asyncio.Event()

        async def body() -> None:
            async with open_ws(origin, tmp_path):
                entered.set()
                await asyncio.sleep(60)

        t = asyncio.create_task(body())
        await entered.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    run(go())
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_timeout(origin: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(GitTimeoutError):
        run(open_ws(origin, tmp_path, timeout=0.0001).__aenter__())
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_size_guard(origin: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(WorkspaceTooLargeError):
        run(open_ws(origin, tmp_path, max_bytes=10).__aenter__())
    assert list(tmp_path.glob("prr-ws-*")) == []


def test_cleanup_stale(tmp_path: Path) -> None:
    (tmp_path / "prr-ws-old").mkdir()
    (tmp_path / "keep").mkdir()
    assert cleanup_stale(tmp_path, max_age_s=0) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]


def _big_origin(tmp_path: Path) -> str:
    src = tmp_path / "big"
    src.mkdir()
    sh(src, "init", "-q", "-b", "main")
    (src / "blob.bin").write_bytes(os.urandom(40 << 20))
    sh(src, "add", ".")
    sh(src, "commit", "-qm", "big")
    return sh(src, "rev-parse", "HEAD")


def _stray_git(tmp_path: Path) -> list[str]:
    ps = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True)
    return [
        ln for ln in ps.stdout.splitlines()
        if str(tmp_path) in ln or "core.hooksPath=/dev/null" in ln
    ]  # fmt: skip


@pytest.mark.parametrize("kw", [{"max_bytes": 100_000, "size_poll_s": 0.005}, {"timeout": 0.3}])
def test_abort_leaves_no_files_or_processes(tmp_path: Path, kw: dict[str, float]) -> None:
    sha = _big_origin(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    err = WorkspaceTooLargeError if "max_bytes" in kw else GitTimeoutError
    with pytest.raises(err):
        run(
            workspace(
                f"file://{tmp_path / 'big'}", sha, sha, root=root, allow_file_protocol=True, **kw
            ).__aenter__()
        )
    assert list(root.iterdir()) == []
    assert _stray_git(tmp_path) == []


def test_size_ignores_entries_vanishing_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "gone.lock").write_bytes(b"x" * 100)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b").write_bytes(b"123")
    real = os.DirEntry.stat

    def flaky(self: os.DirEntry[str], *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == "gone.lock":
            raise FileNotFoundError(self.path)
        return real(self, follow_symlinks=follow_symlinks)

    assert wsmod._size(tmp_path) == 108  # unpatched baseline
    monkeypatch.setattr(os.DirEntry, "stat", flaky, raising=False)
    assert wsmod._size(tmp_path) == 8
