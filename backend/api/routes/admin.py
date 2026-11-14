"""Administração: estatísticas, utilizadores e fornecedores de IA.

Todas as rotas exigem ``require_admin``. As leituras globais correm em modo serviço
(é uma operação de sistema). As chaves dos fornecedores nunca saem do servidor: a API
devolve só se há chave e os últimos 4 caracteres.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator

from ai_core.keys import decrypt_api_key, encrypt_api_key, last4, mask
from ai_core.providers import PAID_PROVIDERS, PROVIDERS, ChatMessage, ProviderError, make_client
from ai_core.router import AIRouter
from api.deps import client_info, enforce_limit, get_db, require_admin, safe_ip
from api.schemas import _In
from connector_hub import oauth
from connector_hub.registry import CONNECTION_SPECS
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit
from security.crypto import DecryptionError

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
TEST_PROMPT = "Responde apenas com a palavra: ok"


class ProviderUpdateIn(_In):
    api_key: str | None = Field(default=None, max_length=512)
    clear_key: bool = False
    model: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    @field_validator("api_key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 10 or any(c.isspace() for c in v):
            raise ValueError("chave inválida")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _MODEL_RE.match(v):
            raise ValueError("nome de modelo inválido")
        return v


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _default_model(settings: Settings, provider: str) -> str:
    return {
        "gemini": settings.gemini_default_model,
        "groq": settings.groq_default_model,
        "openrouter": settings.openrouter_default_model,
    }.get(provider, settings.openai_default_model)


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ApiError(404, "unknown_provider", "Fornecedor desconhecido (usa gemini, groq, openrouter ou openai)")
    return provider


def _provider_out(provider: str, row: dict | None, settings: Settings) -> dict:
    configured = bool(row and row["api_key_ciphertext"] is not None)
    order = settings.ai_provider_order
    return {
        "provider": provider,
        "configured": configured,
        "key_last4": row["api_key_last4"] if configured and row else None,
        "key_masked": mask(row["api_key_last4"]) if configured and row else None,
        "model": row["model"] if row else _default_model(settings, provider),
        "default_model": _default_model(settings, provider),
        "enabled": bool(row and row["enabled"]),
        "priority": order.index(provider) + 1 if provider in order else len(order) + 1,
        "updated_at": row["updated_at"].isoformat() if row else None,
        "updated_by_email": row["updated_by_email"] if row else None,
        "last_tested_at": row["last_tested_at"].isoformat() if row and row["last_tested_at"] else None,
        "last_test_ok": row["last_test_ok"] if row else None,
        "last_test_message": row["last_test_message"] if row else None,
    }


_SELECT = """select p.provider, p.api_key_ciphertext, p.api_key_last4, p.model, p.enabled, p.updated_at,
                    p.last_tested_at, p.last_test_ok, p.last_test_message, u.email::text as updated_by_email
             from public.ai_providers p left join public.users u on u.id = p.updated_by"""


@router.get("/stats")
async def stats(_: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    async with db.service() as conn:
        row = await conn.fetchrow(
            """select
                 (select count(*)::int from public.users where deleted_at is null) as users,
                 (select count(*)::int from public.users where is_admin) as admins,
                 (select count(*)::int from public.users where created_at >= now() - interval '7 days') as users_last_7d,
                 (select count(*)::int from public.conversations) as conversations,
                 (select count(*)::int from public.messages) as messages,
                 (select count(*)::int from public.messages where created_at >= now() - interval '24 hours') as messages_last_24h,
                 (select count(*)::int from public.ai_providers where enabled and api_key_ciphertext is not null) as active_providers""")
    return dict(row)


@router.get("/users")
async def users(
    _: Principal = Depends(require_admin),
    db: Database = Depends(get_db),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(20, ge=1, le=100),
    q: str | None = Query(None, max_length=200),
) -> dict:
    pattern = f"%{q.strip()}%" if q and q.strip() else None
    async with db.service() as conn:
        total = await conn.fetchval(
            "select count(*)::int from public.users u where ($1::text is null or u.email::text ilike $1)", pattern)
        rows = await conn.fetch(
            """select u.id, u.email::text as email, u.is_admin, u.status, u.email_verified_at is not null as email_verified,
                      u.created_at, u.last_login_at, p.display_name,
                      (select count(*)::int from public.conversations c where c.user_id = u.id) as conversations,
                      (select count(*)::int from public.messages m where m.user_id = u.id) as messages
               from public.users u left join public.user_profiles p on p.user_id = u.id
               where ($1::text is null or u.email::text ilike $1)
               order by u.created_at desc
               limit $2 offset $3""",
            pattern, page_size, (page - 1) * page_size)
    items = []
    for r in rows:
        item = dict(r)
        item["id"] = str(item["id"])
        for k in ("created_at", "last_login_at"):
            item[k] = item[k].isoformat() if isinstance(item[k], datetime) else None
        items.append(item)
    return {"items": items, "page": page, "page_size": page_size, "total": total,
            "pages": max(1, -(-total // page_size))}


@router.get("/providers")
async def list_providers(request: Request, _: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    settings = _settings(request)
    async with db.service() as conn:
        rows = {r["provider"]: dict(r) for r in await conn.fetch(_SELECT)}
    items = [_provider_out(p, rows.get(p), settings) for p in PROVIDERS]
    items.sort(key=lambda i: i["priority"])
    return {"items": items, "encryption_ready": request.app.state.cipher is not None,
            "order": settings.ai_provider_order}


@router.put("/providers/{provider}")
async def update_provider(provider: str, body: ProviderUpdateIn, request: Request,
                          principal: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    _check_provider(provider)
    settings = _settings(request)
    cipher = request.app.state.cipher
    if body.api_key and cipher is None:
        raise ApiError(503, "encryption_not_configured",
                       "CREDENTIALS_ENCRYPTION_KEY não está configurada no servidor; não é possível guardar chaves.")
    client = client_info(request)
    async with db.service() as conn:
        current = await conn.fetchrow("select * from public.ai_providers where provider = $1 for update", provider)
        has_key = bool(current and current["api_key_ciphertext"] is not None)
        ct = current["api_key_ciphertext"] if current else None
        nonce = current["api_key_nonce"] if current else None
        key_id = current["api_key_key_id"] if current else None
        l4 = current["api_key_last4"] if current else None
        key_changed = False
        if body.clear_key:
            ct = nonce = key_id = l4 = None
            has_key, key_changed = False, True
        if body.api_key:
            blob = encrypt_api_key(cipher, provider, body.api_key)
            ct, nonce, key_id, l4 = blob.ciphertext, blob.nonce, blob.key_id, last4(body.api_key)
            has_key, key_changed = True, True
        model = body.model or (current["model"] if current else _default_model(settings, provider))
        enabled = body.enabled if body.enabled is not None else bool(current and current["enabled"])
        if enabled and not has_key:
            raise ApiError(422, "key_required", "Define uma chave antes de ativar este fornecedor")
        await conn.execute(
            """insert into public.ai_providers (provider, api_key_ciphertext, api_key_nonce, api_key_key_id, api_key_last4,
                                                model, enabled, updated_by)
               values ($1, $2, $3, $4, $5, $6, $7, $8)
               on conflict (provider) do update set
                 api_key_ciphertext = excluded.api_key_ciphertext, api_key_nonce = excluded.api_key_nonce,
                 api_key_key_id = excluded.api_key_key_id, api_key_last4 = excluded.api_key_last4,
                 model = excluded.model, enabled = excluded.enabled, updated_by = excluded.updated_by,
                 last_tested_at = case when $9 then null else public.ai_providers.last_tested_at end,
                 last_test_ok = case when $9 then null else public.ai_providers.last_test_ok end,
                 last_test_message = case when $9 then null else public.ai_providers.last_test_message end""",
            provider, ct, nonce, key_id, l4, model, enabled, principal.user_id, key_changed,
        )
        changes: dict = {"provider": provider, "key_changed": key_changed, "key_cleared": body.clear_key,
                         "model": model, "enabled": enabled}
        if current:
            changes["previous"] = {"model": current["model"], "enabled": current["enabled"]}
        await record_audit(conn, "admin.ai_provider_updated", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details=changes)
        row = await conn.fetchrow(_SELECT + " where p.provider = $1", provider)
    return _provider_out(provider, dict(row), settings)


@router.post("/providers/{provider}/test")
async def test_provider(provider: str, request: Request, principal: Principal = Depends(require_admin),
                        db: Database = Depends(get_db)) -> dict:
    _check_provider(provider)
    await enforce_limit(request, "admin_provider_test", str(principal.user_id), 30, 15 * 60)
    settings = _settings(request)
    cipher = request.app.state.cipher
    async with db.service() as conn:
        row = await conn.fetchrow("select * from public.ai_providers where provider = $1", provider)
    if row is None or row["api_key_ciphertext"] is None:
        raise ApiError(400, "key_required", "Este fornecedor ainda não tem chave")
    if cipher is None:
        raise ApiError(503, "encryption_not_configured", "CREDENTIALS_ENCRYPTION_KEY não está configurada no servidor")
    try:
        key = decrypt_api_key(cipher, provider, key_id=row["api_key_key_id"], nonce=row["api_key_nonce"],
                              ciphertext=row["api_key_ciphertext"])
    except DecryptionError:
        ok, message, reply_text = False, "A chave guardada não decifra (a CREDENTIALS_ENCRYPTION_KEY mudou?). Volta a colar a chave.", None
        latency_ms = 0
    else:
        ai_router: AIRouter = request.app.state.ai_router
        ai = make_client(provider, key, row["model"], timeout=min(settings.ai_request_timeout_seconds, 30),
                         http=ai_router.http)
        started = time.perf_counter()
        try:
            reply = await ai.generate([ChatMessage("user", TEST_PROMPT)])
            ok, message, reply_text = True, "Ligação OK", reply.text[:200]
        except ProviderError as exc:
            ok, message, reply_text = False, exc.message, None
        latency_ms = int((time.perf_counter() - started) * 1000)
    client = client_info(request)
    async with db.service() as conn:
        await conn.execute(
            """update public.ai_providers set last_tested_at = now(), last_test_ok = $2, last_test_message = $3
               where provider = $1""", provider, ok, message[:500])
        await record_audit(conn, "admin.ai_provider_tested", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent,
                           details={"provider": provider, "model": row["model"], "ok": ok, "latency_ms": latency_ms})
    return {"provider": provider, "model": row["model"], "ok": ok, "message": message, "reply": reply_text,
            "latency_ms": latency_ms}


# --- política de IA para os utilizadores --------------------------------------------------------------
class AIPolicyIn(_In):
    allow_paid_providers: bool


async def _policy_out(conn) -> dict:
    row = await conn.fetchrow(
        """select p.allow_paid_providers, p.updated_at, u.email::text as updated_by_email
           from public.ai_policy p left join public.users u on u.id = p.updated_by where p.id = 1""")
    return {"allow_paid_providers": bool(row and row["allow_paid_providers"]),
            "paid_providers": sorted(PAID_PROVIDERS),
            "updated_at": row["updated_at"].isoformat() if row else None,
            "updated_by_email": row["updated_by_email"] if row else None}


@router.get("/ai-policy")
async def get_ai_policy(_: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    async with db.service() as conn:
        return await _policy_out(conn)


@router.put("/ai-policy")
async def set_ai_policy(body: AIPolicyIn, request: Request, principal: Principal = Depends(require_admin),
                        db: Database = Depends(get_db)) -> dict:
    """Autoriza (ou não) que os utilizadores escolham fornecedores pagos nas Definições."""
    client = client_info(request)
    async with db.service() as conn:
        await conn.execute(
            """insert into public.ai_policy (id, allow_paid_providers, updated_by) values (1, $1, $2)
               on conflict (id) do update set allow_paid_providers = excluded.allow_paid_providers,
                 updated_by = excluded.updated_by""",
            body.allow_paid_providers, principal.user_id)
        await record_audit(conn, "admin.ai_policy_updated", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"allow_paid_providers": body.allow_paid_providers})
        return await _policy_out(conn)


# --- conectores -----------------------------------------------------------------------------------------
@router.get("/connectors")
async def connectors_overview(request: Request, _: Principal = Depends(require_admin),
                              db: Database = Depends(get_db)) -> dict:
    """Estado de cada fornecedor de ligação: OAuth configurado (sem mostrar segredos) e contas ligadas."""
    settings = _settings(request)
    async with db.service() as conn:
        rows = {r["key"]: dict(r) for r in await conn.fetch(
            """select d.key, d.name, d.status,
                      (select count(*)::int from public.connected_apps a
                        where a.connector_definition_id = d.id and a.status = 'connected') as connected,
                      (select count(*)::int from public.connected_apps a
                        where a.connector_definition_id = d.id and a.status in ('expired', 'error')) as failing
               from public.connector_definitions d""")}
    items = []
    for key, spec in CONNECTION_SPECS.items():
        row = rows.get(key) or {"name": key, "status": "missing", "connected": 0, "failing": 0}
        provider = spec.oauth_provider
        env = oauth.ENV_VARS.get(provider or "", ())
        items.append({
            "key": key, "name": row["name"], "catalog_status": row["status"], "method": spec.method,
            "oauth_provider": provider,
            "oauth_configured": oauth.oauth_configured(settings, provider) if provider else None,
            "env_vars": [{"name": name, "set": _env_set(settings, name)} for name in env],
            "redirect_uri": oauth.redirect_uri(settings, provider) if provider else None,
            "connected_accounts": row["connected"], "failing_accounts": row["failing"],
        })
    return {"items": items, "encryption_ready": request.app.state.cipher is not None,
            "redirect_base": settings.oauth_redirect_base}


def _env_set(settings: Settings, env_name: str) -> bool:
    value = getattr(settings, env_name.lower(), None)
    if value is None:
        return False
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    return bool(raw.strip())
