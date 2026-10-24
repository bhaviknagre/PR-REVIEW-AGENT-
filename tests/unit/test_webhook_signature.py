import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from pr_review.api import webhook
from pr_review.api.app import create_app
from pr_review.core.settings import Settings

SECRET = "whsec-test-secret"
BODY = b'{"action": "opened"}'


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def boom(raw: bytes) -> None:
        raise AssertionError("parser invoked")

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        github_app_id=1,
        github_private_key="pk",  # type: ignore[arg-type]
        github_webhook_secret=SECRET,  # type: ignore[arg-type]
        database_url="postgresql://u:p@h/db",  # type: ignore[arg-type]
        redis_url="redis://h/0",  # type: ignore[arg-type]
    )
    monkeypatch.setattr(webhook, "parse_payload", boom)
    return TestClient(create_app(settings))


def post(client: TestClient, body: bytes, sig: str | None) -> int:
    headers = {} if sig is None else {"X-Hub-Signature-256": sig}
    return client.post("/webhook/github", content=body, headers=headers).status_code


def test_valid_signature_accepted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # restore the real parser for the accepted path
    assert post(client, BODY, sign(BODY)) == 202


def test_malformed_json_after_valid_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    assert post(client, b"{nope", sign(b"{nope")) == 400


@pytest.mark.parametrize(
    "body,sig",
    [
        (BODY + b" ", sign(BODY)),  # tampered body
        (BODY, sign(BODY, "other-secret")),  # wrong secret
        (BODY, None),  # missing header
        (BODY, "sha1=" + sign(BODY)[7:]),  # wrong prefix
        (BODY, sign(BODY)[7:]),  # no prefix
        (BODY, "sha256=" + "zz" * 32),  # non-hex
        (BODY, "sha256="),  # empty digest
        (b"", sign(BODY)),  # empty body, wrong sig
        (b"", None),
    ],
)
def test_rejected_with_generic_401_and_no_parse(
    client: TestClient, body: bytes, sig: str | None
) -> None:
    headers = {} if sig is None else {"X-Hub-Signature-256": sig}
    r = client.post("/webhook/github", content=body, headers=headers)
    assert r.status_code == 401
    assert r.text == "Unauthorized"  # parser would have raised AssertionError otherwise


def test_oversized_with_content_length(client: TestClient) -> None:
    big = b"x" * (webhook.MAX_BODY_BYTES + 1)
    assert post(client, big, sign(big)) == 413


def test_oversized_without_content_length(client: TestClient) -> None:
    big = b"x" * (webhook.MAX_BODY_BYTES + 1)

    def gen():  # type: ignore[no-untyped-def]
        yield big

    r = client.post("/webhook/github", content=gen(), headers={"X-Hub-Signature-256": sign(big)})
    assert r.status_code == 413


def test_understated_content_length_still_limited(client: TestClient) -> None:
    big = b"x" * (webhook.MAX_BODY_BYTES + 1)
    r = client.post(
        "/webhook/github",
        content=big,
        headers={"X-Hub-Signature-256": sign(big), "Content-Length": "5"},
    )
    assert r.status_code in (400, 413)
