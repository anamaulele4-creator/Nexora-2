from __future__ import annotations

from conftest import FRONTEND, STRONG_PASSWORD, login, register_and_verify, unique_email


def _outbox(app, email, kind):
    return [m for m in app.state.email_sender.outbox if m.to == email and m.kind == kind]


async def test_register_requires_email_verification_before_login(client, app, db_conn):
    email = unique_email("verify")
    r = await client.post("/api/auth/register", json={"email": email, "password": STRONG_PASSWORD})
    assert r.status_code == 201
    assert r.json()["verification_required"] is True

    # conta de domínio completa: users + perfil + workspace por defeito
    row = await db_conn.fetchrow(
        """select u.email_verified_at, p.default_workspace_id, w.owner_user_id = u.id as owns
           from users u join user_profiles p on p.user_id = u.id join workspaces w on w.id = p.default_workspace_id
           where u.email = $1""", email)
    assert row["email_verified_at"] is None and row["owns"]
    # a palavra-passe nunca fica em texto simples
    stored = await db_conn.fetchval(
        "select password_hash from local_auth.credentials c join users u on u.id = c.user_id where u.email = $1", email)
    assert stored.startswith("$argon2id$") and STRONG_PASSWORD not in stored

    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "email_not_verified"

    token = _outbox(app, email, "email_verification")[-1].link.split("token=")[1]
    assert (await client.post("/api/auth/verify-email", json={"token": token})).status_code == 204
    # token de uso único
    r = await client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_or_expired_token"

    r = await login(client, email)
    assert "access_token" not in r.json()  # browser: só cookies httpOnly
    assert "ca_access" in r.cookies and "ca_refresh" in r.cookies
    set_cookie = " ".join(r.headers.get_list("set-cookie")).lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie


async def test_resend_verification_invalidates_previous_link(client, app):
    email = unique_email("resend")
    await client.post("/api/auth/register", json={"email": email, "password": STRONG_PASSWORD})
    first = _outbox(app, email, "email_verification")[-1].link.split("token=")[1]
    assert (await client.post("/api/auth/resend-verification", json={"email": email})).status_code == 202
    second = _outbox(app, email, "email_verification")[-1].link.split("token=")[1]
    assert first != second
    assert (await client.post("/api/auth/verify-email", json={"token": first})).status_code == 400
    assert (await client.post("/api/auth/verify-email", json={"token": second})).status_code == 204


async def test_weak_password_and_duplicate_email(client, app):
    r = await client.post("/api/auth/register", json={"email": unique_email(), "password": "short"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "weak_password"
    email = unique_email("dup")
    await client.post("/api/auth/register", json={"email": email, "password": STRONG_PASSWORD})
    r = await client.post("/api/auth/register", json={"email": email.upper(), "password": STRONG_PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


async def test_invalid_email_is_rejected(client):
    r = await client.post("/api/auth/register", json={"email": "not-an-email", "password": STRONG_PASSWORD})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_me_requires_auth_and_returns_profile(client, app):
    assert (await client.get("/api/auth/me")).status_code == 401
    email = unique_email("me")
    await register_and_verify(client, app, email)
    await login(client, email)
    r = await client.get("/api/auth/me")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == email and me["email_verified"] and me["theme"] == "dark"
    assert me["default_workspace"]["name"] == "Test's workspace"


async def test_refresh_rotates_and_reuse_revokes_everything(client, app, db_conn):
    email = unique_email("rot")
    await register_and_verify(client, app, email)
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD},
                          headers={"X-Client-Type": "api"})
    first = r.json()
    assert first["access_token"] and first["refresh_token"]

    r = await client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]},
                          headers={"X-Client-Type": "api"})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    # o access token novo funciona
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert me.status_code == 200

    # reutilizar o refresh antigo = suspeita de roubo -> todas as sessões revogadas
    r = await client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert me.status_code == 401
    events = await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'auth.refresh_reuse_detected'""", email)
    assert events == 1


async def test_logout_revokes_session_immediately(client, app):
    email = unique_email("logout")
    await register_and_verify(client, app, email)
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD},
                          headers={"X-Client-Type": "api"})
    tokens = r.json()
    auth = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    r = await client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=auth)
    assert r.status_code == 204
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401
    r = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_password_reset_flow(client, app):
    email = unique_email("reset")
    await register_and_verify(client, app, email)
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD},
                          headers={"X-Client-Type": "api"})
    old_access = r.json()["access_token"]

    # email desconhecido: mesma resposta, nenhum email enviado
    r = await client.post("/api/auth/forgot-password", json={"email": unique_email("ghost")})
    assert r.status_code == 202
    assert (await client.post("/api/auth/forgot-password", json={"email": email})).status_code == 202
    msgs = _outbox(app, email, "password_reset")
    assert len(msgs) == 1 and msgs[0].link.startswith(f"{FRONTEND}/reset-password?token=")
    token = msgs[0].link.split("token=")[1]

    r = await client.post("/api/auth/reset-password", json={"token": token, "new_password": "weak"})
    assert r.status_code == 422
    new_pw = "Brand-New-Pass-77"
    assert (await client.post("/api/auth/reset-password", json={"token": token, "new_password": new_pw})).status_code == 204
    # o mesmo link não serve duas vezes
    assert (await client.post("/api/auth/reset-password", json={"token": token, "new_password": new_pw})).status_code == 400
    # sessões antigas foram revogadas
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_access}"})).status_code == 401
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    assert r.status_code == 401
    await login(client, email, new_pw)


async def test_account_locks_after_repeated_failures(client, app):
    email = unique_email("lock")
    await register_and_verify(client, app, email)
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"email": email, "password": "Wrong-Password-1"})
        assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    assert r.status_code == 423 and r.json()["error"]["code"] == "account_locked"


async def test_unknown_email_login_is_generic(client):
    r = await client.post("/api/auth/login", json={"email": unique_email("nobody"), "password": STRONG_PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


async def test_rate_limit_on_login(client, app):
    app.state.settings.auth_rate_limit_per_15min = 3
    email = unique_email("rl")
    codes = [
        (await client.post("/api/auth/login", json={"email": email, "password": "Whatever-123"})).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429


async def test_cookie_requests_from_foreign_origin_are_blocked(client, app):
    email = unique_email("csrf")
    await register_and_verify(client, app, email)
    await login(client, email)  # o cliente guarda os cookies
    r = await client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed"
    r = await client.post("/api/auth/logout")  # origem permitida (FRONTEND)
    assert r.status_code == 204


async def test_tampered_access_token_rejected(client, app):
    email = unique_email("tamper")
    await register_and_verify(client, app, email)
    r = await client.post("/api/auth/login", json={"email": email, "password": STRONG_PASSWORD},
                          headers={"X-Client-Type": "api"})
    token = r.json()["access_token"]
    head, payload, sig = token.split(".")
    bad = f"{head}.{payload}.{sig[:-2]}{'AA' if sig[-2:] != 'AA' else 'BB'}"
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})).status_code == 401
