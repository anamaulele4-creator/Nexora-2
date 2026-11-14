"""Conectores: OAuth (GitHub, Gmail, Google Drive), Shopify, estado, desligar, cifra e admin.

Os fornecedores externos são simulados com respx (nenhum pedido real sai do teste);
a BD e o Redis são reais.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from pydantic import SecretStr

from conftest import FRONTEND, login, register_and_verify, unique_email
from connector_hub import oauth
from connector_hub.vault import decrypt_payload
from connectors.base import ConnectorError
from local_helpers import admin_client

GITHUB_CLIENT_ID = "Iv1.testclientid0001"
GITHUB_SECRET = "fake-github-secret-for-tests-0001"  # não é um segredo real
GOOGLE_CLIENT_ID = "1234-test.apps.googleusercontent.com"
GOOGLE_SECRET = "fake-google-secret-for-tests-0001"  # não é um segredo real
GH_TOKEN = "gho_faketoken0000000000000000000000001"
GOOGLE_ACCESS = "ya29.fake-access-token-0001"
GOOGLE_REFRESH = "1//fake-refresh-token-0001"
SHOP = "loja-teste.myshopify.com"
SHOP_TOKEN = "shpat_fake0000000000000000000000000001"
SHOP_URL = f"https://{SHOP}/admin/api/2026-07/shop.json"


@pytest.fixture(autouse=True)
def _oauth_settings(app):
    s = app.state.settings
    saved = (s.github_oauth_client_id, s.github_oauth_client_secret, s.google_oauth_client_id,
             s.google_oauth_client_secret, s.oauth_redirect_base_url)
    s.github_oauth_client_id, s.github_oauth_client_secret = GITHUB_CLIENT_ID, SecretStr(GITHUB_SECRET)
    s.google_oauth_client_id, s.google_oauth_client_secret = GOOGLE_CLIENT_ID, SecretStr(GOOGLE_SECRET)
    s.oauth_redirect_base_url = None
    yield
    (s.github_oauth_client_id, s.github_oauth_client_secret, s.google_oauth_client_id,
     s.google_oauth_client_secret, s.oauth_redirect_base_url) = saved


async def new_user(app, make_client, prefix: str = "conn"):
    c = await make_client()
    email = unique_email(prefix)
    await register_and_verify(c, app, email)
    await login(c, email)
    return c, email


async def start(c, provider: str, connector: str) -> dict:
    r = await c.get(f"/api/connectors/oauth/{provider}/start", params={"connector": connector})
    assert r.status_code == 200, r.text
    url = r.json()["authorize_url"]
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()} | {"_base": url.split("?")[0]}


def redirect_params(r) -> dict:
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(f"{FRONTEND}/conectores?"), loc
    return {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}


def mock_github(login_name: str = "octo-ana", user_id: int = 4242):
    token = respx.post(oauth.GITHUB_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": GH_TOKEN, "token_type": "bearer", "scope": "repo,read:user"}))
    user = respx.get(f"{oauth.GITHUB_API}/user").mock(return_value=httpx.Response(
        200, json={"id": user_id, "login": login_name}))
    return token, user


def mock_google(email: str = "ana@gmail.com", expires_in: int = 3599):
    token = respx.post(oauth.GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": GOOGLE_ACCESS, "refresh_token": GOOGLE_REFRESH, "expires_in": expires_in,
                   "scope": "openid email https://www.googleapis.com/auth/gmail.readonly", "token_type": "Bearer"}))
    info = respx.get(oauth.GOOGLE_USERINFO_URL).mock(return_value=httpx.Response(
        200, json={"sub": "1098765", "email": email}))
    return token, info


async def connect_github(c, **kw):
    params = await start(c, "github", "github")
    mock_github(**kw)
    r = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc123", "state": params["state"]})
    return redirect_params(r)


# --- catálogo -----------------------------------------------------------------------------------------
async def test_catalog_shows_connectable_connectors(app, make_client):
    c, _ = await new_user(app, make_client)
    body = (await c.get("/api/connectors")).json()
    assert body["encryption_ready"] is True
    items = {i["key"]: i for i in body["items"]}
    assert len(items) == 6 and all(i["status"] == "planned" for i in items.values())
    assert {k for k, i in items.items() if i["connectable"]} == {"github", "gmail", "google_drive", "shopify"}
    assert items["github"]["method"] == "oauth" and items["github"]["oauth_provider"] == "github"
    assert items["gmail"]["oauth_provider"] == "google" and items["google_drive"]["oauth_provider"] == "google"
    assert items["shopify"]["method"] == "api_key" and items["shopify"]["auth_type"] == "api_key"
    assert items["github"]["oauth_configured"] is True
    assert all(i["connection"] is None for i in items.values())


async def test_connector_routes_require_login(make_client):
    anon = await make_client()
    for method, path in (("GET", "/api/connectors"), ("GET", "/api/connectors/oauth/github/start"),
                         ("POST", "/api/connectors/shopify/connect"), ("GET", "/api/connectors/github/status"),
                         ("DELETE", "/api/connectors/github")):
        assert (await anon.request(method, path)).status_code == 401, path


async def test_unknown_or_unconnectable_connector(app, make_client):
    c, _ = await new_user(app, make_client)
    assert (await c.post("/api/connectors/custom_api/connect")).json()["error"]["code"] == "connector_not_found"
    assert (await c.get("/api/connectors/nope/status")).status_code == 404
    assert (await c.get("/api/connectors/oauth/facebook/start")).status_code == 404
    r = await c.get("/api/connectors/oauth/google/start")  # google precisa de dizer gmail ou google_drive
    assert r.status_code == 422
    r = await c.get("/api/connectors/oauth/github/start", params={"connector": "gmail"})
    assert r.status_code == 422


# --- OAuth: início ---------------------------------------------------------------------------------------
async def test_oauth_not_configured(app, make_client):
    app.state.settings.github_oauth_client_id = None
    c, _ = await new_user(app, make_client)
    r = await c.get("/api/connectors/oauth/github/start")
    assert r.status_code == 503 and r.json()["error"]["code"] == "oauth_not_configured"
    assert "GITHUB_OAUTH_CLIENT_ID" in r.json()["error"]["message"]
    items = {i["key"]: i for i in (await c.get("/api/connectors")).json()["items"]}
    assert items["github"]["oauth_configured"] is False and items["gmail"]["oauth_configured"] is True


async def test_github_start_builds_authorize_url(app, make_client):
    c, _ = await new_user(app, make_client)
    p = await start(c, "github", "github")
    assert p["_base"] == oauth.GITHUB_AUTHORIZE_URL
    assert p["client_id"] == GITHUB_CLIENT_ID
    assert p["redirect_uri"] == f"{FRONTEND}/api/connectors/oauth/github/callback"
    assert p["scope"] == "repo read:user"
    assert len(p["state"]) >= 40
    # o connect genérico devolve o mesmo tipo de URL
    r = await c.post("/api/connectors/github/connect")
    assert r.status_code == 200 and r.json()["method"] == "oauth"
    assert r.json()["authorize_url"].startswith(oauth.GITHUB_AUTHORIZE_URL)


async def test_redirect_base_override(app, make_client):
    app.state.settings.oauth_redirect_base_url = "https://connector-ai-2yn3.onrender.com"
    c, _ = await new_user(app, make_client)
    p = await start(c, "google", "gmail")
    assert p["redirect_uri"] == "https://connector-ai-2yn3.onrender.com/api/connectors/oauth/google/callback"


async def test_google_start_uses_pkce_offline_and_connector_scopes(app, make_client):
    c, _ = await new_user(app, make_client)
    p = await start(c, "google", "gmail")
    assert p["_base"] == oauth.GOOGLE_AUTHORIZE_URL
    assert p["response_type"] == "code" and p["access_type"] == "offline" and p["prompt"] == "consent"
    assert p["code_challenge_method"] == "S256" and len(p["code_challenge"]) == 43
    assert set(p["scope"].split()) == {"https://www.googleapis.com/auth/gmail.readonly",
                                       "https://www.googleapis.com/auth/gmail.compose", "openid", "email"}
    d = await start(c, "google", "google_drive")
    assert set(d["scope"].split()) == {"https://www.googleapis.com/auth/drive.file", "openid", "email"}


# --- OAuth: retorno --------------------------------------------------------------------------------------
@respx.mock
async def test_github_full_flow_stores_encrypted_token(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    params = await start(c, "github", "github")
    token_route, user_route = mock_github()
    r = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc123", "state": params["state"]})
    assert redirect_params(r) == {"connected": "github"}

    sent = parse_qs(token_route.calls.last.request.content.decode())
    assert sent["code"] == ["abc123"] and sent["client_id"] == [GITHUB_CLIENT_ID]
    assert sent["redirect_uri"] == [f"{FRONTEND}/api/connectors/oauth/github/callback"]
    assert user_route.calls.last.request.headers["authorization"] == f"Bearer {GH_TOKEN}"

    item = {i["key"]: i for i in (await c.get("/api/connectors")).json()["items"]}["github"]
    assert item["connection"]["status"] == "connected" and item["connection"]["account_label"] == "octo-ana"
    # nenhum token na resposta da API
    assert GH_TOKEN not in (await c.get("/api/connectors")).text

    row = await db_conn.fetchrow(
        """select c.id, c.user_id, c.key_id, c.nonce, c.ciphertext, c.credential_type, c.algorithm,
                  a.metadata::text as metadata, jsonb_typeof(a.metadata) as metadata_type
           from encrypted_credentials c join connected_apps a on a.id = c.connected_app_id
           join users u on u.id = c.user_id where u.email = $1""", email)
    assert row["credential_type"] == "oauth_token" and row["algorithm"] == "AES-256-GCM"
    assert row["metadata_type"] == "object"
    assert GH_TOKEN.encode() not in bytes(row["ciphertext"])
    payload = decrypt_payload(app.state.cipher, row["user_id"], row["id"], key_id=row["key_id"], nonce=row["nonce"],
                              ciphertext=row["ciphertext"])
    assert payload["access_token"] == GH_TOKEN
    assert json.loads(row["metadata"])["scopes"] == ["repo", "read:user"]  # jsonb (objeto, não texto)
    assert await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'connector.connected'""", email) == 1


