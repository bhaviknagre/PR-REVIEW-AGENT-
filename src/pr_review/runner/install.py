"""Dependency-install sandbox step: the only step with (proxied) network access.

sandbox.py hardcodes network_mode=none, so this module composes the same hardening (non-root,
cap_drop ALL, no-new-privileges, read-only root, tmpfs, pids/mem/cpu/ulimit caps, empty env)
with one difference: the install container joins a per-run Docker *internal* network (no
gateway, no DNS to the outside) holding a single egress-proxy container. The proxy allows only
`CONNECT <allowlisted host>:443`. Installed packages are copied out of a capped tmpfs volume
into `<result.workspace>/.deps`; the caller then runs the test step with run_in_sandbox
(network none) on result.workspace and env PYTHONPATH=/workspace/.deps. Caller removes
result.workspace.

Proxy: a ~25-line stdlib asyncio CONNECT proxy run in the already-allowlisted python image
(no extra image to pin/pull). Managers: pip, npm, go, cargo (per-language images and
per-ecosystem allowlists, see MANAGER_IMAGES). pip runs with --only-binary=:all: by default so
no sdist build (setup.py) code executes during install.
"""

import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import docker
import requests
from docker.models.containers import Container
from docker.types import Ulimit

from pr_review.runner.sandbox import (
    LABEL,
    UID,
    WORKDIR,
    SandboxError,
    SandboxSpec,
    _check_image,
    _copy_repo,
    _read_stream,
    _text,
)
from pr_review.runner.workspace import _size

ECOSYSTEM_HOSTS: dict[str, tuple[str, ...]] = {
    "pip": ("pypi.org", "files.pythonhosted.org"),
    "npm": ("registry.npmjs.org",),
    "go": ("proxy.golang.org", "sum.golang.org"),
    "cargo": ("crates.io", "static.crates.io", "index.crates.io"),
}
DEFAULT_HOSTS: tuple[str, ...] = tuple(h for hs in ECOSYSTEM_HOSTS.values() for h in hs)
MANAGER_IMAGES: dict[str, str] = {
    "npm": "pr-review-sandbox-node:20",
    "go": "pr-review-sandbox-go:1.22",
    "cargo": "pr-review-sandbox-rust:1.83",
}
DEPS_DIR = ".deps"
_DEPS = "/deps"
_PORT = 3128
_PROXY_PY = r"""
import asyncio, sys
from urllib.parse import urlsplit
ALLOW = set(a for a in sys.argv[1:] if not a.startswith("http:"))
HTTP_ALLOW = set(a[5:] for a in sys.argv[1:] if a.startswith("http:"))
async def pipe(r, w):
    try:
        while d := await r.read(65536):
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()
async def deny(w, line):
    print("denied", line.strip()[:200], file=sys.stderr, flush=True)
    w.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    await w.drain()
    w.close()
async def handle(r, w):
    try:
        line = (await asyncio.wait_for(r.readline(), 10)).decode("ascii", "replace")
        heads = []
        while (h := await asyncio.wait_for(r.readline(), 10)) not in (b"\r\n", b"\n", b""):
            heads.append(h)
        method, target, _ = line.split(" ", 2)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            if f"{host.lower()}:{port}" not in ALLOW:
                return await deny(w, line)
            ur, uw = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 10)
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()
            await asyncio.gather(pipe(r, uw), pipe(ur, w))
            return
        u = urlsplit(target)
        if u.scheme != "http" or method not in ("GET", "HEAD") or not u.hostname:
            return await deny(w, line)
        port = u.port or 80
        if f"{u.hostname.lower()}:{port}" not in HTTP_ALLOW:
            return await deny(w, line)
        ur, uw = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), 10)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        drop = (b"connection", b"proxy-connection")
        keep = b"".join(h for h in heads if h.split(b":")[0].lower() not in drop)
        req = f"{method} {path} HTTP/1.1\r\n".encode() + keep + b"Connection: close\r\n\r\n"
        uw.write(req)
        await uw.drain()
        await pipe(ur, w)
    except Exception:
        w.close()
async def main():
    s = await asyncio.start_server(handle, "0.0.0.0", 3128)
    print("ready", flush=True)
    await s.serve_forever()
asyncio.run(main())
"""


def parse_index_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) of a package-index override; only http(s) with a host is valid."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise SandboxError("package index URL must be http(s)://host[:port]/... without userinfo")
    return u.scheme, u.hostname.lower(), u.port or (443 if u.scheme == "https" else 80)


