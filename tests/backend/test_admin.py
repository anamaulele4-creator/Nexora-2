"""Papel de admin, API de administração e fornecedores de IA."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from conftest import ADMIN_EMAIL, login, register_and_verify, unique_email

FAKE_GEMINI_KEY = "fake-gemini-key-for-tests-WXYZ"  # não é uma chave real
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_admin_ready = {"done": False}


@pytest.fixture(autouse=True)
async def _clean_providers(db_conn):
    await db_conn.execute("delete from ai_providers")
    yield


async def admin_client(app, make_client):
    c = await make_client()
    if not _admin_ready["done"]:
        await register_and_verify(c, app, ADMIN_EMAIL)
        _admin_ready["done"] = True
    await login(c, ADMIN_EMAIL)
    return c


async def user_client(app, make_client):
    c = await make_client()
    email = unique_email("plain")
    await register_and_verify(c, app, email)
    await login(c, email)
    return c


ADMIN_ROUTES = [
    ("GET", "/api/admin/stats", None),
    ("GET", "/api/admin/users", None),
    ("GET", "/api/admin/providers", None),
    ("PUT", "/api/admin/providers/gemini", {"model": "x-model"}),
    ("POST", "/api/admin/providers/gemini/test", None),
]


async def test_admin_routes_require_admin(app, make_client):
    anon = await make_client()
    for method, path, body in ADMIN_ROUTES:
        r = await anon.request(method, path, json=body)
        assert r.status_code == 401, (path, r.text)
    plain = await user_client(app, make_client)
    me = (await plain.get("/api/auth/me")).json()
    assert me["is_admin"] is False
    for method, path, body in ADMIN_ROUTES:
        r = await plain.request(method, path, json=body)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "forbidden"


async def test_admin_email_becomes_admin_on_verification(app, make_client, db_conn):
    c = await admin_client(app, make_client)
    me = (await c.get("/api/auth/me")).json()
    assert me["is_admin"] is True
    assert await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'auth.admin_granted'""", ADMIN_EMAIL) == 1
    assert (await c.get("/api/admin/stats")).status_code == 200


async def test_unverified_admin_email_is_not_admin(app, client, db_conn):
    email = unique_email("pending-admin")
    app.state.settings.admin_emails = [ADMIN_EMAIL, email]
    r = await client.post("/api/auth/register", json={"email": email, "password": "Correct-Horse-42"})
    assert r.status_code == 201
    assert await db_conn.fetchval("select is_admin from users where email = $1", email) is False


async def test_existing_session_promoted_when_added_to_admin_emails(app, make_client, db_conn):
    c = await make_client()
    email = unique_email("late-admin")
    await register_and_verify(c, app, email)
    await login(c, email)
    assert (await c.get("/api/admin/stats")).status_code == 403
    app.state.settings.admin_emails = [ADMIN_EMAIL, email]
    assert (await c.get("/api/admin/stats")).status_code == 200
    assert await db_conn.fetchval("select is_admin from users where email = $1", email) is True


async def test_migration_0005_promotes_owner_account(db_conn):
    sql = (Path(__file__).resolve().parents[2] / "database" / "migrations" / "0005_admin_ai_chat.sql").read_text()
    update_stmt = sql[sql.index("update public.users"):].split(";")[0]
    assert "'garenadario3@gmail.com'" in update_stmt
    from authentication.accounts import create_account

    tr = db_conn.transaction()
    await tr.start()
    try:
        await db_conn.execute("delete from users where email = 'garenadario3@gmail.com'")
        uid = await create_account(db_conn, email="garenadario3@gmail.com", display_name=None, email_verified=True)
        other = await create_account(db_conn, email=unique_email("not-owner"), display_name=None, email_verified=True)
        await db_conn.execute(update_stmt)
        assert await db_conn.fetchval("select is_admin from users where id = $1", uid) is True
        assert await db_conn.fetchval("select is_admin from users where id = $1", other) is False
    finally:
        await tr.rollback()


