"""Redis fixed-window rate limiter for the webhook endpoint (FR-4, AC-3).

Wiring requirement: attach `RateLimiter.check_ip` as a route dependency (runs first, needs no
body). Call `check_installation` ONLY after the HMAC signature is verified and the installation
id parsed; unverified bodies must never be parsed, so the installation key must not be derived
from them. Redis outages fail open by default (logged as a warning, no IP in the message).
"""

import logging
from collections.abc import Collection

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        ip_limit: int = 60,
        installation_limit: int = 120,
        window_seconds: int = 60,
        trusted_proxies: Collection[str] = (),
        fail_open: bool = True,
    ) -> None:
        self._redis = redis
        self._ip_limit = ip_limit
        self._installation_limit = installation_limit
        self._window = window_seconds
        self._trusted = frozenset(trusted_proxies)
        self._fail_open = fail_open

    def client_ip(self, request: Request) -> str:
        """Peer address; X-Forwarded-For is honoured only when the peer is a trusted proxy."""
        peer = request.client.host if request.client else "unknown"
        if peer not in self._trusted:
            return peer
        hops = [
            h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()
        ]
        for hop in reversed(hops):  # rightmost untrusted hop is the real client
            if hop not in self._trusted:
                return hop
        return peer

    async def check_ip(self, request: Request) -> None:
        await self._hit(f"rl:ip:{self.client_ip(request)}", self._ip_limit)

    async def check_installation(self, installation_id: int) -> None:
        await self._hit(f"rl:inst:{installation_id}", self._installation_limit)

    async def _hit(self, key: str, limit: int) -> None:
        try:
            # MULTI/EXEC: INCR + EXPIRE NX + TTL run atomically, so the window always has a TTL.
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
                pipe.ttl(key)
                count, _, ttl = await pipe.execute()
        except (RedisError, OSError):
            if self._fail_open:
                log.warning("rate limiter store unavailable; failing open")
                return
            raise HTTPException(status_code=503, detail="rate limiter unavailable") from None
        if count > limit:
            retry = max(int(ttl), 1)
            raise HTTPException(
                status_code=429, detail="rate limit exceeded", headers={"Retry-After": str(retry)}
            )