@respx.mock
async def test_ciphertext_is_bound_to_owner(app, make_client, db_conn):
    """O texto cifrado copiado para outro utilizador não decifra (AAD)."""
    from security.crypto import DecryptionError

    c, email = await new_user(app, make_client)
    await connect_github(c)
    row = await db_conn.fetchrow(
        """select c.id, c.key_id, c.nonce, c.ciphertext from encrypted_credentials c
           join users u on u.id = c.user_id where u.email = $1""", email)
    other = await db_conn.fetchval("select id from users where email <> $1 limit 1", email)
    with pytest.raises(DecryptionError):
        decrypt_payload(app.state.cipher, other, row["id"], key_id=row["key_id"], nonce=row["nonce"],
                        ciphertext=row["ciphertext"])


@respx.mock
async def test_state_is_single_use_and_validated(app, make_client):
    c, _ = await new_user(app, make_client)
    params = await start(c, "github", "github")
    mock_github()
    first = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": params["state"]})
    assert redirect_params(first) == {"connected": "github"}
    again = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": params["state"]})
    assert redirect_params(again)["error"] == "oauth_state_invalid"
    forged = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": "forged-state"})
    assert redirect_params(forged)["error"] == "oauth_state_invalid"
    # state do Google não serve no retorno do GitHub
    g = await start(c, "google", "gmail")
    cross = await c.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": g["state"]})
    assert redirect_params(cross)["error"] == "oauth_state_invalid"


