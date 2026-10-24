"""Allowlist composition and index-URL validation (pure logic, no Docker)."""

import pytest

from pr_review.core.settings import Settings
from pr_review.runner.install import ECOSYSTEM_HOSTS, allowlist, parse_index_url
from pr_review.runner.sandbox import SandboxError

REQ = {
    "github_app_id": 1,
    "github_private_key": "k",
    "github_webhook_secret": "s",
    "database_url": "postgresql://u@h/d",
    "redis_url": "redis://h",
}


def test_default_is_real_registries_only() -> None:
    assert allowlist("pip") == (("pypi.org", "files.pythonhosted.org"), ())
    assert allowlist("npm")[0] == ("registry.npmjs.org",)
    assert allowlist("go")[0] == ("proxy.golang.org", "sum.golang.org")
    assert allowlist("cargo")[0] == ("crates.io", "static.crates.io", "index.crates.io")


def test_http_override_adds_exactly_one_plain_target() -> None:
    connect, plain = allowlist("pip", "http://172.17.0.5:5000/index/")
    assert connect == ECOSYSTEM_HOSTS["pip"]
    assert plain == ("172.17.0.5:5000",)


def test_https_override_adds_exactly_one_connect_target() -> None:
    connect, plain = allowlist("pip", "https://Mirror.Example:8443/simple")
    assert connect == (*ECOSYSTEM_HOSTS["pip"], "mirror.example:8443")
    assert plain == ()
    assert allowlist("pip", "https://mirror.example/simple")[0][-1] == "mirror.example:443"


@pytest.mark.parametrize("manager", ["npm", "go", "cargo"])
def test_override_never_widens_other_ecosystems(manager: str) -> None:
    assert allowlist(manager, "http://10.0.0.1:5000/") == (ECOSYSTEM_HOSTS[manager], ())


@pytest.mark.parametrize(
    "url", ["ftp://h/x", "file:///etc", "http://", "h:5000/x", "http://u:p@h/x", "javascript:1"]
)
def test_bad_index_urls_rejected(url: str) -> None:
    with pytest.raises(SandboxError):
        parse_index_url(url)
    with pytest.raises(ValueError, match="http"):
        Settings(**REQ, sandbox_pip_index_url=url)  # type: ignore[arg-type]


def test_setting_default_none_and_accepts_http() -> None:
    assert Settings(**REQ).sandbox_pip_index_url is None  # type: ignore[arg-type]
    s = Settings(**REQ, sandbox_pip_index_url="http://h:5000/index/")  # type: ignore[arg-type]
    assert s.sandbox_pip_index_url == "http://h:5000/index/"
