import asyncio

import fakeredis
import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

from pr_review.api.ratelimit import RateLimiter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def req(peer: str = "1.2.3.4", xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1)})


def make(**kw: object) -> RateLimiter:
    redis = fakeredis.FakeAsyncRedis()
    return RateLimiter(redis, **kw)  # type: ignore[arg-type]


async def test_under_and_over_limit_with_retry_after() -> None:
    rl = make(ip_limit=2, window_seconds=30)
    await rl.check_ip(req())
    await rl.check_ip(req())
    with pytest.raises(HTTPException) as e:
        await rl.check_ip(req())
    assert e.value.status_code == 429
    assert e.value.headers is not None
    assert 1 <= int(e.value.headers["Retry-After"]) <= 30


async def test_window_reset() -> None:
    rl = make(ip_limit=1, window_seconds=1)
    await rl.check_ip(req())
    with pytest.raises(HTTPException):
        await rl.check_ip(req())
    await asyncio.sleep(1.1)
    await rl.check_ip(req())


async def test_ip_and_installation_independent() -> None:
    rl = make(ip_limit=1, installation_limit=1)
    await rl.check_ip(req("1.1.1.1"))
    await rl.check_ip(req("2.2.2.2"))
    await rl.check_installation(1)
    await rl.check_installation(2)
    with pytest.raises(HTTPException):
        await rl.check_installation(1)
    with pytest.raises(HTTPException):
        await rl.check_ip(req("1.1.1.1"))


async def test_xff_ignored_unless_trusted() -> None:
    rl = make(ip_limit=1, trusted_proxies=["10.0.0.1"])
    # untrusted peer: spoofed XFF cannot evade the limit
    await rl.check_ip(req("9.9.9.9", xff="5.5.5.1"))
    with pytest.raises(HTTPException):
        await rl.check_ip(req("9.9.9.9", xff="5.5.5.2"))
    # trusted proxy: XFF client is the key; a client-injected left entry is ignored
    assert rl.client_ip(req("10.0.0.1", xff="6.6.6.6, 7.7.7.7")) == "7.7.7.7"
    assert rl.client_ip(req("10.0.0.1", xff="7.7.7.7, 10.0.0.1")) == "7.7.7.7"
    assert rl.client_ip(req("10.0.0.1")) == "10.0.0.1"


class Down:
    def pipeline(self, transaction: bool = True) -> "Down":
        raise RedisConnectionError("down")


async def test_redis_down_fails_open_by_default(caplog: pytest.LogCaptureFixture) -> None:
    rl = RateLimiter(Down())  # type: ignore[arg-type]
    await rl.check_ip(req("3.3.3.3"))
    assert "failing open" in caplog.text
    assert "3.3.3.3" not in caplog.text


async def test_redis_down_fail_closed_option() -> None:
    rl = RateLimiter(Down(), fail_open=False)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as e:
        await rl.check_installation(1)
    assert e.value.status_code == 503
