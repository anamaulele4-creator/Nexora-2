from __future__ import annotations

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from conftest import make_settings


async def test_health_is_ok(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "X-Request-ID" in r.headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_ready_checks_database_migrations_and_redis(client):
    r = await client.get("/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"]
    assert body["checks"]["migrations"]["current"] == body["checks"]["migrations"]["expected"]
    assert body["checks"]["redis"]["ok"]


async def test_ready_reports_redis_down(migrated_db):
    from api.main import create_app

    app = create_app(make_settings(redis_url="redis://127.0.0.1:6399/0"))
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"]["ok"] is False
    assert r.json()["checks"]["database"]["ok"] is True
