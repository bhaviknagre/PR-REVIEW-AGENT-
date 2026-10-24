"""Shared fixtures: a real caching PyPI mirror so sandbox installs don't depend on PyPI speed.

The `pkg_index` container (epicwink/proxpi, pinned) is removed at session end, but its cache lives
in the named Docker volume `pr-review-test-pkgcache`, which is deliberately KEPT: the first run
downloads wheels once, later runs are served locally. Delete it with
`docker volume rm pr-review-test-pkgcache`. Set PR_REVIEW_TEST_NO_PKGCACHE=1 to use real PyPI.
"""

import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator

import docker
import pytest

PROXPI_IMAGE = "epicwink/proxpi:1.3.1"
CACHE_VOLUME = "pr-review-test-pkgcache"


@pytest.fixture(scope="session")
def pkg_index() -> Iterator[str | None]:
    """URL of the caching index reachable from sandbox/proxy containers (bridge IP), or None."""
    if os.environ.get("PR_REVIEW_TEST_NO_PKGCACHE"):
        yield None
        return
    try:
        cli = docker.from_env()
        cli.ping()
    except Exception:  # noqa: BLE001 - docker tests skip themselves
        yield None
        return
    try:
        cli.images.get(PROXPI_IMAGE)
    except docker.errors.ImageNotFound:
        cli.images.pull(PROXPI_IMAGE)
    c = cli.containers.run(
        PROXPI_IMAGE,
        name=f"prr-test-pkgcache-{uuid.uuid4().hex[:8]}",
        detach=True,
        environment={"PROXPI_CACHE_DIR": "/var/cache/proxpi"},
        volumes={CACHE_VOLUME: {"bind": "/var/cache/proxpi", "mode": "rw"}},
        ports={"5000/tcp": ("127.0.0.1", None)},
    )
    try:
        c.reload()
        host_port = c.ports["5000/tcp"][0]["HostPort"]
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{host_port}/health", timeout=5).read()
                break
            except urllib.error.HTTPError:
                break  # the server answered
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("package cache did not start")
        ip = c.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        yield f"http://{ip}:5000/index/"
    finally:
        c.remove(force=True, v=True)  # container + anonymous volumes; the named cache stays
