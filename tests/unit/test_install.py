"""Real Docker + real PyPI: install through the allowlisting proxy, then test network-off."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from pr_review.runner.install import DEPS_DIR, egress_proxy, install_deps
from pr_review.runner.sandbox import LABEL, SandboxSpec, run_in_sandbox

CLIENT: docker.DockerClient | None
try:
    CLIENT = docker.from_env()
    CLIENT.ping()
    REASON = None
except Exception as e:  # noqa: BLE001
    CLIENT, REASON = None, f"docker unavailable: {e}"

pytestmark = [pytest.mark.docker, pytest.mark.skipif(bool(REASON), reason=REASON or "x")]
SPEC = SandboxSpec()
_PROBE = (
    "import socket,sys;s=socket.create_connection((sys.argv[1],443),timeout=5);print('CONNECTED')"
)


@pytest.fixture(autouse=True)
def _no_leftovers() -> Iterator[None]:
    yield
    assert CLIENT is not None
    f: dict[str, str | list[str] | bool] = {"label": f"{LABEL}=1"}
    assert not CLIENT.containers.list(all=True, filters=f)
    assert not CLIENT.networks.list(filters=f)
    assert not CLIENT.volumes.list(filters=f)


def _ws(tmp_path: Path, reqs: str) -> Path:
    (tmp_path / "requirements.txt").write_text(reqs)
    return tmp_path


def _probe_in_net(net: str, argv: list[str]) -> tuple[int, str]:
    assert CLIENT is not None
    c = CLIENT.containers.create(
        SPEC.image, entrypoint=argv[0], command=argv[1:], network_mode=net, labels={LABEL: "1"}
    )
    try:
        c.start()
        code = int(c.wait(timeout=60)["StatusCode"])
        return code, c.logs().decode()
    finally:
        c.remove(force=True)


def test_install_then_network_off_test_step(tmp_path: Path, pkg_index: str | None) -> None:
    r = install_deps(_ws(tmp_path, "six==1.16.0\n"), client=CLIENT, index_url=pkg_index)
    assert r.ok, r.stderr
    assert r.workspace is not None
    try:
        assert (r.workspace / DEPS_DIR / "six.py").is_file()
        env = {**SPEC.env, "PYTHONPATH": f"/workspace/{DEPS_DIR}"}
        t = SandboxSpec(env=env)
        ok = run_in_sandbox(
            t, r.workspace, ["python", "-c", "import six;print(six.__version__)"], timeout_s=60
        )
        assert ok.stdout.strip() == "1.16.0"
        net = run_in_sandbox(t, r.workspace, ["python", "-c", _PROBE, "pypi.org"], timeout_s=60)
        assert net.exit_code != 0
        assert "CONNECTED" not in net.stdout
    finally:
        shutil.rmtree(r.workspace, ignore_errors=True)


def test_proxy_allows_only_allowlist_and_no_direct_egress() -> None:
    assert CLIENT is not None
    with egress_proxy(CLIENT, SPEC) as (net, url):
        base = ["python", "-c"]
        # direct egress without proxy fails on the internal network
        code, out = _probe_in_net(net, [*base, _PROBE, "1.1.1.1"])
        assert code != 0
        assert "CONNECTED" not in out
        fetch = (
            "import urllib.request,sys\n"
            "try:\n urllib.request.urlopen(sys.argv[1],timeout=20);print('FETCHED')\n"
            "except Exception as e:\n print('ERR',e)\n"
        )
        env = ["env", f"HTTPS_PROXY={url}", "python", "-c", fetch]
        code, out = _probe_in_net(net, [*env, "https://example.com/"])
        assert "FETCHED" not in out
        assert "403" in out
        code, out = _probe_in_net(net, [*env, "https://pypi.org/simple/six/"])
        assert "FETCHED" in out


def test_non_allowlisted_index_is_rejected(tmp_path: Path) -> None:
    ws = _ws(tmp_path, "--index-url https://example.com/simple\nsix==1.16.0\n")
    r = install_deps(ws, client=CLIENT)
    assert not r.ok
    assert r.workspace is None
    assert r.notice == "dependency install failed: registry not allowlisted"


def test_missing_requirements_and_unsupported_manager(tmp_path: Path) -> None:
    r = install_deps(tmp_path, client=CLIENT)
    assert not r.ok
    assert "requirements.txt" in r.notice
    with pytest.raises(Exception, match="unsupported"):
        install_deps(tmp_path, manager="bundler", client=CLIENT)


def test_timeout_notice(tmp_path: Path) -> None:
    r = install_deps(_ws(tmp_path, "six==1.16.0\n"), timeout_s=0.5, client=CLIENT)
    assert not r.ok
    assert r.notice == "dependency install failed: timed out"
