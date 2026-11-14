"""Fornecedor Supabase testado com um servidor Supabase Auth SIMULADO (httpx.MockTransport).

Isto valida o nosso código (pedidos enviados, criação da conta de domínio, validação
do JWT), não o comportamento de um projeto Supabase real.
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import jwt

from authentication.providers.base import ClientInfo
from authentication.providers.supabase import SupabaseAuthProvider
from conftest import make_settings
from core.db import Database

SB_SECRET = "supabase-test-jwt-secret-0123456789abcdef0123"
CLIENT = ClientInfo("198.51.100.7", "pytest")


def _token(uid: str, email: str) -> str:
    return jwt.encode({"sub": uid, "email": email, "aud": "authenticated", "role": "authenticated",
                       "exp": int(time.time()) + 600, "session_id": str(uuid.uuid4())}, SB_SECRET, algorithm="HS256")


async def test_supabase_login_creates_domain_account_and_validates_jwt(migrated_db, db_conn):
    uid, email = str(uuid.uuid4()), f"sb-{uuid.uuid4().hex[:6]}@example.com"
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path + ("?" + request.url.query.decode() if request.url.query else ""), body))
        assert request.headers["apikey"] == "anon-key"
        if request.url.path.endswith("/token"):
            if body.get("password") != "Right-Password-1":
                return httpx.Response(400, json={"error_code": "invalid_credentials", "msg": "Invalid login"})
            return httpx.Response(200, json={
                "access_token": _token(uid, email), "refresh_token": "r" * 20, "expires_in": 3600,
                "user": {"id": uid, "email": email, "email_confirmed_at": "2026-11-13T10:00:00Z"}})
        return httpx.Response(404)

    settings = make_settings(auth_provider="supabase", supabase_url="https://proj.supabase.co",
                             supabase_anon_key="anon-key", supabase_jwt_secret=SB_SECRET)
    db = Database(migrated_db)
    await db.connect()
    try:
        provider = SupabaseAuthProvider(db, settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            await provider.login(email, "wrong", CLIENT)
            raise AssertionError("devia falhar")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == "invalid_credentials"

        pair = await provider.login(email, "Right-Password-1", CLIENT)
        assert str(pair.user_id) == uid
        assert seen[-1][0] == "/auth/v1/token?grant_type=password"
        row = await db_conn.fetchrow("select email, email_verified_at from users where id = $1::uuid", uid)
        assert row["email"] == email and row["email_verified_at"] is not None

        principal = await provider.authenticate(pair.access_token)
        assert str(principal.user_id) == uid

        forged = jwt.encode({"sub": uid, "aud": "authenticated", "exp": int(time.time()) + 600}, "x" * 40, algorithm="HS256")
        try:
            await provider.authenticate(forged)
            raise AssertionError("token forjado aceite")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "status_code", None) == 401
    finally:
        await db.close()
