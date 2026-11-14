"""Autenticação delegada ao Supabase Auth (AUTH_PROVIDER=supabase).

O backend fala com a API REST do Supabase Auth (GoTrue) e valida localmente os
access tokens com o SUPABASE_JWT_SECRET (HS256). As tabelas de domínio (users,
user_profiles, workspaces) são criadas no primeiro login com o mesmo id do Supabase,
por isso as políticas RLS funcionam igual nos dois modos.

Estado na Etapa 1: implementado e testado com respostas simuladas (sem projeto
Supabase real). Validação contra um projeto real fica para quando a Ana autorizar
as chaves. Projetos Supabase novos com chaves de assinatura assimétricas (JWKS)
precisam de um pequeno ajuste em ``authenticate`` (ver docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import ipaddress
import uuid

import httpx
import jwt

from authentication.accounts import create_account
from authentication.passwords import password_problems
from authentication.providers.base import (
    AuthError,
    AuthProviderBase,
    ClientInfo,
    RegistrationResult,
    TokenPair,
    unauthorized,
)
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from security.audit import record_audit


def _ip(client: ClientInfo) -> str | None:
    try:
        return str(ipaddress.ip_address(client.ip)) if client.ip else None
    except ValueError:
        return None


class SupabaseAuthProvider(AuthProviderBase):
    name = "supabase"

    def __init__(self, db: Database, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        assert settings.supabase_url and settings.supabase_anon_key and settings.supabase_jwt_secret
        self.db = db
        self.settings = settings
        self.base = settings.supabase_url.rstrip("/") + "/auth/v1"
        self.http = http or httpx.AsyncClient(timeout=15)
        self._headers = {"apikey": settings.supabase_anon_key.get_secret_value(), "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict, bearer: str | None = None, method: str = "POST") -> dict:
        headers = dict(self._headers)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        resp = await self.http.request(method, self.base + path, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            msg = data.get("msg") or data.get("error_description") or data.get("message") or "Erro de autenticação"
            code = data.get("error_code") or data.get("error") or "auth_error"
            status = 401 if resp.status_code in (400, 401) and "token" in path else resp.status_code
            if code in ("email_not_confirmed",):
                raise AuthError(403, "email_not_verified", "Confirma o teu email antes de entrar")
            if code in ("invalid_credentials", "invalid_grant"):
                raise AuthError(401, "invalid_credentials", "Email ou palavra-passe incorretos")
            if code in ("user_already_exists", "email_exists"):
                raise AuthError(409, "email_taken", "Já existe uma conta com este email")
            raise AuthError(status if 400 <= status < 600 else 502, str(code), str(msg))
        return resp.json() if resp.content else {}

    async def _ensure_account(self, user_id: uuid.UUID, email: str, verified: bool, display_name: str | None = None) -> None:
        async with self.db.service() as conn:
            exists = await conn.fetchval("select true from public.users where id = $1", user_id)
            if not exists:
                await create_account(conn, email=email, display_name=display_name, user_id=user_id, email_verified=verified)
            elif verified:
                await conn.execute(
                    "update public.users set email_verified_at = coalesce(email_verified_at, now()) where id = $1", user_id)

    def _pair(self, data: dict) -> TokenPair:
        return TokenPair(data["access_token"], data["refresh_token"], int(data.get("expires_in", 3600)),
                         uuid.UUID(data["user"]["id"]))

    async def register(self, email: str, password: str, display_name: str | None, client: ClientInfo) -> RegistrationResult:
        problems = password_problems(password)
        if problems:
            raise AuthError(422, "weak_password", "Palavra-passe fraca: " + "; ".join(problems))
        data = await self._post("/signup", {
            "email": email, "password": password, "data": {"display_name": display_name},
            "options": {"email_redirect_to": f"{self.settings.frontend_url}/verify-email"},
        })
        user = data.get("user") or data
        user_id = uuid.UUID(user["id"])
        verified = bool(user.get("email_confirmed_at"))
        await self._ensure_account(user_id, email, verified, display_name)
        async with self.db.service() as conn:
            await record_audit(conn, "auth.register", user_id=user_id, ip=_ip(client), user_agent=client.user_agent,
                               details={"provider": "supabase"})
        return RegistrationResult(user_id, not verified)

    async def login(self, email: str, password: str, client: ClientInfo) -> TokenPair:
        data = await self._post("/token?grant_type=password", {"email": email, "password": password})
        pair = self._pair(data)
        await self._ensure_account(pair.user_id, data["user"]["email"], bool(data["user"].get("email_confirmed_at")))
        async with self.db.service() as conn:
            await conn.execute("update public.users set last_login_at = now() where id = $1", pair.user_id)
            await record_audit(conn, "auth.login", user_id=pair.user_id, ip=_ip(client), user_agent=client.user_agent,
                               details={"provider": "supabase"})
        return pair

    async def refresh(self, refresh_token: str, client: ClientInfo) -> TokenPair:
        try:
            return self._pair(await self._post("/token?grant_type=refresh_token", {"refresh_token": refresh_token}))
        except AuthError as exc:
            raise unauthorized() from exc

    async def logout(self, principal: Principal | None, refresh_token: str | None, client: ClientInfo) -> None:
        # o Supabase revoga pela sessão do access token; sem ele não há o que revogar do nosso lado
        if principal is not None and getattr(principal, "raw_token", None):
            await self._post("/logout", {}, bearer=principal.raw_token)  # type: ignore[attr-defined]

    async def verify_email(self, token: str, client: ClientInfo) -> None:
        data = await self._post("/verify", {"type": "email", "token_hash": token})
        user = data.get("user") or {}
        if user.get("id"):
            await self._ensure_account(uuid.UUID(user["id"]), user.get("email", ""), True)

    async def resend_verification(self, email: str, client: ClientInfo) -> None:
        try:
            await self._post("/resend", {"type": "signup", "email": email})
        except AuthError:
            return  # não revela se o email existe

    async def request_password_reset(self, email: str, client: ClientInfo) -> None:
        try:
            await self._post("/recover", {"email": email, "redirect_to": f"{self.settings.frontend_url}/reset-password"})
        except AuthError:
            return

    async def reset_password(self, token: str, new_password: str, client: ClientInfo) -> None:
        problems = password_problems(new_password)
        if problems:
            raise AuthError(422, "weak_password", "Palavra-passe fraca: " + "; ".join(problems))
        session = await self._post("/verify", {"type": "recovery", "token_hash": token})
        await self._post("/user", {"password": new_password}, bearer=session["access_token"], method="PUT")

    async def authenticate(self, access_token: str) -> Principal:
        try:
            claims = jwt.decode(
                access_token, self.settings.supabase_jwt_secret.get_secret_value(),  # type: ignore[union-attr]
                algorithms=["HS256"], audience="authenticated", options={"require": ["exp", "sub"]}, leeway=5,
            )
            user_id = uuid.UUID(claims["sub"])
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise unauthorized() from exc
        sid = claims.get("session_id")
        principal = _SupabasePrincipal(user_id=user_id, email=claims.get("email", ""),
                                       session_id=uuid.UUID(sid) if sid else None)
        object.__setattr__(principal, "raw_token", access_token)
        return principal


class _SupabasePrincipal(Principal):
    raw_token: str = ""
