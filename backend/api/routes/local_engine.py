"""Administração do motor local (Ollama) e gestor de modelos.

Só administradores. O URL base e o token ficam cifrados na BD e nunca voltam ao
navegador: a API devolve só uma pista (esquema + anfitrião) e os últimos 4 caracteres
do token. Descarregar um modelo só acontece por ação explícita do admin e é recusado
quando o catálogo indica mais RAM do que o limite configurado (salvo confirmação).
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator

from ai_core import model_catalog
from ai_core.keys import encrypt_api_key, last4, mask
from ai_core.local_engine import SELECT_ENGINE, TOKEN_SECRET, URL_SECRET, LocalEngine, engine_from_row
from ai_core.ollama import NOT_CONFIGURED_MESSAGE, OllamaClient, OllamaManager, normalise_base_url, url_hint
from ai_core.providers import ChatMessage, ProviderError
from ai_core.router import AIRouter
from api.deps import client_info, enforce_limit, get_db, require_admin, safe_ip
from api.schemas import _In
from api.sse import HEARTBEAT, SSE_HEADERS, SSE_PING, sse_event, with_heartbeat
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

router = APIRouter(prefix="/api/admin/local-engine", tags=["admin", "local-engine"])

MODEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,149}(:[A-Za-z0-9._-]{1,80})?$")
TEST_PROMPT = "Responde apenas com a palavra: ok"


def _check_model_name(v: str) -> str:
    if not MODEL_NAME_RE.match(v):
        raise ValueError("nome de modelo inválido (ex.: qwen3:1.7b)")
    return v


class EngineUpdateIn(_In):
    base_url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=1024)
    clear_token: bool = False
    clear_base_url: bool = False
    enabled: bool | None = None
    api_mode: str | None = Field(default=None, pattern="^(native|openai)$")
    default_model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=32768)
    context_length: int | None = Field(default=None, ge=512, le=131072)
    think: bool | None = None
    ram_limit_gb: float | None = Field(default=None, gt=0, le=1024)
    allow_cloud_fallback: bool | None = None
    allow_paid_fallback: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return normalise_base_url(v)

    @field_validator("token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 8 or any(c.isspace() for c in v):
            raise ValueError("token inválido (mínimo 8 caracteres, sem espaços)")
        return v

    @field_validator("default_model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return _check_model_name(v)


class PullIn(_In):
    name: str = Field(min_length=1, max_length=200)
    confirm_over_limit: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_model_name(v)


class TestIn(_In):
    model: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=2000)

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        return _check_model_name(v) if v else None


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _engine_out(row: dict | None, engine: LocalEngine | None) -> dict:
    r = row or {}
    return {
        "configured": bool(r.get("base_url_ciphertext") is not None),
        "base_url_hint": r.get("base_url_hint"),
        "token_configured": r.get("token_ciphertext") is not None,
        "token_masked": mask(r.get("token_last4")) if r.get("token_ciphertext") is not None else None,
        "enabled": bool(r.get("enabled")),
        "api_mode": r.get("api_mode") or "native",
        "default_model": r.get("default_model"),
        "temperature": float(r["temperature"]) if r.get("temperature") is not None else 0.7,
        "max_tokens": r.get("max_tokens") or 1024,
        "context_length": r.get("context_length") or 4096,
        "think": bool(r.get("think")),
        "ram_limit_gb": float(r["ram_limit_gb"]) if r.get("ram_limit_gb") is not None else 8.0,
        "allow_cloud_fallback": bool(r.get("allow_cloud_fallback")),
        "allow_paid_fallback": bool(r.get("allow_paid_fallback")),
        "priority": 1,
        "warning": None if (engine and engine.usable) else (engine.error if engine and engine.error else NOT_CONFIGURED_MESSAGE),
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
        "updated_by_email": r.get("updated_by_email"),
        "last_tested_at": r["last_tested_at"].isoformat() if r.get("last_tested_at") else None,
        "last_test_ok": r.get("last_test_ok"),
        "last_test_message": r.get("last_test_message"),
    }


async def _load(request: Request, db: Database) -> tuple[dict | None, LocalEngine | None]:
    async with db.service() as conn:
        row = await conn.fetchrow(SELECT_ENGINE)
    row_d = dict(row) if row else None
    engine = engine_from_row(row_d, request.app.state.cipher, _settings(request).local_engine_timeout_seconds)
    return row_d, engine


async def _manager(request: Request, db: Database, timeout: float = 15) -> tuple[OllamaManager, LocalEngine]:
    _, engine = await _load(request, db)
    if engine is None or engine.config is None:
        raise ApiError(409, "local_engine_not_configured",
                       (engine.error if engine and engine.error else None) or NOT_CONFIGURED_MESSAGE)
    ai: AIRouter = request.app.state.ai_router
    return OllamaManager(engine.config.base_url, engine.config.token, timeout=timeout, http=ai.http), engine


def _provider_error(exc: ProviderError) -> ApiError:
    status = 503 if exc.code == "local_engine_unreachable" else 502
    return ApiError(status, exc.code or "local_engine_error", exc.message[:500])


async def _audit(request: Request, db: Database, principal: Principal, action: str, details: dict) -> None:
    client = client_info(request)
    async with db.service() as conn:
        await record_audit(conn, action, user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details=details)


# --- configuração ------------------------------------------------------------------------------
@router.get("")
async def get_engine(request: Request, _: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    row, engine = await _load(request, db)
    out = _engine_out(row, engine)
    out["encryption_ready"] = request.app.state.cipher is not None
    return out


@router.put("")
async def update_engine(body: EngineUpdateIn, request: Request, principal: Principal = Depends(require_admin),
                        db: Database = Depends(get_db)) -> dict:
    cipher = request.app.state.cipher
    if (body.base_url or body.token) and cipher is None:
        raise ApiError(503, "encryption_not_configured",
                       "CREDENTIALS_ENCRYPTION_KEY não está configurada no servidor; não é possível guardar o URL/token.")
    async with db.service() as conn:
        cur = await conn.fetchrow("select * from public.local_ai_engine where id = 1 for update")
        c = dict(cur) if cur else {}
        url_ct, url_nonce, url_kid, hint = (c.get("base_url_ciphertext"), c.get("base_url_nonce"),
                                            c.get("base_url_key_id"), c.get("base_url_hint"))
        tok_ct, tok_nonce, tok_kid, tok_l4 = (c.get("token_ciphertext"), c.get("token_nonce"),
                                              c.get("token_key_id"), c.get("token_last4"))
        secret_changed = False
        if body.clear_base_url:
            url_ct = url_nonce = url_kid = hint = None
            secret_changed = True
        if body.base_url:
            blob = encrypt_api_key(cipher, URL_SECRET, body.base_url)
            url_ct, url_nonce, url_kid, hint = blob.ciphertext, blob.nonce, blob.key_id, url_hint(body.base_url)
            secret_changed = True
        if body.clear_token:
            tok_ct = tok_nonce = tok_kid = tok_l4 = None
            secret_changed = True
        if body.token:
            blob = encrypt_api_key(cipher, TOKEN_SECRET, body.token)
            tok_ct, tok_nonce, tok_kid, tok_l4 = blob.ciphertext, blob.nonce, blob.key_id, last4(body.token)
            secret_changed = True

        def pick(field: str, default):
            v = getattr(body, field)
            return v if v is not None else c.get(field, default)

        default_model = body.default_model if body.default_model is not None else c.get("default_model")
        enabled = pick("enabled", False)
        if enabled and url_ct is None:
            raise ApiError(422, "base_url_required", "Define o URL do motor local antes de o ativar")
        if enabled and not default_model:
            raise ApiError(422, "default_model_required", "Escolhe um modelo por defeito antes de ativar o motor local")
        allow_cloud = pick("allow_cloud_fallback", False)
        allow_paid = pick("allow_paid_fallback", False)
        if allow_paid and not allow_cloud:
            raise ApiError(422, "invalid_fallback", "O recurso a fornecedores pagos exige o recurso à nuvem ligado")
        await conn.execute(
            """insert into public.local_ai_engine (id, base_url_ciphertext, base_url_nonce, base_url_key_id, base_url_hint,
                   token_ciphertext, token_nonce, token_key_id, token_last4, enabled, api_mode, default_model,
                   temperature, max_tokens, context_length, think, ram_limit_gb, allow_cloud_fallback,
                   allow_paid_fallback, updated_by)
               values (1, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
               on conflict (id) do update set
                 base_url_ciphertext = excluded.base_url_ciphertext, base_url_nonce = excluded.base_url_nonce,
                 base_url_key_id = excluded.base_url_key_id, base_url_hint = excluded.base_url_hint,
                 token_ciphertext = excluded.token_ciphertext, token_nonce = excluded.token_nonce,
                 token_key_id = excluded.token_key_id, token_last4 = excluded.token_last4,
                 enabled = excluded.enabled, api_mode = excluded.api_mode, default_model = excluded.default_model,
                 temperature = excluded.temperature, max_tokens = excluded.max_tokens,
                 context_length = excluded.context_length, think = excluded.think,
                 ram_limit_gb = excluded.ram_limit_gb, allow_cloud_fallback = excluded.allow_cloud_fallback,
                 allow_paid_fallback = excluded.allow_paid_fallback, updated_by = excluded.updated_by,
                 last_tested_at = case when $20 then null else public.local_ai_engine.last_tested_at end,
                 last_test_ok = case when $20 then null else public.local_ai_engine.last_test_ok end,
                 last_test_message = case when $20 then null else public.local_ai_engine.last_test_message end""",
            url_ct, url_nonce, url_kid, hint, tok_ct, tok_nonce, tok_kid, tok_l4, enabled,
            pick("api_mode", "native"), default_model, pick("temperature", 0.7), pick("max_tokens", 1024),
            pick("context_length", 4096), pick("think", False), pick("ram_limit_gb", 8.0), allow_cloud, allow_paid,
            principal.user_id, secret_changed)
        changes = {"secret_changed": secret_changed, "token_cleared": body.clear_token, "enabled": enabled,
                   "default_model": default_model, "allow_cloud_fallback": allow_cloud,
                   "allow_paid_fallback": allow_paid}
        client = client_info(request)
        await record_audit(conn, "admin.local_engine_updated", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details=changes)
    row, engine = await _load(request, db)
    out = _engine_out(row, engine)
    out["encryption_ready"] = cipher is not None
    return out


# --- estado e teste ----------------------------------------------------------------------------
@router.get("/status")
async def status(request: Request, _: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    row, engine = await _load(request, db)
    if engine is None or engine.config is None:
        return {"configured": False, "reachable": False, "version": None, "running": [],
                "warning": (engine.error if engine and engine.error else None) or NOT_CONFIGURED_MESSAGE}
    mgr, _ = await _manager(request, db, timeout=5)
    try:
        version = await mgr.version()
        running = await mgr.ps()
    except ProviderError as exc:
        return {"configured": True, "reachable": False, "version": None, "running": [], "warning": exc.message}
    return {"configured": True, "reachable": True, "version": version, "running": running,
            "warning": None if engine.usable else NOT_CONFIGURED_MESSAGE}


@router.post("/test")
async def test_engine(request: Request, body: TestIn | None = None, principal: Principal = Depends(require_admin),
                      db: Database = Depends(get_db)) -> dict:
    """Testa a ligação (versão + modelos) e, se houver modelo, uma resposta real curta."""
    await enforce_limit(request, "admin_local_engine_test", str(principal.user_id), 30, 15 * 60)
    mgr, engine = await _manager(request, db, timeout=10)
    assert engine.config is not None
    model = (body.model if body else None) or engine.default_model
    started = time.perf_counter()
    version, reply_text, installed = None, None, []
    try:
        version = await mgr.version()
        installed = [m["name"] for m in await mgr.tags()]
        if model:
            ai: AIRouter = request.app.state.ai_router
            client = OllamaClient(engine.config, model, http=ai.http)
            reply = await client.generate([ChatMessage("user", (body.prompt if body and body.prompt else TEST_PROMPT))])
            reply_text = reply.text[:500]
            ok, message = True, "Ligação OK"
        else:
            ok, message = True, "Ligação OK (sem modelo por defeito: escolhe um para testar respostas)"
    except ProviderError as exc:
        ok, message = False, exc.message
    latency_ms = int((time.perf_counter() - started) * 1000)
    async with db.service() as conn:
        await conn.execute(
            """update public.local_ai_engine set last_tested_at = now(), last_test_ok = $1, last_test_message = $2
               where id = 1""", ok, message[:500])
    await _audit(request, db, principal, "admin.local_engine_tested",
                 {"model": model, "ok": ok, "latency_ms": latency_ms})
    return {"ok": ok, "message": message, "version": version, "model": model, "reply": reply_text,
            "installed": installed, "latency_ms": latency_ms}


# --- modelos -------------------------------------------------------------------------------------
@router.get("/models")
async def installed_models(request: Request, _: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    mgr, engine = await _manager(request, db)
    try:
        models = await mgr.tags()
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    for m in models:
        info = model_catalog.find(m["name"] or "")
        m["is_default"] = m["name"] == engine.default_model
        m["ram_gb"] = info.ram_gb if info else None
        m["capabilities"] = list(info.capabilities) if info else []
        m["license"] = info.license if info else None
    return {"items": models, "default_model": engine.default_model}


@router.get("/catalog")
async def catalog(request: Request, _: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    """Catálogo curado. Funciona mesmo sem motor configurado (sem marcar instalados)."""
    _, engine = await _load(request, db)
    limit = engine.ram_limit_gb if engine else 8.0
    installed: set[str] = set()
    reachable = False
    if engine and engine.config:
        ai: AIRouter = request.app.state.ai_router
        mgr = OllamaManager(engine.config.base_url, engine.config.token, timeout=5, http=ai.http)
        try:
            installed = {m["name"] for m in await mgr.tags()}
            reachable = True
        except ProviderError:
            pass
    items = []
    for m in model_catalog.CATALOG:
        d = m.as_dict()
        d["installed"] = m.name in installed
        d["fits"] = m.ram_gb <= limit
        items.append(d)
    return {"items": items, "ram_limit_gb": limit, "reachable": reachable}


@router.post("/models/pull")
async def pull_model(body: PullIn, request: Request, principal: Principal = Depends(require_admin),
                     db: Database = Depends(get_db)) -> StreamingResponse:
    """Descarrega um modelo (progresso em SSE: ``progress``, ``done`` ou ``error``).

    Recusa (409 ``ram_limit_exceeded``) um modelo do catálogo que precisa de mais RAM que o
    limite, e um modelo fora do catálogo (RAM desconhecida), salvo ``confirm_over_limit``.
    """
    await enforce_limit(request, "admin_model_pull", str(principal.user_id), 10, 3600)
    mgr, engine = await _manager(request, db)
    info = model_catalog.find(body.name)
    if not body.confirm_over_limit:
        if info is None:
            raise ApiError(409, "ram_unknown",
                           f"'{body.name}' não está no catálogo: a RAM necessária é desconhecida. Confirma para descarregar.")
        if info.ram_gb > engine.ram_limit_gb:
            raise ApiError(409, "ram_limit_exceeded",
                           f"'{body.name}' precisa de ~{info.ram_gb:g} GB de RAM e o limite é {engine.ram_limit_gb:g} GB. "
                           "Confirma para descarregar mesmo assim.")
    await _audit(request, db, principal, "admin.local_model_pull",
                 {"model": body.name, "confirmed_over_limit": body.confirm_over_limit})
    timeout = _settings(request).local_engine_pull_timeout_seconds

    async def events():
        yield SSE_PING
        last_sent = 0.0
        try:
            async for ev in with_heartbeat(mgr.pull(body.name, timeout=timeout)):
                if ev is HEARTBEAT:
                    yield SSE_PING
                    continue
                now = time.monotonic()
                if ev.get("total") and ev.get("completed") and now - last_sent < 0.5:
                    continue  # no máximo ~2 eventos de progresso por segundo
                last_sent = now
                yield sse_event("progress", ev)
            yield sse_event("done", {"name": body.name})
        except ProviderError as exc:
            yield sse_event("error", {"code": exc.code or "pull_failed", "message": exc.message})

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.delete("/models/{name:path}")
async def delete_model(name: str, request: Request, principal: Principal = Depends(require_admin),
                       db: Database = Depends(get_db)) -> dict:
    try:
        _check_model_name(name)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    mgr, engine = await _manager(request, db)
    if name == engine.default_model and engine.enabled:
        raise ApiError(409, "model_is_default",
                       "Este é o modelo por defeito de um motor ativo. Escolhe outro por defeito antes de o apagar.")
    try:
        await mgr.delete(name)
    except ProviderError as exc:
        if exc.status == 404:
            raise ApiError(404, "model_not_found", f"O modelo '{name}' não está instalado") from exc
        raise _provider_error(exc) from exc
    await _audit(request, db, principal, "admin.local_model_deleted", {"model": name})
    return {"deleted": name}


@router.post("/models/{name:path}/default")
async def set_default_model(name: str, request: Request, principal: Principal = Depends(require_admin),
                            db: Database = Depends(get_db)) -> dict:
    try:
        _check_model_name(name)
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    mgr, _ = await _manager(request, db)
    try:
        installed = {m["name"] for m in await mgr.tags()}
    except ProviderError as exc:
        raise _provider_error(exc) from exc
    if name not in installed and model_catalog.canonical(name) not in installed:
        raise ApiError(404, "model_not_found", f"O modelo '{name}' não está instalado no motor local")
    async with db.service() as conn:
        await conn.execute("update public.local_ai_engine set default_model = $1, updated_by = $2 where id = 1",
                           name, principal.user_id)
    await _audit(request, db, principal, "admin.local_model_default", {"model": name})
    row, engine = await _load(request, db)
    return _engine_out(row, engine)
