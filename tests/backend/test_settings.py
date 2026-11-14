"""Definições: perfil, palavra-passe, preferência de IA e sessões."""

from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest
import respx

from ai_core.preferences import AIPreference, selection_for
from conftest import STRONG_PASSWORD, login, register_and_verify, unique_email
from local_helpers import OLLAMA, admin_client, native_reply, set_cloud, set_engine

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
NEW_PASSWORD = "Another-Horse-77"


@pytest.fixture(autouse=True)
async def _clean(db_conn):
    await db_conn.execute("delete from ai_providers")
    await db_conn.execute("delete from local_ai_engine")
    await db_conn.execute("update ai_policy set allow_paid_providers = false")
    yield
    await db_conn.execute("delete from local_ai_engine")
    await db_conn.execute("update ai_policy set allow_paid_providers = false")


async def new_user(app, make_client, prefix: str = "settings"):
    c = await make_client()
    email = unique_email(prefix)
    await register_and_verify(c, app, email)
    await login(c, email)
    return c, email


def groq_ok(text: str = "Resposta Groq") -> httpx.Response:
    return httpx.Response(200, json={"model": "groq-test", "choices": [{"message": {"content": text}}]})


SETTINGS_ROUTES = [
    ("GET", "/api/settings/profile", None),
    ("PATCH", "/api/settings/profile", {"display_name": "x"}),
    ("POST", "/api/settings/password", {"current_password": "a", "new_password": "b"}),
    ("GET", "/api/settings/ai", None),
    ("PUT", "/api/settings/ai", {"provider": "ollama"}),
    ("DELETE", "/api/settings/ai", None),
    ("GET", "/api/settings/sessions", None),
    ("POST", "/api/settings/sessions/revoke-others", None),
]


async def test_settings_require_login(make_client):
    anon = await make_client()
    for method, path, body in SETTINGS_ROUTES:
        r = await anon.request(method, path, json=body)
        assert r.status_code == 401, (path, r.text)


