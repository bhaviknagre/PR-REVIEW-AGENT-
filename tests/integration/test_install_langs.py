"""Real per-language dependency installs (npm, Go, Rust) through the allowlisting proxy, plus the
pip index override against the real caching mirror. Needs `./docker/sandbox/build.sh` images."""

import shutil
from pathlib import Path

import docker
import pytest

from pr_review.runner.install import DEPS_DIR, MANAGER_IMAGES, install_deps

FX = Path(__file__).parent.parent / "fixtures" / "install"

try:
    CLIENT: docker.DockerClient | None = docker.from_env()
    assert CLIENT is not None
    CLIENT.ping()
    REASON = None
except Exception as e:  # noqa: BLE001
    CLIENT, REASON = None, f"docker unavailable: {e}"

pytestmark = [pytest.mark.docker, pytest.mark.skipif(bool(REASON), reason=REASON or "x")]


def _need(manager: str) -> None:
    assert CLIENT is not None
    img = MANAGER_IMAGES[manager]
    try:
        CLIENT.images.get(img)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{img} not built locally (run ./docker/sandbox/build.sh)")


def _run(manager: str, name: str, tmp_path: Path) -> Path:
    _need(manager)
    src = tmp_path / "src"
    shutil.copytree(FX / name, src)
    r = install_deps(src, manager=manager, client=CLIENT, timeout_s=240)
    assert r.ok, (r.notice, r.stderr)
    assert r.workspace is not None
    return r.workspace


def test_npm_ci_ignore_scripts(tmp_path: Path) -> None:
    ws = _run("npm", "npm", tmp_path)
    try:
        assert (ws / DEPS_DIR / "node_modules" / "left-pad" / "package.json").is_file()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_go_mod_download(tmp_path: Path) -> None:
    ws = _run("go", "go", tmp_path)
    try:
        assert any((ws / DEPS_DIR).rglob("uuid@v1.6.0"))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_cargo_fetch_locked(tmp_path: Path) -> None:
    ws = _run("cargo", "rust", tmp_path)
    try:
        assert any((ws / DEPS_DIR).rglob("itoa-1.0.11"))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_npm_requires_lockfile(tmp_path: Path) -> None:
    _need("npm")
    (tmp_path / "package.json").write_text("{}")
    r = install_deps(tmp_path, manager="npm", client=CLIENT)
    assert not r.ok
    assert "package-lock.json" in r.notice


def test_pip_index_override_through_real_mirror(tmp_path: Path, pkg_index: str | None) -> None:
    if pkg_index is None:
        pytest.skip("package cache disabled")
    (tmp_path / "requirements.txt").write_text("six==1.16.0\n")
    r = install_deps(tmp_path, client=CLIENT, index_url=pkg_index)
    assert r.ok, (r.notice, r.stderr)
    assert r.workspace is not None
    try:
        assert (r.workspace / DEPS_DIR / "six.py").is_file()
    finally:
        shutil.rmtree(r.workspace, ignore_errors=True)
