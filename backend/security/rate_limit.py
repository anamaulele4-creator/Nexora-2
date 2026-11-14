"""Limite de pedidos em janela fixa, guardado em Redis."""

from __future__ import annotations

import hashlib
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger("connector_ai.security")


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "rl") -> None:
        self._redis = redis
        self._prefix = prefix

    async def hit(self, bucket: str, identity: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Regista uma tentativa. Devolve (permitido, segundos até reset)."""
        ident = hashlib.sha256(identity.lower().encode()).hexdigest()[:32]
        key = f"{self._prefix}:{bucket}:{ident}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds, nx=True)
                pipe.ttl(key)
                count, _, ttl = await pipe.execute()
        except RedisError:
            # Redis indisponível: não bloqueia o serviço, mas fica registado
            # (o /ready reporta o Redis em baixo).
            log.warning("rate limiter sem Redis; pedido permitido", extra={"fields": {"bucket": bucket}})
            return True, 0
        return int(count) <= limit, max(int(ttl), 0)