# --- perfil ---------------------------------------------------------------------------------------------
async def test_profile_read_and_update(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    p = (await c.get("/api/settings/profile")).json()
    assert p["email"] == email and p["email_verified"] is True and p["display_name"] == "Test"
    assert p["avatar_url"] is None

    r = await c.patch("/api/settings/profile", json={"display_name": "  Ana Maulele ",
                                                      "avatar_url": "https://cdn.example.com/a.png"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Ana Maulele" and r.json()["avatar_url"] == "https://cdn.example.com/a.png"
    assert (await c.get("/api/auth/me")).json()["display_name"] == "Ana Maulele"

    # só o campo enviado muda
    r = await c.patch("/api/settings/profile", json={"avatar_url": ""})
    assert r.json()["avatar_url"] is None and r.json()["display_name"] == "Ana Maulele"
    row = await db_conn.fetchrow(
        "select p.display_name, p.avatar_url from user_profiles p join users u on u.id = p.user_id where u.email = $1",
        email)
    assert row["display_name"] == "Ana Maulele" and row["avatar_url"] is None


@pytest.mark.parametrize("bad", ["http://example.com/a.png", "javascript:alert(1)", "ftp://x/y", "https://"])
async def test_profile_rejects_unsafe_avatar(app, make_client, bad):
    c, _ = await new_user(app, make_client)
    r = await c.patch("/api/settings/profile", json={"avatar_url": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_profile_rejects_unknown_fields(app, make_client):
    c, _ = await new_user(app, make_client)
    r = await c.patch("/api/settings/profile", json={"email": "other@example.com"})
    assert r.status_code == 422


# --- palavra-passe ----------------------------------------------------------------------------------------
async def test_change_password_flow(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    other = await make_client()
    await login(other, email)
    assert (await other.get("/api/auth/me")).status_code == 200

    r = await c.post("/api/settings/password", json={"current_password": "Wrong-Password-1", "new_password": NEW_PASSWORD})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_current_password"
    assert await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'settings.password_change_failed'""", email) == 1

    r = await c.post("/api/settings/password", json={"current_password": STRONG_PASSWORD, "new_password": "weak"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "weak_password"

    r = await c.post("/api/settings/password", json={"current_password": STRONG_PASSWORD,
                                                      "new_password": STRONG_PASSWORD})
    assert r.status_code == 422 and r.json()["error"]["code"] == "same_password"

    r = await c.post("/api/settings/password", json={"current_password": STRONG_PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "changed", "revoked_sessions": 1}

    # a sessão atual continua; a outra foi fechada
    assert (await c.get("/api/auth/me")).status_code == 200
    assert (await other.get("/api/auth/me")).status_code == 401
    # hash argon2id novo; a antiga deixa de entrar, a nova entra
    assert (await db_conn.fetchval(
        "select c.password_hash from local_auth.credentials c join users u on u.id = c.user_id where u.email = $1",
        email)).startswith("$argon2id$")
    fresh = await make_client()
    assert (await fresh.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD})).status_code == 401
    assert (await fresh.post("/api/auth/login", json={"email": email, "password": NEW_PASSWORD})).status_code == 200
    assert await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'settings.password_changed'""", email) == 1


async def test_change_password_is_rate_limited(app, make_client):
    c, _ = await new_user(app, make_client)
    app.state.settings.auth_rate_limit_per_15min = 2
    for _ in range(2):
        r = await c.post("/api/settings/password", json={"current_password": "Wrong-Password-1", "new_password": NEW_PASSWORD})
        assert r.status_code == 400
    r = await c.post("/api/settings/password", json={"current_password": "Wrong-Password-1", "new_password": NEW_PASSWORD})
    assert r.status_code == 429


# --- sessões -------------------------------------------------------------------------------------------------
async def test_list_and_revoke_sessions(app, make_client):
    c, email = await new_user(app, make_client)
    other = await make_client()
    await login(other, email)

    items = (await c.get("/api/settings/sessions")).json()["items"]
    assert len(items) == 2
    assert items[0]["current"] is True and items[1]["current"] is False
    assert items[0]["ip"] == "203.0.113.11"
    assert all("refresh_token_hash" not in i for i in items)

    # a sessão atual não se revoga por aqui
    r = await c.delete(f"/api/settings/sessions/{items[0]['id']}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "cannot_revoke_current"

    r = await c.delete(f"/api/settings/sessions/{items[1]['id']}")
    assert r.status_code == 204
    assert (await other.get("/api/auth/me")).status_code == 401
    assert (await c.get("/api/auth/me")).status_code == 200
    assert len((await c.get("/api/settings/sessions")).json()["items"]) == 1

    # já revogada / desconhecida
    assert (await c.delete(f"/api/settings/sessions/{items[1]['id']}")).status_code == 404
    assert (await c.delete("/api/settings/sessions/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_cannot_revoke_another_users_session(app, make_client):
    a, _ = await new_user(app, make_client, "sess-a")
    b, _ = await new_user(app, make_client, "sess-b")
    b_session = (await b.get("/api/settings/sessions")).json()["items"][0]["id"]
    r = await a.delete(f"/api/settings/sessions/{b_session}")
    assert r.status_code == 404
    assert (await b.get("/api/auth/me")).status_code == 200


async def test_revoke_all_other_sessions(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    others = []
    for _ in range(2):
        o = await make_client()
        await login(o, email)
        others.append(o)
    r = await c.post("/api/settings/sessions/revoke-others")
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    for o in others:
        assert (await o.get("/api/auth/me")).status_code == 401
    assert (await c.get("/api/auth/me")).status_code == 200
    assert (await c.post("/api/settings/sessions/revoke-others")).json() == {"revoked": 0}
    assert await db_conn.fetchval(
        """select count(*) from local_auth.sessions s join users u on u.id = s.user_id
           where u.email = $1 and s.revoked_reason = 'user_revoked_others'""", email) == 2


async def test_sessions_and_password_need_local_auth(app, make_client):
    from core.config import AuthProvider

    c, _ = await new_user(app, make_client)
    app.state.settings.auth_provider = AuthProvider.supabase
    try:
        assert (await c.get("/api/settings/sessions")).json()["error"]["code"] == "not_supported"
        r = await c.post("/api/settings/password", json={"current_password": STRONG_PASSWORD, "new_password": NEW_PASSWORD})
        assert r.status_code == 501
    finally:
        app.state.settings.auth_provider = AuthProvider.local


# --- preferência de IA --------------------------------------------------------------------------------------
async def test_ai_preference_defaults(app, make_client):
    c, _ = await new_user(app, make_client)
    body = (await c.get("/api/settings/ai")).json()
    assert body["provider"] == "ollama" and body["fallback_provider"] == "groq" and body["saved"] is False
    assert body["allow_paid_providers"] is False
    opts = {o["provider"]: o for o in body["options"]}
    assert list(opts) == ["auto", "ollama", "gemini", "groq", "openrouter", "openai"]
    assert opts["openai"]["paid"] is True and opts["openai"]["allowed"] is False
    assert opts["groq"]["paid"] is False and opts["groq"]["allowed"] is True
    assert opts["ollama"]["available"] is False and opts["groq"]["available"] is False


async def test_ai_preference_save_validate_and_reset(app, make_client, db_conn):
    await set_cloud(app, db_conn, "groq", "groq-test")
    c, email = await new_user(app, make_client)
    r = await c.put("/api/settings/ai", json={"provider": "groq", "model": "llama-x", "fallback_provider": None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "groq" and body["model"] == "llama-x" and body["fallback_provider"] is None
    assert body["saved"] is True
    assert {o["provider"]: o for o in body["options"]}["groq"]["available"] is True

    for bad in ({"provider": "nope"}, {"provider": "groq", "fallback_provider": "groq"},
                {"provider": "groq", "model": "bad model!"}, {"provider": "ollama", "fallback_provider": "auto"}):
        r = await c.put("/api/settings/ai", json=bad)
        assert r.status_code == 422, (bad, r.text)

    # "auto" não guarda modelo
    r = await c.put("/api/settings/ai", json={"provider": "auto", "model": "ignored", "fallback_provider": "groq"})
    assert r.json()["model"] is None

    assert (await c.delete("/api/settings/ai")).status_code == 204
    assert (await c.get("/api/settings/ai")).json()["saved"] is False
    assert await db_conn.fetchval(
        "select count(*) from user_ai_preferences p join users u on u.id = p.user_id where u.email = $1", email) == 0


async def test_paid_provider_needs_admin_permission(app, make_client):
    c, _ = await new_user(app, make_client)
    for body in ({"provider": "openai"}, {"provider": "ollama", "fallback_provider": "openai"}):
        r = await c.put("/api/settings/ai", json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "paid_provider_disabled"

    # utilizador normal não muda a política
    assert (await c.put("/api/admin/ai-policy", json={"allow_paid_providers": True})).status_code == 403
    assert (await c.get("/api/admin/ai-policy")).status_code == 403

    admin = await admin_client(app, make_client)
    r = await admin.put("/api/admin/ai-policy", json={"allow_paid_providers": True})
    assert r.status_code == 200 and r.json()["allow_paid_providers"] is True and r.json()["paid_providers"] == ["openai"]
    assert (await admin.get("/api/admin/ai-policy")).json()["updated_by_email"]

    r = await c.put("/api/settings/ai", json={"provider": "openai", "model": "gpt-test"})
    assert r.status_code == 200 and r.json()["provider"] == "openai"
    assert {o["provider"]: o for o in r.json()["options"]}["openai"]["allowed"] is True


def test_selection_for_preference():
    assert selection_for(AIPreference(), allow_paid=False) is None  # sem preferência: automático
    sel = selection_for(AIPreference("ollama", None, "groq", saved=True), allow_paid=False)
    assert sel.provider == "ollama" and sel.fallback == ("groq",)
    sel = selection_for(AIPreference("groq", "m1", None, saved=True), allow_paid=False)
    assert sel.provider == "groq" and sel.model == "m1" and sel.fallback == ()
    # paga sem autorização: passa para o recurso escolhido, ou para o automático
    sel = selection_for(AIPreference("openai", "gpt", "groq", saved=True), allow_paid=False)
    assert sel.provider == "groq" and sel.model is None and sel.fallback == ()
    sel = selection_for(AIPreference("openai", "gpt", None, saved=True), allow_paid=False)
    assert sel.provider is None and sel.model is None and sel.fallback == ()
    sel = selection_for(AIPreference("groq", None, "openai", saved=True), allow_paid=False)
    assert sel.fallback == ()
    sel = selection_for(AIPreference("openai", "gpt", None, saved=True), allow_paid=True)
    assert sel.provider == "openai" and sel.model == "gpt"


async def new_conv(c) -> str:
    return (await c.post("/api/chat/conversations", json={})).json()["id"]


@respx.mock
async def test_chat_uses_preference_and_falls_back_to_groq(app, make_client, db_conn):
    """Preferência ollama + recurso groq: com o motor em baixo, responde o Groq."""
    await set_engine(app, db_conn, allow_cloud_fallback=False)
    await set_cloud(app, db_conn, "groq", "groq-test")
    local = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("desligado"))
    groq = respx.post(GROQ_URL).mock(return_value=groq_ok())
    c, _ = await new_user(app, make_client)
    conv = await new_conv(c)

    # sem preferência gravada: modo automático do admin (sem recurso para a nuvem) -> erro
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 503 and not groq.called

    assert (await c.put("/api/settings/ai", json={"provider": "ollama", "fallback_provider": "groq"})).status_code == 200
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 201, r.text
    assert r.json()["assistant_message"]["provider"] == "groq"
    assert local.called and groq.called


@respx.mock
async def test_chat_uses_preferred_provider_and_model(app, make_client, db_conn):
    await set_engine(app, db_conn)
    await set_cloud(app, db_conn, "groq", "groq-test")
    local = respx.post(f"{OLLAMA}/api/chat").mock(return_value=native_reply("Local"))
    groq = respx.post(GROQ_URL).mock(return_value=groq_ok("Groq escolhido"))
    c, _ = await new_user(app, make_client)
    await c.put("/api/settings/ai", json={"provider": "groq", "model": "my-groq-model", "fallback_provider": None})
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 201 and r.json()["assistant_message"]["content"] == "Groq escolhido"
    assert json.loads(groq.calls.last.request.content)["model"] == "my-groq-model"
    assert not local.called

    # escolha explícita no pedido continua a mandar
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá", "provider": "ollama"})
    assert r.status_code == 201 and r.json()["assistant_message"]["provider"] == "ollama"


@respx.mock
async def test_chat_ignores_paid_preference_after_admin_disables(app, make_client, db_conn):
    await set_cloud(app, db_conn, "openai", "gpt-test")
    await set_cloud(app, db_conn, "groq", "groq-test")
    paid = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "pago"}}]}))
    groq = respx.post(GROQ_URL).mock(return_value=groq_ok("gratis"))
    await db_conn.execute("update ai_policy set allow_paid_providers = true")
    c, _ = await new_user(app, make_client)
    assert (await c.put("/api/settings/ai", json={"provider": "openai", "fallback_provider": "groq"})).status_code == 200
    conv = await new_conv(c)
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.json()["assistant_message"]["provider"] == "openai"

    await db_conn.execute("update ai_policy set allow_paid_providers = false")
    paid.reset()
    r = await c.post(f"/api/chat/conversations/{conv}/messages", json={"content": "Olá"})
    assert r.status_code == 201 and r.json()["assistant_message"]["provider"] == "groq"
    assert not paid.called and groq.called


async def test_ai_preferences_rls(db_conn):
    """Ao nível da BD: cada utilizador só vê e escreve a sua preferência; ai_policy é só do serviço."""
    from authentication.accounts import create_account

    tr = db_conn.transaction()
    await tr.start()
    try:
        a = await create_account(db_conn, email=f"pref-a-{uuid.uuid4().hex[:6]}@example.com", display_name=None)
        b = await create_account(db_conn, email=f"pref-b-{uuid.uuid4().hex[:6]}@example.com", display_name=None)
        await db_conn.execute("insert into user_ai_preferences (user_id, provider) values ($1, 'ollama')", a)
        await db_conn.execute("set local role authenticated")
        await db_conn.execute("select set_config('request.jwt.claims', $1, true)", json.dumps({"sub": str(b)}))
        assert await db_conn.fetchval("select count(*) from user_ai_preferences") == 0
        await db_conn.execute("savepoint s")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.execute("insert into user_ai_preferences (user_id, provider) values ($1, 'ollama')", a)
        await db_conn.execute("rollback to savepoint s")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.fetch("select * from ai_policy")
    finally:
        await tr.rollback()
