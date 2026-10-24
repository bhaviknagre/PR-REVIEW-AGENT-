"""Docker SDK sandbox for untrusted repository commands.

Isolation: non-root uid, all caps dropped, default seccomp, no-new-privileges, read-only root,
no network, no swap, pids/cpu/mem/ulimit caps, empty env (explicit allowlist only), killed on
wall-clock timeout. The repo is copied (minus .git and askpass.sh) and mounted read-only; the
only writable paths are tmpfs /tmp and a per-run output dir. The host env is never forwarded.

Production should run against a rootless Docker daemon (or gVisor runtime, per the v1 decision);
Docker Desktop is not rootless, so this is detected and reported, not enforced.
"""

import io
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import docker
import requests
from docker.errors import ImageNotFound
from docker.models.containers import Container
from docker.types import Ulimit

from pr_review.core.redaction import redact
from pr_review.runner.workspace import _size

LABEL = "pr-review.sandbox"
UID = "65534:65534"
OUT_DIR = "/out"
WORKDIR = "/workspace"
# Pinned tags only; pr-review-sandbox-* are built from docker/sandbox/build.sh.
ALLOWED_IMAGES: frozenset[str] = frozenset(
    {
        "python:3.12-slim",
        "alpine:3.20",
        "pr-review-sandbox-python:3.12",
        "pr-review-sandbox-node:20",
        "pr-review-sandbox-go:1.22",
        "pr-review-sandbox-rust:1.83",
    }
)
_EXCLUDE = (".git", "askpass.sh")
_DEFAULT_ENV = {"HOME": "/tmp", "LANG": "C.UTF-8", "CI": "1"}  # noqa: S108 - in-container tmpfs
_TAG = re.compile(r"^[^:@\s]+:[\w][\w.-]*$")


class SandboxError(Exception):
    pass


class ImageNotAllowedError(SandboxError):
    pass


@dataclass(frozen=True)
class SandboxSpec:
    image: str = "python:3.12-slim"
    digest: str | None = None  # "sha256:..." verified against the local image when set
    mem_bytes: int = 512 << 20
    cpus: float = 1.0
    pids_limit: int = 128
    tmp_bytes: int = 64 << 20
    tmp_exec: bool = False  # compiled languages (go, cargo) must exec test binaries from /tmp
    out_bytes: int = 64 << 20
    max_output_bytes: int = 1 << 20  # per stream
    max_artifact_bytes: int = 16 << 20  # total collected from OUT_DIR
    max_repo_bytes: int = 1 << 30  # workspace copy limit
    env: Mapping[str, str] = field(default_factory=lambda: dict(_DEFAULT_ENV))
    allowed_images: frozenset[str] = ALLOWED_IMAGES


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool
    duration_s: float
    artifacts: dict[str, bytes]
    rootless: bool
    output_truncated: bool = False
    artifacts_rejected: bool = False


def _check_image(client: docker.DockerClient, spec: SandboxSpec) -> None:
    if spec.image not in spec.allowed_images or not _TAG.match(spec.image):
        raise ImageNotAllowedError(f"image not allowed: {spec.image}")
    if spec.image.endswith(":latest"):
        raise ImageNotAllowedError("latest tag is not allowed")
    try:
        img = client.images.get(spec.image)
    except ImageNotFound:
        img = client.images.pull(spec.image)
    if spec.digest and not any(d.endswith("@" + spec.digest) for d in img.attrs["RepoDigests"]):
        raise ImageNotAllowedError(f"digest mismatch for {spec.image}")


def _copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(
        src, dst, symlinks=True, ignore=shutil.ignore_patterns(*_EXCLUDE), dirs_exist_ok=True
    )
    for root, dirs, files in os.walk(dst):  # container uid must read it
        for n in dirs:
            if not (Path(root) / n).is_symlink():
                (Path(root) / n).chmod(0o755)
        for n in files:
            if not (Path(root) / n).is_symlink():
                (Path(root) / n).chmod(0o755 if (Path(root) / n).stat().st_mode & 0o100 else 0o644)
    dst.chmod(0o755)


def _extract(chunks: Iterable[bytes], cap: int) -> dict[str, bytes]:
    """Safely read a tar of OUT_DIR: byte-capped, regular files only, no absolute/.. names."""
    raw = bytearray()
    for ch in chunks:
        raw += ch
        if len(raw) > cap + (4 << 20):  # tar header/padding slack
            raise SandboxError("artifact archive too large")
    found: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(bytes(raw)), mode="r:") as tf:
        for m in tf:
            parts = PurePosixPath(m.name).parts
            if m.name.startswith("/") or ".." in parts:
                raise SandboxError(f"unsafe artifact path: {m.name}")
            if m.isdir() and parts == (OUT_DIR.strip("/"),):
                continue
            if not (m.isreg() or m.isdir()):
                raise SandboxError(f"unsafe artifact member: {m.name}")
            if m.isdir():
                continue
            total += m.size
            if total > cap:
                raise SandboxError("artifacts exceed size cap")
            f = tf.extractfile(m)
            assert f is not None
            found["/".join(parts[1:])] = f.read()
    return found


