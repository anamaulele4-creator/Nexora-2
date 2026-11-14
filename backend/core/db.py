"""Acesso a PostgreSQL com dois modos explícitos.

* ``db.as_user(user_id)`` — transação com ``SET LOCAL ROLE authenticated`` e as claims
  JWT do utilizador. As políticas RLS aplicam-se: é o modo usado por TODAS as rotas
  que leem ou escrevem dados de um utilizador.
* ``db.service()`` — transação com o papel do serviço (ignora RLS). Reservado a
  operações de sistema: autenticação, auditoria, workers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.execute("set time zone 'UTC'")
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=self._min, max_size=self._max, init=_init_connection, command_timeout=30
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Base de dados não ligada")
        return self._pool

    @asynccontextmanager
    async def service(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def as_user(self, user_id: uuid.UUID | str) -> AsyncIterator[asyncpg.Connection]:
        claims = json.dumps({"sub": str(uuid.UUID(str(user_id))), "role": "authenticated"})
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("set local role authenticated")
                await conn.execute("select set_config('request.jwt.claims', $1, true)", claims)
                yield conn

    async def ping(self) -> bool:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("select 1") == 1

    async def latest_migration(self) -> str | None:
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval("select to_regclass('public.schema_migrations') is not null")
            if not exists:
                return None
            return await conn.fetchval("select max(version) from public.schema_migrations")