@respx.mock
async def test_callback_from_another_logged_in_user_is_rejected(app, make_client, db_conn):
    a, email_a = await new_user(app, make_client, "owner")
    b, _ = await new_user(app, make_client, "intruder")
    params = await start(a, "github", "github")
    mock_github()
    r = await b.get("/api/connectors/oauth/github/callback", params={"code": "abc", "state": params["state"]})
    assert redirect_params(r)["error"] == "oauth_state_invalid"
    assert await db_conn.fetchval(
        "select count(*) from connected_apps a join users u on u.id = a.user_id where u.email = $1", email_a) == 0


@respx.mock
async def test_callback_user_denied_and_exchange_failure(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    params = await start(c, "github", "github")
    r = await c.get("/api/connectors/oauth/github/callback",
                    params={"error": "access_denied", "state": params["state"]})
    assert redirect_params(r) == {"error": "access_denied", "connector": "github"}

    params = await start(c, "github", "github")
    respx.post(oauth.GITHUB_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"error": "bad_verification_code", "error_description": "The code is incorrect"}))
    r = await c.get("/api/connectors/oauth/github/callback", params={"code": "bad", "state": params["state"]})
    assert redirect_params(r) == {"error": "oauth_exchange_failed", "connector": "github"}
    assert await db_conn.fetchval(
        "select count(*) from connected_apps a join users u on u.id = a.user_id where u.email = $1", email) == 0
    assert await db_conn.fetchval(
        """select count(*) from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'connector.oauth_failed'""", email) == 2


