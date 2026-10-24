import asyncio
import logging
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from pr_review.connectors.github.auth import (
    Forbidden,
    GitHubAppAuth,
    GitHubAuthError,
    NotFound,
    Unauthorized,
    Unavailable,
)
from pr_review.core import redaction

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUB = _KEY.public_key()
T0 = 1_800_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Env:
    def __init__(self, statuses: list[int | Exception] | None = None) -> None:
        self.now = T0
        self.calls: list[httpx.Request] = []
        self.statuses = statuses or []
        self.sleeps: list[float] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        n = len(self.calls)
        step = self.statuses[n - 1] if n <= len(self.statuses) else 201
        if isinstance(step, Exception):
            raise step
        if step != 201:
            return httpx.Response(step, json={"message": "nope"})
        tok = f"ghs_Abc123Xyz789Tok{n:04d}QrStUvWx"
        return httpx.Response(201, json={"token": tok, "expires_at": _iso(self.now + 3600)})

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)

    def auth(self, **kw: object) -> GitHubAppAuth:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return GitHubAppAuth(
            7,
            SecretStr(PRIV),
            client,
            clock=lambda: self.now,
            sleep=self.sleep,
            **kw,  # type: ignore[arg-type]
        )


def test_jwt_claims_and_signature() -> None:
    env = Env()
    claims = jwt.decode(
        env.auth().app_jwt(),
        PUB,
        algorithms=["RS256"],
        options={"verify_iat": False, "verify_exp": False},
    )
    assert claims["iss"] == "7"
    assert claims["iat"] == int(T0) - 60
    assert claims["exp"] - int(T0) <= 600


def test_cache_refresh_and_request_shape() -> None:
    env = Env()
    auth = env.auth(permissions={"contents": "read"})

    async def run() -> None:
        a = await auth.token(1)
        assert await auth.token(1) is a
        assert len(env.calls) == 1
        assert env.calls[0].url.path == "/app/installations/1/access_tokens"
        assert b'"contents"' in env.calls[0].content
        env.now += 3600 - 299  # inside refresh margin
        b = await auth.token(1)
        assert b.reveal() != a.reveal() and len(env.calls) == 2
        await auth.token(2)
        assert len(env.calls) == 3

    asyncio.run(run())


def test_single_mint_under_concurrency() -> None:
    env = Env()
    auth = env.auth()

    async def run() -> None:
        toks = await asyncio.gather(*(auth.token(1) for _ in range(20)))
        assert len({t.reveal() for t in toks}) == 1

    asyncio.run(run())
    assert len(env.calls) == 1


def test_token_redacted_and_masked(caplog: pytest.LogCaptureFixture) -> None:
    env = Env()
    tok = asyncio.run(env.auth().token(1))
    secret = tok.reveal()
    assert secret not in repr(tok) and secret not in str(tok)
    assert secret not in redaction.redact(f"leaked {secret}")
    caplog.set_level(logging.INFO)
    logging.getLogger("x").info("token %s", tok)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("status", "exc"),
    [(401, Unauthorized), (403, Forbidden), (404, NotFound), (400, GitHubAuthError)],
)
def test_error_mapping_no_retry(status: int, exc: type[Exception]) -> None:
    env = Env([status])
    with pytest.raises(exc) as ei:
        asyncio.run(env.auth().token(1))
    assert len(env.calls) == 1
    assert "eyJ" not in str(ei.value)


def test_transient_retried_with_backoff() -> None:
    env = Env([503, httpx.ReadTimeout("t"), 201])
    tok = asyncio.run(env.auth().token(1))
    assert tok.reveal() and env.sleeps == [0.5, 1.0]


def test_transient_exhausted() -> None:
    env = Env([500, 502, 503])
    with pytest.raises(Unavailable) as ei:
        asyncio.run(env.auth().token(1))
    assert len(env.calls) == 3
    assert "ghs_" not in str(ei.value)


def test_timeout_message_has_no_secrets() -> None:
    env = Env([httpx.ConnectTimeout("Bearer eyJsecret")] * 3)
    with pytest.raises(Unavailable) as ei:
        asyncio.run(env.auth().token(1))
    assert "eyJ" not in str(ei.value)