def _read_stream(c: Container, which: str, cap: int, buf: bytearray, over: threading.Event) -> None:
    kw = {"stdout": which == "out", "stderr": which == "err"}
    try:
        for chunk in c.logs(stream=True, follow=True, **kw):
            buf += chunk
            if len(buf) > cap:
                over.set()
                c.kill()  # unbounded output: stop the producer
                return
    except Exception:  # noqa: BLE001 - container removed/killed under us
        return


def _text(buf: bytearray, cap: int) -> str:
    return redact(bytes(buf[:cap]).decode("utf-8", errors="replace"))


def run_in_sandbox(
    spec: SandboxSpec,
    workspace_dir: Path,
    argv: Sequence[str],
    *,
    timeout_s: float,
    client: docker.DockerClient | None = None,
) -> SandboxResult:
    """Run argv in a fresh container; blocking (use asyncio.to_thread from async code)."""
    if not argv:
        raise SandboxError("empty argv")
    if any(k.upper().startswith("PRR_") for k in spec.env):
        raise SandboxError("PRR_* variables may not enter the sandbox")
    cli = client or docker.from_env()
    _check_image(cli, spec)
    rootless = "name=rootless" in cli.info().get("SecurityOptions", [])

    run_dir = Path(tempfile.mkdtemp(prefix="prr-sbx-"))
    container: Container | None = None
    holder: Container | None = None
    volume = None
    try:
        if _size(workspace_dir) > spec.max_repo_bytes:
            raise SandboxError("workspace exceeds size limit")
        repo = run_dir / "repo"
        _copy_repo(workspace_dir, repo)
        # RAM-backed, size-capped volume: no host disk use. It is unmounted when its last
        # container stops, so a idle holder keeps it alive for artifact extraction.
        volume = cli.volumes.create(
            labels={LABEL: "1"},
            driver="local",
            driver_opts={
                "type": "tmpfs",
                "device": "tmpfs",
                "o": f"size={spec.out_bytes},uid=65534,gid=65534,noexec,nosuid,nodev",
            },
        )
        holder = cli.containers.create(
            spec.image,
            entrypoint="sleep",
            command=["86400"],
            labels={LABEL: "1"},
            user=UID,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            network_mode="none",
            mem_limit=64 << 20,
            memswap_limit=64 << 20,
            pids_limit=8,
            volumes={volume.name: {"bind": OUT_DIR, "mode": "ro"}},
        )
        holder.start()
        tmpfs = ("exec" if spec.tmp_exec else "noexec") + ",nosuid,nodev,size="
        container = cli.containers.create(
            spec.image,
            command=list(argv[1:]),
            entrypoint=argv[0],
            name=f"prr-sbx-{uuid.uuid4().hex[:12]}",
            labels={LABEL: "1"},
            user=UID,
            working_dir=WORKDIR,
            environment=dict(spec.env),
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            read_only=True,
            network_mode="none",
            pids_limit=spec.pids_limit,
            mem_limit=spec.mem_bytes,
            memswap_limit=spec.mem_bytes,
            nano_cpus=int(spec.cpus * 1e9),
            ulimits=[
                Ulimit(name="nofile", soft=1024, hard=1024),
                Ulimit(name="nproc", soft=512, hard=512),
                Ulimit(name="fsize", soft=64 << 20, hard=64 << 20),
            ],
            tmpfs={"/tmp": f"{tmpfs}{spec.tmp_bytes}"},  # noqa: S108
            volumes={
                str(repo): {"bind": WORKDIR, "mode": "ro"},
                volume.name: {"bind": OUT_DIR, "mode": "rw"},
            },
            auto_remove=False,
            detach=True,
        )
        start = time.monotonic()
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
        duration = time.monotonic() - start
        for t in readers:
            t.join(timeout=10)
        container.reload()
        # Docker's OOMKilled flag is unreliable on cgroup v2 when a child process is the one killed;
        # we only SIGKILL on timeout, so an untimed 137 (128 + SIGKILL) is the memory limit.
        oom = bool(container.attrs["State"].get("OOMKilled")) or (code == 137 and not timed_out)
        rejected = False
        try:
            artifacts = _extract(holder.get_archive(OUT_DIR)[0], spec.max_artifact_bytes)
        except SandboxError:
            artifacts, rejected = {}, True
        return SandboxResult(
            exit_code=code,
            stdout=_text(bufs[0], spec.max_output_bytes),
            stderr=_text(bufs[1], spec.max_output_bytes),
            timed_out=timed_out,
            oom_killed=oom,
            duration_s=duration,
            artifacts=artifacts,
            rootless=rootless,
            output_truncated=over.is_set(),
            artifacts_rejected=rejected,
        )
    finally:
        for c in (container, holder):
            if c is not None:
                try:
                    c.remove(force=True)
                except Exception:  # noqa: BLE001 - already gone
                    pass
        if volume is not None:
            try:
                volume.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(run_dir, ignore_errors=True)