async def test_providers_key_is_encrypted_and_masked(app, make_client, db_conn):
    c = await admin_client(app, make_client)
    r = await c.get("/api/admin/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["encryption_ready"] is True
    assert [p["provider"] for p in body["items"]] == ["gemini", "openai"]
    assert all(p["configured"] is False and p["key_masked"] is None for p in body["items"])
    assert body["items"][0]["model"] == "gemini-flash-latest"

    # ativar sem chave é recusado
    r = await c.put("/api/admin/providers/gemini", json={"enabled": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "key_required"

    r = await c.put("/api/admin/providers/gemini", json={"api_key": FAKE_GEMINI_KEY, "model": "gemini-x", "enabled": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["configured"] and out["enabled"] and out["model"] == "gemini-x"
    assert out["key_masked"] == "••••WXYZ" and out["key_last4"] == "WXYZ"
    assert FAKE_GEMINI_KEY not in r.text
    assert out["updated_by_email"] == ADMIN_EMAIL

    listing = await c.get("/api/admin/providers")
    assert FAKE_GEMINI_KEY not in listing.text

    row = await db_conn.fetchrow("select * from ai_providers where provider = 'gemini'")
    assert FAKE_GEMINI_KEY.encode() not in bytes(row["api_key_ciphertext"])
    assert len(bytes(row["api_key_nonce"])) == 12
    from ai_core.keys import decrypt_api_key

    assert decrypt_api_key(app.state.cipher, "gemini", key_id=row["api_key_key_id"], nonce=row["api_key_nonce"],
                           ciphertext=row["api_key_ciphertext"]) == FAKE_GEMINI_KEY

    audit = await db_conn.fetch(
        "select details from audit_logs where event_type = 'admin.ai_provider_updated' order by created_at")
    assert audit and all(FAKE_GEMINI_KEY not in a["details"] for a in audit)  # jsonb em texto
    assert json.loads(audit[-1]["details"])["key_changed"] is True

    # mudar só o modelo mantém a chave
    r = await c.put("/api/admin/providers/gemini", json={"model": "gemini-y"})
    assert r.json()["configured"] and r.json()["model"] == "gemini-y" and r.json()["enabled"]

    # o authenticated nunca lê a tabela
    import asyncpg

    async with db_conn.transaction():
        await db_conn.execute("set local role authenticated")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.fetch("select * from ai_providers")

    # apagar a chave desativa obrigatoriamente
    r = await c.put("/api/admin/providers/gemini", json={"clear_key": True})
    assert r.status_code == 422
    r = await c.put("/api/admin/providers/gemini", json={"clear_key": True, "enabled": False})
    assert r.status_code == 200 and r.json()["configured"] is False


async def test_put_key_without_encryption_key_is_refused(app, make_client):
    c = await admin_client(app, make_client)
    cipher = app.state.cipher
    app.state.cipher = None
    try:
        r = await c.put("/api/admin/providers/openai", json={"api_key": "sk-fake-test-key-1234"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "encryption_not_configured"
        assert (await c.get("/api/admin/providers")).json()["encryption_ready"] is False
    finally:
        app.state.cipher = cipher


async def test_unknown_provider_and_invalid_input(app, make_client):
    c = await admin_client(app, make_client)
    assert (await c.put("/api/admin/providers/claude", json={"model": "x"})).status_code == 404
    assert (await c.put("/api/admin/providers/gemini", json={"api_key": "has space key"})).status_code == 422
    assert (await c.put("/api/admin/providers/gemini", json={"model": "../bad"})).status_code == 422


@respx.mock
async def test_provider_test_endpoint(app, make_client, db_conn):
    c = await admin_client(app, make_client)
    r = await c.post("/api/admin/providers/gemini/test")
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_required"

    await c.put("/api/admin/providers/gemini", json={"api_key": FAKE_GEMINI_KEY, "model": "gemini-t"})
    route = respx.post(GEMINI_URL.format(model="gemini-t")).mock(return_value=httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    r = await c.post("/api/admin/providers/gemini/test")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["reply"] == "ok"
    assert route.calls.last.request.headers["x-goog-api-key"] == FAKE_GEMINI_KEY

    respx.post(GEMINI_URL.format(model="gemini-t")).mock(return_value=httpx.Response(
        400, json={"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}))
    r = await c.post("/api/admin/providers/gemini/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False and "API key not valid" in r.json()["message"]
    p = [i for i in (await c.get("/api/admin/providers")).json()["items"] if i["provider"] == "gemini"][0]
    assert p["last_test_ok"] is False and p["last_tested_at"]
    assert await db_conn.fetchval(
        "select count(*) from audit_logs where event_type = 'admin.ai_provider_tested'") >= 2


async def test_stats_and_users_pagination(app, make_client):
    c = await admin_client(app, make_client)
    for _ in range(3):
        await user_client(app, make_client)
    stats = (await c.get("/api/admin/stats")).json()
    for k in ("users", "conversations", "messages", "admins", "active_providers"):
        assert isinstance(stats[k], int)
    assert stats["users"] >= 4 and stats["admins"] >= 1

    r = await c.get("/api/admin/users", params={"page": 1, "page_size": 2})
    body = r.json()
    assert r.status_code == 200 and len(body["items"]) == 2
    assert body["total"] >= 4 and body["pages"] == -(-body["total"] // 2)
    assert {"email", "is_admin", "email_verified", "created_at", "conversations", "messages"} <= set(body["items"][0])
    page2 = (await c.get("/api/admin/users", params={"page": 2, "page_size": 2})).json()
    assert {u["id"] for u in page2["items"]}.isdisjoint({u["id"] for u in body["items"]})
    found = (await c.get("/api/admin/users", params={"q": ADMIN_EMAIL})).json()
    assert found["total"] == 1 and found["items"][0]["is_admin"] is True
    assert (await c.get("/api/admin/users", params={"page_size": 500})).status_code == 422