def allowlist(
    manager: str = "pip", index_url: str | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(CONNECT hosts, plain-http host:port targets). The override host is the only addition,
    and it replaces nothing: it applies to pip only."""
    hosts = ECOSYSTEM_HOSTS[manager]
    if manager != "pip" or not index_url:
        return hosts, ()
    scheme, host, port = parse_index_url(index_url)
    if scheme == "https":
        return (*hosts, f"{host}:{port}"), ()
    return hosts, (f"{host}:{port}",)


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    notice: str  # sanitized, safe to show; never raw tool output
    workspace: Path | None  # workspace copy + DEPS_DIR when ok
    stderr: str = ""


def _hardening() -> dict[str, object]:
    return {
        "user": UID,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "privileged": False,
        "read_only": True,
        "labels": {LABEL: "1"},
    }


@contextmanager
def egress_proxy(
    cli: docker.DockerClient,
    spec: SandboxSpec,
    hosts: Sequence[str] = DEFAULT_HOSTS,
    http_targets: Sequence[str] = (),
) -> Iterator[tuple[str, str]]:
    """Yield (internal network name, proxy URL); everything is removed on exit."""
    net = cli.networks.create(
        f"prr-inst-{uuid.uuid4().hex[:12]}", driver="bridge", internal=True, labels={LABEL: "1"}
    )
    proxy: Container | None = None
    try:
        proxy = cli.containers.create(
            spec.image,
            entrypoint="python",
            command=[
                "-c",
                _PROXY_PY,
                *(h if ":" in h else f"{h}:443" for h in hosts),
                *(f"http:{t}" for t in http_targets),
            ],
            network_mode="bridge",  # egress side; clients only reach it via the internal net
            mem_limit=128 << 20,
            memswap_limit=128 << 20,
            pids_limit=64,
            **_hardening(),  # type: ignore[arg-type]
        )
        net.connect(proxy, aliases=["proxy"])
        proxy.start()
        for _ in range(60):
            if b"ready" in proxy.logs(stdout=True, stderr=False):
                break
            time.sleep(0.25)
        else:
            raise SandboxError("egress proxy did not start")
        yield str(net.name), f"http://proxy:{_PORT}"
    finally:
        if proxy is not None:
            try:
                proxy.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            net.remove()
        except Exception:  # noqa: BLE001
            pass


def _unpack(chunks: Iterator[bytes], ws: Path, cap: int) -> None:
    raw = bytearray()
    for ch in chunks:
        raw += ch
        if len(raw) > cap + (4 << 20):
            raise SandboxError("dependencies exceed size cap")
    stage = Path(tempfile.mkdtemp(prefix="prr-deps-"))
    try:
        with tarfile.open(fileobj=BytesIO(bytes(raw)), mode="r:") as tf:
            keep = [m for m in tf if m.isreg() or m.isdir()]  # symlinks/devices dropped
            if sum(m.size for m in keep if m.isreg()) > cap:
                raise SandboxError("dependencies exceed size cap")
            tf.extractall(stage, members=keep, filter="data")
        dest = ws / DEPS_DIR
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        src = stage / "deps"
        if src.is_dir():
            shutil.move(src, dest)
        else:
            dest.mkdir()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


_SH = "cd /deps && cp {w}/package.json {w}/package-lock.json . && npm ci --ignore-scripts"


def _plan(manager: str, workspace: Path, only_binary: bool) -> tuple[list[str], dict[str, str]]:
    """(argv, extra env) for one manager, or raise SandboxError when the project can't install."""

    def need(*names: str) -> None:
        for n in names:
            f = workspace / n
            if f.is_symlink() or not f.is_file():
                raise SandboxError(f"no {n}")

    if manager == "pip":
        need("requirements.txt")
        argv = ["python", "-m", "pip", "install", "--target", _DEPS, "--no-cache-dir"]
        argv += ["--disable-pip-version-check", "-r", f"{WORKDIR}/requirements.txt"]
        if only_binary:
            argv.append("--only-binary=:all:")
        return argv, {"PIP_NO_INPUT": "1"}
    if manager == "npm":
        need("package.json", "package-lock.json")
        sh = _SH.format(w=WORKDIR) + " --no-audit --no-fund --cache /tmp/npm"  # noqa: S108
        return ["sh", "-c", sh], {}
    if manager == "go":
        need("go.mod")
        env = {"GOMODCACHE": _DEPS, "GOFLAGS": "-modcacherw", "GOCACHE": "/tmp/gocache"}  # noqa: S108
        return ["go", "mod", "download"], env
    if manager == "cargo":
        need("Cargo.toml", "Cargo.lock")
        return ["cargo", "fetch", "--locked"], {"CARGO_HOME": _DEPS, "CARGO_NET_OFFLINE": "false"}
    raise SandboxError(f"unsupported package manager: {manager}")


def install_deps(  # noqa: PLR0912, PLR0915
    workspace: Path,
    *,
    manager: str = "pip",
    hosts: Sequence[str] | None = None,
    index_url: str | None = None,
    spec: SandboxSpec | None = None,
    timeout_s: float = 300,
    only_binary: bool = True,
    client: docker.DockerClient | None = None,
) -> InstallResult:
    """Install into <result.workspace>/.deps. `index_url` (pip only) swaps the package index for
    one extra allowlisted host:port (SANDBOX_PIP_INDEX_URL); None means real PyPI."""
    proxy_spec = spec or SandboxSpec()  # the proxy always runs in the python image
    spec = proxy_spec
    if manager not in ECOSYSTEM_HOSTS:
        raise SandboxError(f"unsupported package manager: {manager}")
    if manager != "pip":
        spec = replace(spec, image=MANAGER_IMAGES[manager])
    if any(k.upper().startswith("PRR_") for k in spec.env):
        raise SandboxError("PRR_* variables may not enter the sandbox")
    try:
        argv, extra = _plan(manager, workspace, only_binary)
        connect, plain = allowlist(manager, index_url)
    except SandboxError as e:
        return InstallResult(False, f"dependency install failed: {e}", None)
    if hosts is not None:
        connect = tuple(hosts)
    if manager == "pip" and index_url:
        extra["PIP_INDEX_URL"] = index_url
        if parse_index_url(index_url)[0] == "http":
            extra["PIP_TRUSTED_HOST"] = parse_index_url(index_url)[1]
    if _size(workspace) > spec.max_repo_bytes:
        raise SandboxError("workspace exceeds size limit")

    cli = client or docker.from_env()
    _check_image(cli, proxy_spec)
    _check_image(cli, spec)
    run_dir = Path(tempfile.mkdtemp(prefix="prr-inst-"))
    container: Container | None = None
    holder: Container | None = None
    volume = None
    keep_dir = False
    try:
        _copy_repo(workspace, run_dir)
        volume = cli.volumes.create(
            labels={LABEL: "1"},
            driver="local",
            driver_opts={
                "type": "tmpfs",
                "device": "tmpfs",
                "o": f"size={spec.out_bytes},uid=65534,gid=65534,nosuid,nodev",
            },
        )
        holder = cli.containers.create(
            spec.image,
            entrypoint="sleep",
            command=["86400"],
            network_mode="none",
            mem_limit=64 << 20,
            memswap_limit=64 << 20,
            pids_limit=8,
            volumes={volume.name: {"bind": _DEPS, "mode": "ro"}},
            **_hardening(),  # type: ignore[arg-type]
        )
        holder.start()
        with egress_proxy(cli, proxy_spec, connect, plain) as (net, url):
            env = {
                **spec.env,
                "HTTPS_PROXY": url,
                "HTTP_PROXY": url,
                "https_proxy": url,
                "http_proxy": url,
                "PIP_NO_INPUT": "1",
                "PIP_DEFAULT_TIMEOUT": "60",
                "PIP_RETRIES": "3",
                **extra,
            }
            container = cli.containers.create(
                spec.image,
                entrypoint=argv[0],
                command=argv[1:],
                working_dir=WORKDIR,
                environment=env,
                network_mode=net,
                pids_limit=spec.pids_limit,
                mem_limit=spec.mem_bytes,
                memswap_limit=spec.mem_bytes,
                nano_cpus=int(spec.cpus * 1e9),
                ulimits=[
                    Ulimit(name="nofile", soft=1024, hard=1024),
                    Ulimit(name="nproc", soft=512, hard=512),
                    Ulimit(name="fsize", soft=64 << 20, hard=64 << 20),
                ],
                tmpfs={"/tmp": f"noexec,nosuid,nodev,size={spec.tmp_bytes}"},  # noqa: S108
                volumes={
                    str(run_dir): {"bind": WORKDIR, "mode": "ro"},
                    volume.name: {"bind": _DEPS, "mode": "rw"},
                },
                detach=True,
                **_hardening(),  # type: ignore[arg-type]
            )
            container.start()
            over = threading.Event()
            bufs = (bytearray(), bytearray())
            readers = [
                threading.Thread(
                    target=_read_stream,
                    args=(container, w, spec.max_output_bytes, b, over),
                    daemon=True,
                )
                for w, b in zip(("out", "err"), bufs, strict=True)
            ]
            for t in readers:
                t.start()
            timed_out = False
            try:
                code = int(container.wait(timeout=timeout_s)["StatusCode"])
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                timed_out = True
                container.kill()
                code = int(container.wait(timeout=30)["StatusCode"])
            for t in readers:
                t.join(timeout=10)
        err = _text(bufs[1], spec.max_output_bytes)
        if timed_out:
            return InstallResult(False, "dependency install failed: timed out", None, err)
        if code != 0:
            low = err.lower()
            if "403" in low or "forbidden" in low:
                msg = "dependency install failed: registry not allowlisted"
            else:
                msg = f"dependency install failed (exit {code})"
            return InstallResult(False, msg, None, err)
        _unpack(holder.get_archive(_DEPS)[0], run_dir, spec.out_bytes)  # tmpfs-bounded
        keep_dir = True
        return InstallResult(True, "dependencies installed", run_dir, err)
    except SandboxError as e:
        return InstallResult(False, f"dependency install failed: {e}", None)
    finally:
        for c in (container, holder):
            if c is not None:
                try:
                    c.remove(force=True)
                except Exception:  # noqa: BLE001
                    pass
        if volume is not None:
            try:
                volume.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        if not keep_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
