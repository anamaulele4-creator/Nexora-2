"""Fixtures dos testes do backend.

Os testes correm contra PostgreSQL e Redis REAIS (nada de mocks de BD):
  TEST_DATABASE_URL  (por defeito postgresql://postgres@127.0.0.1:5432/connector_ai_test)
  TEST_REDIS_URL     (por defeito redis://127.0.0.1:6379/15)
A base de dados de teste é apagada e recriada no início de cada execução.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "backend", ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from migrate import migrate  # noqa: E402

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/connector_ai_test")
TEST_REDIS = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
FRONTEND = "http://localhost:3000"
JWT_SECRET = "test-only-jwt-secret-0123456789abcdef0123456789"


def make_settings(**overrides):
    from core.config import Settings

    base = dict(
        environment="test",
        database_url=TEST_DB,
        redis_url=TEST_REDIS,
        jwt_secret=JWT_SECRET,
        frontend_url=FRONTEND,
        email_backend="memory",
        require_email_verification=True,
        auth_rate_limit_per_15min=1000,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


async def _recreate_database() -> None:
    parsed = urlparse(TEST_DB)
    dbname = parsed.path.lstrip("/")
    admin = urlunparse(parsed._replace(path="/postgres"))
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'drop database if exists "{dbname}" with (force)')
        await conn.execute(f'create database "{dbname}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def migrated_db() -> str:
    await _recreate_database()
    await migrate(TEST_DB, seed=True, verbose=False)
    return TEST_DB


@pytest.fixture(scope="session")
async def app(migrated_db):
    from api.main import create_app
    from authentication.email import MemoryEmailSender

    application = create_app(make_settings(), email_sender=MemoryEmailSender())
    async with LifespanManager(application):
        yield application


@pytest.fixture(autouse=True)
async def _clean_state(request):
    if "app" in request.fixturenames:
        app = request.getfixturevalue("app")
        app.state.email_sender.outbox.clear()
        app.state.settings.auth_rate_limit_per_15min = 1000
    r = Redis.from_url(TEST_REDIS)
    try:
        await r.flushdb()
    finally:
        await r.aclose()
    yield


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app, client=("203.0.113.10", 51000))
    async with AsyncClient(transport=transport, base_url="http://testserver",
                           headers={"Origin": FRONTEND}) as c:
        yield c


@pytest.fixture
async def db_conn(migrated_db):
    conn = await asyncpg.connect(migrated_db)
    try:
        yield conn
    finally:
        await conn.close()


_counter = {"n": 0}


def unique_email(prefix: str = "user") -> str:
    _counter["n"] += 1
    return f"{prefix}{_counter['n']}-{os.getpid()}@example.com".lower()


STRONG_PASSWORD = "Correct-Horse-42"


async def register_and_verify(client, app, email: str, password: str = STRONG_PASSWORD) -> str:
    r = await client.post("/api/auth/register", json={"email": email, "password": password, "display_name": "Test"})
    assert r.status_code == 201, r.text
    user_id = r.json()["user_id"]
    msg = [m for m in app.state.email_sender.outbox if m.to == email and m.kind == "email_verification"][-1]
    token = msg.link.split("token=")[1]
    r = await client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 204, r.text
    return user_id


async def login(client, email: str, password: str = STRONG_PASSWORD):
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r
