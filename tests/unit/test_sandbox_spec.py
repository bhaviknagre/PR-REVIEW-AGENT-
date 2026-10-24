"""Real-container sandbox tests (no mocks). Skipped only when the Docker daemon is unreachable."""

import io
import os
import tarfile
import time
from pathlib import Path

import docker
import pytest

from pr_review.runner.sandbox import (
    LABEL,
    ImageNotAllowedError,
    SandboxError,
    SandboxSpec,
    _extract,
    run_in_sandbox,
)

try:
    CLIENT = docker.from_env()
    CLIENT.ping()
    REASON = ""
except Exception as e:  # noqa: BLE001
    REASON = f"Docker daemon unreachable: {e}"

pytestmark = [pytest.mark.docker, pytest.mark.skipif(bool(REASON), reason=REASON or "x")]

PY = ["python", "-c"]
SPEC = SandboxSpec()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    (w / ".git").mkdir(parents=True)
    (w / ".git" / "config").write_text("[remote]\n")
    (w / "askpass.sh").write_text("#!/bin/sh\n")
    (w / "t.py").write_text("print(1)\n")
    return w


def run(ws: Path, code: str, spec: SandboxSpec = SPEC, timeout: float = 30):  # noqa: ANN201
    return run_in_sandbox(spec, ws, [*PY, code], timeout_s=timeout)


@pytest.fixture(autouse=True)
def no_leftovers():  # noqa: ANN201
    yield
    assert CLIENT.containers.list(all=True, filters={"label": f"{LABEL}=1"}) == []
    assert CLIENT.volumes.list(filters={"label": f"{LABEL}=1"}) == []


def test_runs_python(ws: Path) -> None:
    r = run(ws, "import sys;print('hi');sys.stderr.write('e');sys.exit(3)")
    assert (r.exit_code, r.stdout.strip(), r.stderr, r.timed_out) == (3, "hi", "e", False)
    assert isinstance(r.rootless, bool)


def test_non_root_and_no_caps(ws: Path) -> None:
    r = run(ws, "import os;print(os.getuid());print(open('/proc/self/status').read())")
    assert r.stdout.splitlines()[0] == "65534"
    assert "CapEff:\t0000000000000000" in r.stdout


def test_network_blocked(ws: Path) -> None:
    code = (
        "import socket;s=socket.socket();s.settimeout(3)\n"
        "try: s.connect(('1.1.1.1',53));print('CONNECTED')\n"
        "except OSError: print('blocked')\n"
        "try: socket.gethostbyname('example.com');print('RESOLVED')\n"
        "except OSError: print('noresolve')"
    )
    assert run(ws, code).stdout.split() == ["blocked", "noresolve"]


