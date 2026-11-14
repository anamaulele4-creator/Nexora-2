from __future__ import annotations

from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