@respx.mock
async def test_google_gmail_flow_sends_pkce_verifier_and_keeps_refresh_token(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    params = await start(c, "google", "gmail")
    token_route, _ = mock_google()
    r = await c.get("/api/connectors/oauth/google/callback", params={"code": "4/xyz", "state": params["state"]})
    assert redirect_params(r) == {"connected": "gmail"}
    sent = parse_qs(token_route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    verifier = sent["code_verifier"][0]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() \
        == params["code_challenge"]

    item = {i["key"]: i for i in (await c.get("/api/connectors")).json()["items"]}
    assert item["gmail"]["connection"]["account_label"] == "ana@gmail.com"
    assert item["google_drive"]["connection"] is None  # cada conector Google é uma ligação própria
    row = await db_conn.fetchrow(
        """select c.id, c.user_id, c.key_id, c.nonce, c.ciphertext, c.expires_at from encrypted_credentials c
           join users u on u.id = c.user_id where u.email = $1""", email)
    payload = decrypt_payload(app.state.cipher, row["user_id"], row["id"], key_id=row["key_id"], nonce=row["nonce"],
                              ciphertext=row["ciphertext"])
    assert payload["refresh_token"] == GOOGLE_REFRESH and row["expires_at"] is not None


@respx.mock
async def test_reconnect_replaces_credential(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    await connect_github(c, login_name="first")
    respx.reset()
    await connect_github(c, login_name="second")
    assert await db_conn.fetchval(
        "select count(*) from connected_apps a join users u on u.id = a.user_id where u.email = $1", email) == 1
    assert await db_conn.fetchval(
        "select count(*) from encrypted_credentials c join users u on u.id = c.user_id where u.email = $1", email) == 1
    item = {i["key"]: i for i in (await c.get("/api/connectors")).json()["items"]}["github"]
    assert item["connection"]["account_label"] == "second"


# --- estado ------------------------------------------------------------------------------------------------
@respx.mock
async def test_status_live_check_github(app, make_client):
    c, _ = await new_user(app, make_client)
    r = await c.get("/api/connectors/github/status", params={"check": "true"})
    assert r.json()["check"]["code"] == "not_connected"
    await connect_github(c)
    r = await c.get("/api/connectors/github/status")
    assert r.status_code == 200 and r.json()["connection"]["status"] == "connected" and r.json()["check"] is None
    r = await c.get("/api/connectors/github/status", params={"check": "true"})
    assert r.json()["check"]["ok"] is True

    # token revogado do lado do GitHub -> expirado
    respx.get(f"{oauth.GITHUB_API}/user").mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    r = await c.get("/api/connectors/github/status", params={"check": "true"})
    body = r.json()
    assert body["check"] == {"ok": False, "code": "token_invalid", "message": body["check"]["message"]}
    assert body["connection"]["status"] == "expired" and body["connection"]["last_health_status"] == "unhealthy"


@respx.mock
async def test_status_check_refreshes_expired_google_token(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    params = await start(c, "google", "google_drive")
    mock_google(expires_in=1)  # expira já (margem de 60 s)
    await c.get("/api/connectors/oauth/google/callback", params={"code": "4/xyz", "state": params["state"]})
    refresh = respx.post(oauth.GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "ya29.refreshed-0002", "expires_in": 3599, "token_type": "Bearer"}))
    info = respx.get(oauth.GOOGLE_USERINFO_URL).mock(return_value=httpx.Response(
        200, json={"sub": "1098765", "email": "ana@gmail.com"}))
    r = await c.get("/api/connectors/google_drive/status", params={"check": "true"})
    assert r.json()["check"]["ok"] is True, r.text
    sent = parse_qs(refresh.calls.last.request.content.decode())
    assert sent["grant_type"] == ["refresh_token"] and sent["refresh_token"] == [GOOGLE_REFRESH]
    assert info.calls.last.request.headers["authorization"] == "Bearer ya29.refreshed-0002"
    row = await db_conn.fetchrow(
        """select c.id, c.user_id, c.key_id, c.nonce, c.ciphertext, c.rotated_at from encrypted_credentials c
           join users u on u.id = c.user_id where u.email = $1""", email)
    payload = decrypt_payload(app.state.cipher, row["user_id"], row["id"], key_id=row["key_id"], nonce=row["nonce"],
                              ciphertext=row["ciphertext"])
    assert payload["access_token"] == "ya29.refreshed-0002" and payload["refresh_token"] == GOOGLE_REFRESH
    assert payload["expires_at"] > time.time() + 3000 and row["rotated_at"] is not None


# --- Shopify ----------------------------------------------------------------------------------------------
def test_normalize_shop_domain():
    assert oauth.normalize_shop_domain("Loja-Teste") == SHOP
    assert oauth.normalize_shop_domain("https://loja-teste.myshopify.com/admin/products") == SHOP
    for bad in ("loja.com", "evil.example.com", "-x.myshopify.com", "a b.myshopify.com", ""):
        with pytest.raises(ConnectorError):
            oauth.normalize_shop_domain(bad)


@respx.mock
async def test_shopify_connect_check_and_disconnect(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    r = await c.post("/api/connectors/shopify/connect", json={"shop_domain": "loja.com", "access_token": SHOP_TOKEN})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_shop_domain"
    r = await c.post("/api/connectors/shopify/connect", json={"shop_domain": "loja-teste"})
    assert r.status_code == 422

    shop = respx.get(SHOP_URL).mock(return_value=httpx.Response(401, json={"errors": "Invalid API key"}))
    r = await c.post("/api/connectors/shopify/connect", json={"shop_domain": "loja-teste", "access_token": SHOP_TOKEN})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_shopify_token"
    assert shop.calls.last.request.headers["x-shopify-access-token"] == SHOP_TOKEN

    shop.mock(return_value=httpx.Response(200, json={"shop": {"id": 777, "name": "Loja Teste"}}))
    r = await c.post("/api/connectors/shopify/connect", json={"shop_domain": "loja-teste", "access_token": SHOP_TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["status"] == "connected"
    assert r.json()["connection"]["account_label"] == f"Loja Teste ({SHOP})"
    assert SHOP_TOKEN not in r.text
    cred = await db_conn.fetchrow(
        """select c.credential_type, c.ciphertext, a.metadata::text as metadata from encrypted_credentials c
           join connected_apps a on a.id = c.connected_app_id join users u on u.id = c.user_id where u.email = $1""",
        email)
    assert cred["credential_type"] == "api_key" and SHOP_TOKEN.encode() not in bytes(cred["ciphertext"])
    assert json.loads(cred["metadata"])["shop_domain"] == SHOP

    r = await c.get("/api/connectors/shopify/status", params={"check": "true"})
    assert r.json()["check"]["ok"] is True
    shop.mock(return_value=httpx.Response(401, json={"errors": "Invalid API key"}))
    r = await c.get("/api/connectors/shopify/status", params={"check": "true"})
    assert r.json()["check"]["code"] == "invalid_shopify_token" and r.json()["connection"]["status"] == "expired"

    assert (await c.delete("/api/connectors/shopify")).status_code == 204
    assert await db_conn.fetchval(
        "select count(*) from encrypted_credentials c join users u on u.id = c.user_id where u.email = $1", email) == 0


# --- desligar e isolamento -----------------------------------------------------------------------------------
@respx.mock
async def test_disconnect_revokes_remotely_and_deletes_token(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    await connect_github(c)
    revoke = respx.delete(f"{oauth.GITHUB_API}/applications/{GITHUB_CLIENT_ID}/grant").mock(
        return_value=httpx.Response(204))
    assert (await c.delete("/api/connectors/github")).status_code == 204
    assert revoke.called
    assert json.loads(revoke.calls.last.request.content) == {"access_token": GH_TOKEN}
    assert await db_conn.fetchval(
        "select count(*) from encrypted_credentials c join users u on u.id = c.user_id where u.email = $1", email) == 0
    item = {i["key"]: i for i in (await c.get("/api/connectors")).json()["items"]}["github"]
    assert item["connection"]["status"] == "revoked" and item["connection"]["revoked_at"]
    assert (await c.delete("/api/connectors/github")).json()["error"]["code"] == "not_connected"
    assert await db_conn.fetchval(
        """select (details->>'remote_revoked')::boolean from audit_logs a join users u on u.id = a.user_id
           where u.email = $1 and a.event_type = 'connector.disconnected'""", email) is True


@respx.mock
async def test_disconnect_google_works_even_if_remote_revoke_fails(app, make_client, db_conn):
    c, email = await new_user(app, make_client)
    params = await start(c, "google", "gmail")
    mock_google()
    await c.get("/api/connectors/oauth/google/callback", params={"code": "4/xyz", "state": params["state"]})
    revoke = respx.post(oauth.GOOGLE_REVOKE_URL).mock(side_effect=httpx.ConnectError("offline"))
    assert (await c.delete("/api/connectors/gmail")).status_code == 204
    assert revoke.called
    assert await db_conn.fetchval(
        "select count(*) from encrypted_credentials c join users u on u.id = c.user_id where u.email = $1", email) == 0


@respx.mock
async def test_connections_are_isolated_between_users(app, make_client):
    a, _ = await new_user(app, make_client, "iso-a")
    b, _ = await new_user(app, make_client, "iso-b")
    await connect_github(a)
    items_b = {i["key"]: i for i in (await b.get("/api/connectors")).json()["items"]}
    assert items_b["github"]["connection"] is None
    assert (await b.delete("/api/connectors/github")).status_code == 404
    assert (await b.get("/api/connectors/github/status")).json()["connection"] is None
    assert {i["key"]: i for i in (await a.get("/api/connectors")).json()["items"]}["github"]["connection"]["status"] \
        == "connected"


async def test_connect_requires_encryption_key(app, make_client):
    c, _ = await new_user(app, make_client)
    cipher = app.state.cipher
    app.state.cipher = None
    try:
        r = await c.get("/api/connectors/oauth/github/start")
        assert r.status_code == 503 and r.json()["error"]["code"] == "encryption_not_configured"
        r = await c.post("/api/connectors/shopify/connect", json={"shop_domain": "x", "access_token": SHOP_TOKEN})
        assert r.status_code == 503
        assert (await c.get("/api/connectors")).json()["encryption_ready"] is False
    finally:
        app.state.cipher = cipher


async def test_connect_is_rate_limited(app, make_client):
    c, _ = await new_user(app, make_client)
    for _ in range(20):
        assert (await c.get("/api/connectors/oauth/github/start")).status_code == 200
    r = await c.get("/api/connectors/oauth/github/start")
    assert r.status_code == 429


# --- admin -----------------------------------------------------------------------------------------------------
async def test_admin_connectors_overview(app, make_client):
    plain, _ = await new_user(app, make_client)
    assert (await plain.get("/api/admin/connectors")).status_code == 403
    app.state.settings.google_oauth_client_secret = None
    admin = await admin_client(app, make_client)
    r = await admin.get("/api/admin/connectors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["encryption_ready"] is True and body["redirect_base"] == FRONTEND
    items = {i["key"]: i for i in body["items"]}
    assert list(items) == ["github", "gmail", "google_drive", "shopify"]
    assert items["github"]["oauth_configured"] is True
    assert items["github"]["redirect_uri"] == f"{FRONTEND}/api/connectors/oauth/github/callback"
    assert items["github"]["env_vars"] == [{"name": "GITHUB_OAUTH_CLIENT_ID", "set": True},
                                           {"name": "GITHUB_OAUTH_CLIENT_SECRET", "set": True}]
    assert items["gmail"]["oauth_configured"] is False
    assert {"name": "GOOGLE_OAUTH_CLIENT_SECRET", "set": False} in items["gmail"]["env_vars"]
    assert items["shopify"]["method"] == "api_key" and items["shopify"]["oauth_configured"] is None
    assert GITHUB_SECRET not in r.text and GITHUB_CLIENT_ID not in r.text


@respx.mock
async def test_admin_overview_counts_connections(app, make_client):
    admin = await admin_client(app, make_client)
    before = {i["key"]: i for i in (await admin.get("/api/admin/connectors")).json()["items"]}["github"]
    c, _ = await new_user(app, make_client)
    await connect_github(c)
    after = {i["key"]: i for i in (await admin.get("/api/admin/connectors")).json()["items"]}["github"]
    assert after["connected_accounts"] == before["connected_accounts"] + 1


# --- configuração e migração --------------------------------------------------------------------------------------
def test_oauth_settings_validation():
    from conftest import make_settings

    s = make_settings(github_oauth_client_id="  ", github_oauth_client_secret="",
                      oauth_redirect_base_url="https://connector-ai-2yn3.onrender.com/")
    assert s.github_oauth_client_id is None and s.github_oauth_client_secret is None
    assert s.oauth_redirect_base == "https://connector-ai-2yn3.onrender.com"
    assert oauth.redirect_uri(s, "google") == "https://connector-ai-2yn3.onrender.com/api/connectors/oauth/google/callback"
    assert make_settings().oauth_redirect_base == FRONTEND
    with pytest.raises(ValueError):
        make_settings(oauth_redirect_base_url="connector-ai.onrender.com")
    with pytest.raises(ValueError):
        make_settings(shopify_api_version="latest")


async def test_migration_0008_constraints(db_conn):
    assert await db_conn.fetchval("select allow_paid_providers from ai_policy where id = 1") is False
    idx = await db_conn.fetchval(
        "select indexdef from pg_indexes where indexname = 'connected_apps_user_definition_key'")
    assert "UNIQUE" in idx and "(user_id, connector_definition_id)" in idx
    assert await db_conn.fetchval("select auth_type from connector_definitions where key = 'shopify'") == "api_key"