def test_host_env_not_visible(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRR_GIT_TOKEN", "secret-marker-xyz")
    monkeypatch.setenv("OTHER_HOST_VAR", "secret-marker-xyz")
    r = run(ws, "import os;print(sorted(os.environ))")
    assert "secret-marker" not in r.stdout
    assert "PRR_GIT_TOKEN" not in r.stdout and "OTHER_HOST_VAR" not in r.stdout
    assert os.environ["PRR_GIT_TOKEN"]  # still set on the host


def test_prr_env_rejected(ws: Path) -> None:
    with pytest.raises(SandboxError):
        run(ws, "pass", SandboxSpec(env={"PRR_GIT_TOKEN": "x"}))


def test_filesystem_read_only_and_writable_paths(ws: Path) -> None:
    code = (
        "def w(p):\n"
        "    try: open(p,'w').write('x');return 'ok'\n"
        "    except OSError: return 'denied'\n"
        "print(w('/etc/x'),w('/workspace/x'),w('/tmp/x'),w('/out/a.xml'))"
    )
    r = run(ws, code)
    assert r.stdout.split() == ["denied", "denied", "ok", "ok"]
    assert r.artifacts == {"a.xml": b"x"}


def test_askpass_and_git_absent(ws: Path) -> None:
    r = run(ws, "import os;print(sorted(os.listdir('/workspace')))")
    assert r.stdout.strip() == "['t.py']"
    assert (ws / "askpass.sh").exists()  # source untouched


def test_oom_killed(ws: Path) -> None:
    r = run(ws, "b=[]\nwhile True: b.append(bytearray(10**7))", SandboxSpec(mem_bytes=64 << 20))
    assert r.oom_killed and r.exit_code != 0


def test_fork_bomb_bounded(ws: Path) -> None:
    code = (
        "import os,time\n"
        "n=0\n"
        "for _ in range(2000):\n"
        "    try:\n"
        "        if os.fork()==0: time.sleep(60)\n"
        "        n+=1\n"
        "    except OSError: break\n"
        "print('forks',n)"
    )
    r = run(ws, code, SandboxSpec(pids_limit=32), timeout=20)
    assert r.stdout.startswith("forks") and int(r.stdout.split()[1]) < 32


def test_timeout(ws: Path) -> None:
    t = time.monotonic()
    r = run(ws, "while True: pass", timeout=3)
    assert r.timed_out and time.monotonic() - t < 3 + 10


def test_huge_stdout_capped(ws: Path) -> None:
    r = run(ws, "while True: print('x'*1000)", SandboxSpec(max_output_bytes=10_000))
    assert len(r.stdout) <= 10_000 and r.output_truncated


def test_output_redacted(ws: Path) -> None:
    r = run(ws, "print('ghp_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3')")
    assert "ghp_" not in r.stdout


def test_image_policy(ws: Path) -> None:
    for bad in ("python:latest", "ubuntu:24.04", "python"):
        with pytest.raises(ImageNotAllowedError):
            run(ws, "pass", SandboxSpec(image=bad))
    with pytest.raises(ImageNotAllowedError):
        run(ws, "pass", SandboxSpec(digest="sha256:" + "0" * 64))
    digest = CLIENT.images.get(SPEC.image).attrs["RepoDigests"][0].split("@")[1]
    assert run(ws, "print(1)", SandboxSpec(digest=digest)).stdout.strip() == "1"


def test_alpine_image(ws: Path) -> None:
    r = run_in_sandbox(SandboxSpec(image="alpine:3.20"), ws, ["id", "-u"], timeout_s=30)
    assert r.stdout.strip() == "65534"


def test_out_cap_enospc_and_extracted(ws: Path) -> None:
    code = (
        "open('/out/junit.xml','w').write('<x/>')\n"
        "import os;os.makedirs('/out/cov');open('/out/cov/c.xml','w').write('c')\n"
        "try:\n"
        "    open('/out/big','wb').write(b'x'*(3<<20));print('WROTE')\n"
        "except OSError as e: print('ENOSPC' if e.errno==28 else e.errno)"
    )
    r = run(ws, code, SandboxSpec(out_bytes=1 << 20))
    assert r.stdout.strip() == "ENOSPC"
    assert r.artifacts["junit.xml"] == b"<x/>" and r.artifacts["cov/c.xml"] == b"c"
    assert sum(map(len, r.artifacts.values())) <= 1 << 20


def _tar(add) -> bytes:  # noqa: ANN001
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        add(t)
    return b.getvalue()


def test_extract_rejects_unsafe_members() -> None:
    def trav(t: tarfile.TarFile) -> None:
        i = tarfile.TarInfo("out/../../evil")
        i.size = 1
        t.addfile(i, io.BytesIO(b"x"))

    def sym(t: tarfile.TarFile) -> None:
        i = tarfile.TarInfo("out/l")
        i.type, i.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(i)

    def hard(t: tarfile.TarFile) -> None:
        i = tarfile.TarInfo("out/h")
        i.type, i.linkname = tarfile.LNKTYPE, "out/a"
        t.addfile(i)

    for add in (trav, sym, hard):
        with pytest.raises(SandboxError):
            _extract([_tar(add)], 1 << 20)
    with pytest.raises(SandboxError):
        _extract([b"\0" * (6 << 20)], 1 << 20)


def test_container_symlink_artifact_rejected(ws: Path) -> None:
    r = run(ws, "import os;os.symlink('/etc/passwd','/out/l')")
    assert r.artifacts == {} and r.artifacts_rejected


def test_oversized_repo_rejected(ws: Path) -> None:
    with pytest.raises(SandboxError):
        run(ws, "pass", SandboxSpec(max_repo_bytes=1))


_SANDBOX_DIR = Path(__file__).parents[2] / "docker" / "sandbox"


@pytest.fixture(scope="module")
def lang_images() -> None:
    import subprocess

    r = subprocess.run([str(_SANDBOX_DIR / "build.sh")], capture_output=True, text=True)  # noqa: S603
    if r.returncode:
        pytest.skip(f"sandbox image build failed: {r.stderr[-300:]}")


def _lang_ws(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_node_image_runs_jest(lang_images: None, tmp_path: Path) -> None:
    w = _lang_ws(
        tmp_path,
        {
            "package.json": '{"name":"x","version":"1.0.0"}',
            "a.test.js": "test('a',()=>{expect(1+1).toBe(2)})",
        },
    )
    r = run_in_sandbox(
        SandboxSpec(image="pr-review-sandbox-node:20"), w, ["jest", "--ci"], timeout_s=120
    )
    assert r.exit_code == 0, r.stderr
    assert "1 passed" in r.stderr + r.stdout


def test_go_image_runs_go_test(lang_images: None, tmp_path: Path) -> None:
    w = _lang_ws(
        tmp_path,
        {
            "go.mod": "module x\n\ngo 1.22\n",
            "a_test.go": 'package x\nimport "testing"\nfunc TestA(t *testing.T){}\n',
        },
    )
    r = run_in_sandbox(
        SandboxSpec(image="pr-review-sandbox-go:1.22", tmp_exec=True),
        w,
        ["go", "test", "-json", "./..."],
        timeout_s=180,
    )
    assert r.exit_code == 0, r.stderr
    assert '"Action":"pass"' in r.stdout


def test_rust_image_runs_cargo_test(lang_images: None, tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    w = _lang_ws(
        tmp_path,
        {
            "Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n',
            # workspace is read-only, so cargo cannot create the lockfile
            "Cargo.lock": (
                "# This file is automatically @generated by Cargo.\n"
                "# It is not intended for manual editing.\n"
                'version = 4\n\n[[package]]\nname = "x"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "#[test]\nfn a() { assert_eq!(1 + 1, 2); }\n",
        },
    )
    spec = SandboxSpec(image="pr-review-sandbox-rust:1.83", tmp_exec=True, tmp_bytes=256 << 20)
    r = run_in_sandbox(spec, w, ["cargo", "test", "--offline", "--locked"], timeout_s=240)
    assert r.exit_code == 0, r.stderr
    assert "1 passed" in r.stdout
