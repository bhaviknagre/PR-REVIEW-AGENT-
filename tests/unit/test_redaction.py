import json
import logging

import pytest

from pr_review.core import redaction
from pr_review.core.logging import JsonFormatter, bind
from pr_review.core.redaction import MASK, redact, register_secrets

GHP = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef123\n-----END RSA PRIVATE KEY-----"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
HIGH = "x9Qv3LmT7pZk2WnR8sYb4HcJ6dFg1AeU"


@pytest.fixture(autouse=True)
def _clean() -> None:
    redaction._values.clear()
    bind(delivery_id=None, job_id=None, stage=None)


@pytest.mark.parametrize(
    "secret",
    [
        GHP,
        "ghs_" + "A" * 10 + "b1" * 10,
        "github_pat_" + "11ABCDEFG0abcdefghij_KLMNOPqrstuvwxyz0123",
        PEM,
        AWS,
        HIGH,
    ],
)
def test_known_formats_redacted(secret: str) -> None:
    out = redact(f"before {secret} after")
    assert secret not in out and MASK in out
    assert out.startswith("before") and out.endswith("after")


def test_bearer_and_url_password() -> None:
    assert "abc123def456" not in redact("Authorization: Bearer abc123def456")
    assert redact("Authorization: Bearer abc123def456").startswith("Authorization: Bearer ")
    out = redact("postgres://user:hunter2pw@db:5432/x")
    assert "hunter2pw" not in out and "user:" in out and "@db" in out


def test_configured_value_redacted() -> None:
    register_secrets("plain-secret")
    assert redact("x plain-secret y") == f"x {MASK} y"


def test_not_over_redacted() -> None:
    sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    for s in [
        "Reviewing pull request for repo owner/name at line 42",
        f"commit {sha} job-123 abc123",
        "some_very_long_function_name_for_testing",
        "src/pr_review/core/redaction.py",
    ]:
        assert redact(s) == s


def _record(msg: str, args: tuple[object, ...] = (), **kw: object) -> logging.LogRecord:
    r = logging.LogRecord("t", logging.INFO, "f", 1, msg, args, kw.pop("exc_info", None))  # type: ignore[arg-type]
    r.__dict__.update(kw)
    return r


def test_json_log_redacts_everywhere_with_ids() -> None:
    register_secrets("cfg-secret-value")
    bind(delivery_id="d1", job_id="j1", stage="fetch")
    try:
        raise ValueError(f"boom {GHP}")
    except ValueError:
        import sys

        exc = sys.exc_info()
    raw = JsonFormatter().format(
        _record("msg %s", (AWS,), exc_info=exc, extra_f={"k": ["cfg-secret-value", PEM]}, tok=HIGH)
    )
    for s in (AWS, GHP, "cfg-secret-value", "MIIabc", HIGH):
        assert s not in raw
    d = json.loads(raw)
    assert (d["delivery_id"], d["job_id"], d["stage"]) == ("d1", "j1", "fetch")
    assert d["level"] == "INFO" and MASK in d["message"] and MASK in d["exc"]
