"""Definições do utilizador: perfil, palavra-passe, preferência de IA e sessões.

* Perfil e preferência de IA são dados do utilizador: leitura e escrita com ``db.as_user`` (RLS).
* Palavra-passe e sessões vivem em ``local_auth`` (inacessível ao papel authenticated): correm em
  modo serviço, sempre filtradas pelo utilizador da sessão. Só existem com AUTH_PROVIDER=local.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import Field, field_validator

from ai_core.preferences import (
    DEFAULT_FALLBACK,
    DEFAULT_PROVIDER,
    PROVIDER_CHOICES,
    PROVIDER_LABELS,
    allow_paid_providers,
    is_paid,
    load_preference,
)
from ai_core.providers import PROVIDERS
from ai_core.router import AIRouter
from api.deps import client_info, current_principal, enforce_limit, get_db, safe_ip
from api.schemas import _In
from authentication.passwords import hash_password, password_problems, verify_password
from authentication.tokens import Principal
from core.config import AuthProvider, Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

router = APIRouter(prefix="/api/settings", tags=["settings"])

_MODEL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-")


# --- esquemas ---------------------------------------------------------------------------------------
class ProfileIn(_In):
    display_name: str | None = Field(default=None, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=500)

    @field_validator("avatar_url")
    @classmethod
    def _avatar(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        parsed = urlparse(v)
        if parsed.scheme != "https" or not parsed.netloc or any(c.isspace() for c in v):
            raise ValueError("o avatar tem de ser um URL https://")
        return v


class PasswordChangeIn(_In):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class AIPreferenceIn(_In):
    provider: str = Field(default=DEFAULT_PROVIDER, max_length=20)
    model: str | None = Field(default=None, max_length=200)
    fallback_provider: str | None = Field(default=DEFAULT_FALLBACK, max_length=20)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in PROVIDER_CHOICES:
            raise ValueError("fornecedor desconhecido")
        return v

    @field_validator("fallback_provider")
    @classmethod
    def _fallback(cls, v: str | None) -> str | None:
        if v in (None, "", "none"):
            return None
        if v == "auto" or v not in PROVIDER_CHOICES:
            raise ValueError("fornecedor de recurso desconhecido")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v[0].isalnum() or any(c not in _MODEL_CHARS for c in v):
            raise ValueError("nome de modelo inválido")
        return v


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_local_auth(request: Request) -> None:
    if _settings(request).auth_provider is not AuthProvider.local:
        raise ApiError(501, "not_supported", "Disponível só com a autenticação própria (AUTH_PROVIDER=local)")


# --- perfil -----------------------------------------------------------------------------------------
async def _profile(db: Database, user_id: uuid.UUID) -> dict:
    async with db.as_user(user_id) as conn:
        row = await conn.fetchrow(
            """select u.email::text as email, u.email_verified_at is not null as email_verified, u.created_at,
                      p.display_name, p.avatar_url
               from public.users u left join public.user_profiles p on p.user_id = u.id where u.id = $1""",
            user_id)
    if row is None:
        raise ApiError(404, "not_found", "Utilizador não encontrado")
    return {"email": row["email"], "email_verified": row["email_verified"], "display_name": row["display_name"],
            "avatar_url": row["avatar_url"], "created_at": row["created_at"].isoformat()}


@router.get("/profile")
async def get_profile(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    return await _profile(db, principal.user_id)


@router.patch("/profile")
async def update_profile(body: ProfileIn, principal: Principal = Depends(current_principal),
                         db: Database = Depends(get_db)) -> dict:
    fields = body.model_fields_set
    if not fields:
        return await _profile(db, principal.user_id)
    display_name = (body.display_name or "").strip() or None
    avatar_url = body.avatar_url or None
    async with db.as_user(principal.user_id) as conn:
        await conn.execute(
            """update public.user_profiles set
                 display_name = case when $2 then $3 else display_name end,
                 avatar_url = case when $4 then $5 else avatar_url end
               where user_id = $1""",
            principal.user_id, "display_name" in fields, display_name, "avatar_url" in fields, avatar_url)
    return await _profile(db, principal.user_id)


# --- palavra-passe ----------------------------------------------------------------------------------
@router.post("/password")
async def change_password(body: PasswordChangeIn, request: Request, principal: Principal = Depends(current_principal),
                          db: Database = Depends(get_db)) -> dict:
    """Confirma a palavra-passe atual, grava a nova (argon2id) e fecha as outras sessões."""
    _require_local_auth(request)
    await enforce_limit(request, "password_change", str(principal.user_id),
                        _settings(request).auth_rate_limit_per_15min, 15 * 60)
    problems = password_problems(body.new_password)
    if problems:
        raise ApiError(422, "weak_password", "Palavra-passe fraca: " + "; ".join(problems))
    client = client_info(request)
    failure: ApiError | None = None
    revoked = 0
    async with db.service() as conn:
        current_hash = await conn.fetchval(
            "select password_hash from local_auth.credentials where user_id = $1 for update", principal.user_id)
        if not verify_password(current_hash, body.current_password):
            await record_audit(conn, "settings.password_change_failed", user_id=principal.user_id,
                               ip=safe_ip(client.ip), user_agent=client.user_agent)
            failure = ApiError(400, "invalid_current_password", "A palavra-passe atual está incorreta")
        elif verify_password(current_hash, body.new_password):
            failure = ApiError(422, "same_password", "A nova palavra-passe tem de ser diferente da atual")
        else:
            await conn.execute(
                """update local_auth.credentials set password_hash = $2, password_updated_at = now(),
                          failed_attempts = 0, locked_until = null where user_id = $1""",
                principal.user_id, hash_password(body.new_password))
            revoked = len(await conn.fetch(
                """update local_auth.sessions set revoked_at = now(), revoked_reason = 'password_changed'
                   where user_id = $1 and revoked_at is null and id is distinct from $2 returning id""",
                principal.user_id, principal.session_id))
            await record_audit(conn, "settings.password_changed", user_id=principal.user_id, ip=safe_ip(client.ip),
                               user_agent=client.user_agent, details={"revoked_sessions": revoked})
    if failure is not None:  # depois do commit: a auditoria da falha fica gravada
        raise failure
    return {"status": "changed", "revoked_sessions": revoked}


# --- preferência de IA ------------------------------------------------------------------------------
async def _ai_options(request: Request, allow_paid: bool) -> list[dict]:
    ai: AIRouter = request.app.state.ai_router
    engine = await ai.local_engine()
    active = {p.provider for p in await ai.active_providers()}
    options = [{"provider": "auto", "label": PROVIDER_LABELS["auto"], "paid": False, "allowed": True,
                "available": True},
               {"provider": "ollama", "label": PROVIDER_LABELS["ollama"], "paid": False, "allowed": True,
                "available": bool(engine and engine.usable)}]
    for p in PROVIDERS:
        options.append({"provider": p, "label": PROVIDER_LABELS[p], "paid": is_paid(p),
                        "allowed": allow_paid or not is_paid(p), "available": p in active})
    return options


async def _ai_out(request: Request, db: Database, user_id: uuid.UUID) -> dict:
    async with db.as_user(user_id) as conn:
        pref = await load_preference(conn, user_id)
    async with db.service() as conn:
        allow_paid = await allow_paid_providers(conn)
    return {"provider": pref.provider, "model": pref.model, "fallback_provider": pref.fallback_provider,
            "saved": pref.saved, "allow_paid_providers": allow_paid,
            "options": await _ai_options(request, allow_paid)}


@router.get("/ai")
async def get_ai_preference(request: Request, principal: Principal = Depends(current_principal),
                            db: Database = Depends(get_db)) -> dict:
    return await _ai_out(request, db, principal.user_id)


@router.put("/ai")
async def set_ai_preference(body: AIPreferenceIn, request: Request, principal: Principal = Depends(current_principal),
                            db: Database = Depends(get_db)) -> dict:
    if body.fallback_provider and body.fallback_provider == body.provider:
        raise ApiError(422, "validation_error", "O fornecedor de recurso tem de ser diferente do principal")
    async with db.service() as conn:
        allow_paid = await allow_paid_providers(conn)
    for p in (body.provider, body.fallback_provider):
        if p and is_paid(p) and not allow_paid:
            raise ApiError(403, "paid_provider_disabled",
                           "Fornecedores pagos estão desligados. Um administrador tem de os autorizar.")
    model = body.model if body.provider != "auto" else None
    async with db.as_user(principal.user_id) as conn:
        await conn.execute(
            """insert into public.user_ai_preferences (user_id, provider, model, fallback_provider)
               values ($1, $2, $3, $4)
               on conflict (user_id) do update set provider = excluded.provider, model = excluded.model,
                 fallback_provider = excluded.fallback_provider""",
            principal.user_id, body.provider, model, body.fallback_provider)
    return await _ai_out(request, db, principal.user_id)


@router.delete("/ai", status_code=status.HTTP_204_NO_CONTENT)
async def reset_ai_preference(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> Response:
    """Volta aos valores por defeito (modo automático no chat)."""
    async with db.as_user(principal.user_id) as conn:
        await conn.execute("delete from public.user_ai_preferences where user_id = $1", principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- sessões ----------------------------------------------------------------------------------------
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


@router.get("/sessions")
async def list_sessions(request: Request, principal: Principal = Depends(current_principal),
                        db: Database = Depends(get_db)) -> dict:
    _require_local_auth(request)
    async with db.service() as conn:
        rows = await conn.fetch(
            """select id, created_at, last_used_at, expires_at, host(ip) as ip, user_agent
               from local_auth.sessions
               where user_id = $1 and revoked_at is null and expires_at > now()
               order by last_used_at desc, created_at desc""",
            principal.user_id)
    items = [{"id": str(r["id"]), "created_at": _iso(r["created_at"]), "last_used_at": _iso(r["last_used_at"]),
              "expires_at": _iso(r["expires_at"]), "ip": r["ip"], "user_agent": r["user_agent"],
              "current": r["id"] == principal.session_id} for r in rows]
    items.sort(key=lambda i: not i["current"])  # a sessão atual primeiro
    return {"items": items}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal),
                         db: Database = Depends(get_db)) -> Response:
    _require_local_auth(request)
    if session_id == principal.session_id:
        raise ApiError(400, "cannot_revoke_current", "Para fechar esta sessão usa Sair")
    client = client_info(request)
    async with db.service() as conn:
        done = await conn.fetchval(
            """update local_auth.sessions set revoked_at = now(), revoked_reason = 'user_revoked'
               where id = $1 and user_id = $2 and revoked_at is null returning true""",
            session_id, principal.user_id)
        if done:
            await record_audit(conn, "settings.session_revoked", user_id=principal.user_id, ip=safe_ip(client.ip),
                               user_agent=client.user_agent, details={"session_id": str(session_id)})
    if not done:
        raise ApiError(404, "not_found", "Sessão não encontrada")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, principal: Principal = Depends(current_principal),
                                db: Database = Depends(get_db)) -> dict:
    _require_local_auth(request)
    client = client_info(request)
    async with db.service() as conn:
        revoked = len(await conn.fetch(
            """update local_auth.sessions set revoked_at = now(), revoked_reason = 'user_revoked_others'
               where user_id = $1 and revoked_at is null and id is distinct from $2 returning id""",
            principal.user_id, principal.session_id))
        await record_audit(conn, "settings.sessions_revoked", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"count": revoked})
    return {"revoked": revoked}
