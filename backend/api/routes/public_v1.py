"""API pública v1 (compatível com OpenAI) para programas externos.

Autenticação: ``Authorization: Bearer cai_…`` — tokens emitidos em /admin (só o hash
fica na BD). Rotas:

* ``GET  /api/v1/health``            — sem token; estado mínimo do serviço.
* ``GET  /api/v1/models``            — âmbito ``models``; formato OpenAI (``auto`` + modelos locais).
* ``GET  /api/v1/models/status``     — âmbito ``models``; estado do motor local.
* ``POST /api/v1/chat/completions``  — âmbito ``chat``; formato OpenAI, ``stream`` opcional (SSE).

``model``: ``auto`` (encaminhamento normal: motor local primeiro, com as regras de recurso
do admin), ``local``/``default`` (modelo por defeito do motor local) ou o nome de um modelo
instalado no Ollama (ex.: ``qwen3:1.7b``). Cada pedido fica em ``api_usage_logs``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_core.ollama import NOT_CONFIGURED_MESSAGE, OllamaManager
from ai_core.providers import AIReply, ChatMessage, ProviderError
from ai_core.router import AIRouter, AllProvidersFailed, ModelSelection, NoProviderError
from api.deps import enforce_limit, get_db
from api.routes.chat import ai_error
from api.routes.health import VERSION
from api.sse import HEARTBEAT, SSE_HEADERS, SSE_PING, sse_event, with_heartbeat
from core.db import Database
from core.errors import ApiError
from security.api_keys import hash_token, looks_like_token

router = APIRouter(prefix="/api/v1", tags=["public-v1"])

AUTO_MODELS = ("auto",)
LOCAL_DEFAULT_ALIASES = ("local", "default")


@dataclass(frozen=True)
class ApiCaller:
    key_id: uuid.UUID
    user_id: uuid.UUID | None
    scopes: tuple[str, ...]


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "invalid_api_key", message, headers={"WWW-Authenticate": "Bearer"})


async def api_caller(request: Request, db: Database = Depends(get_db)) -> ApiCaller:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise _unauthorized("Falta o token da API (Authorization: Bearer cai_…)")
    if not looks_like_token(token):
        raise _unauthorized("Token da API inválido")
    async with db.service() as conn:
        row = await conn.fetchrow(
            """select id, user_id, scopes, revoked_at, expires_at, last_used_at from public.api_keys
               where key_hash = $1""", hash_token(token))
        if row is None or row["revoked_at"] is not None:
            raise _unauthorized("Token da API inválido ou revogado")
        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
            raise _unauthorized("Token da API expirado")
        if row["last_used_at"] is None or (datetime.now(UTC) - row["last_used_at"]).total_seconds() > 60:
            await conn.execute("update public.api_keys set last_used_at = now() where id = $1", row["id"])
    caller = ApiCaller(row["id"], row["user_id"], tuple(row["scopes"] or ()))
    await enforce_limit(request, "api_v1", str(caller.key_id),
                        request.app.state.settings.api_v1_rate_limit_per_minute, 60)
    return caller


def _require(caller: ApiCaller, scope: str) -> None:
    if scope not in caller.scopes:
        raise ApiError(403, "insufficient_scope", f"Este token não tem o âmbito '{scope}'")


async def _log(db: Database, caller: ApiCaller, endpoint: str, status_code: int, started: float, *,
               reply: AIReply | None = None, model: str | None = None, streamed: bool = False) -> None:
    async with db.service() as conn:
        await conn.execute(
            """insert into public.api_usage_logs (api_key_id, user_id, endpoint, provider, model, status_code, streamed,
                                                  input_tokens, output_tokens, latency_ms)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            caller.key_id, caller.user_id, endpoint, reply.provider if reply else None,
            (reply.model if reply else model), status_code, streamed,
            reply.input_tokens if reply else None, reply.output_tokens if reply else None,
            int((time.perf_counter() - started) * 1000))


_BACKGROUND: set[asyncio.Task] = set()


async def _log_quietly(*args, **kwargs) -> None:
    try:
        await _log(*args, **kwargs)
    except Exception:  # noqa: BLE001 - o registo de uso nunca parte a resposta
        logging.getLogger("connector_ai.api_v1").warning("falha ao registar uso da API v1")


# --- saúde e modelos ----------------------------------------------------------------------------
@router.get("/health")
async def health(request: Request) -> dict:
    ai: AIRouter = request.app.state.ai_router
    engine = await ai.local_engine()
    if engine is None or not engine.usable or engine.config is None:
        local = "not_configured"
    else:
        try:
            await OllamaManager(engine.config.base_url, engine.config.token, timeout=3, http=ai.http).version()
            local = "ok"
        except ProviderError:
            local = "unreachable"
    return {"status": "ok", "version": VERSION, "local_engine": local}


@router.get("/models")
async def list_models(request: Request, caller: ApiCaller = Depends(api_caller), db: Database = Depends(get_db)) -> dict:
    _require(caller, "models")
    started = time.perf_counter()
    ai: AIRouter = request.app.state.ai_router
    engine = await ai.local_engine()
    data = [{"id": "auto", "object": "model", "created": 0, "owned_by": "connector-ai"}]
    if engine and engine.usable and engine.config:
        try:
            for m in await OllamaManager(engine.config.base_url, engine.config.token, timeout=5, http=ai.http).tags():
                data.append({"id": m["name"], "object": "model", "created": 0, "owned_by": "ollama",
                             "default": m["name"] == engine.default_model})
        except ProviderError:
            pass  # o estado detalhado está em /api/v1/models/status
    await _log(db, caller, "models", 200, started)
    return {"object": "list", "data": data}


@router.get("/models/status")
async def models_status(request: Request, caller: ApiCaller = Depends(api_caller),
                        db: Database = Depends(get_db)) -> dict:
    _require(caller, "models")
    started = time.perf_counter()
    ai: AIRouter = request.app.state.ai_router
    engine = await ai.local_engine()
    out: dict = {"local_engine": {"configured": bool(engine and engine.configured),
                                  "enabled": bool(engine and engine.enabled),
                                  "reachable": False, "version": None,
                                  "default_model": engine.default_model if engine else None,
                                  "installed": [], "running": [], "warning": None}}
    le = out["local_engine"]
    if engine is None or not engine.usable or engine.config is None:
        le["warning"] = (engine.error if engine and engine.error else None) or NOT_CONFIGURED_MESSAGE
    else:
        mgr = OllamaManager(engine.config.base_url, engine.config.token, timeout=5, http=ai.http)
        try:
            le["version"] = await mgr.version()
            le["installed"] = [m["name"] for m in await mgr.tags()]
            le["running"] = [m["name"] for m in await mgr.ps()]
            le["reachable"] = True
        except ProviderError as exc:
            le["warning"] = exc.message
    await _log(db, caller, "models/status", 200, started)
    return out


# --- chat completions ------------------------------------------------------------------------------
class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "user", "assistant", "developer"]
    content: str | list[dict]  # lista = partes (só o texto é usado)

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(str(p.get("text") or "") for p in self.content if isinstance(p, dict) and p.get("type") == "text")


class ChatCompletionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # campos OpenAI não suportados são ignorados
    model: str = Field(default="auto", min_length=1, max_length=200)
    messages: list[_Msg] = Field(min_length=1, max_length=200)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)


def _selection(body: ChatCompletionIn) -> ModelSelection:
    m = body.model.strip()
    overrides = {"temperature": body.temperature, "max_tokens": body.max_completion_tokens or body.max_tokens}
    if m in AUTO_MODELS:
        return ModelSelection(**overrides)
    if m in LOCAL_DEFAULT_ALIASES:
        return ModelSelection(provider="ollama", **overrides)
    return ModelSelection(provider="ollama", model=m, **overrides)


def _split(body: ChatCompletionIn, max_chars: int) -> tuple[str | None, list[ChatMessage]]:
    system_parts: list[str] = []
    msgs: list[ChatMessage] = []
    total = 0
    for m in body.messages:
        text = m.text()
        total += len(text)
        if m.role in ("system", "developer"):
            if text.strip():
                system_parts.append(text)
        elif text.strip():
            msgs.append(ChatMessage("assistant" if m.role == "assistant" else "user", text))
    if total > max_chars:
        raise ApiError(413, "context_too_long", f"Mensagens demasiado longas (máximo {max_chars} caracteres no total)")
    if not msgs or msgs[-1].role != "user":
        raise ApiError(422, "invalid_messages", "A última mensagem tem de ser do utilizador (role=user)")
    return ("\n\n".join(system_parts) or None), msgs


def _error_for(exc: Exception) -> ApiError:
    err = ai_error(exc)
    if err.code == "local_model_missing":
        return ApiError(404, "model_not_found", err.message)
    return err


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionIn, request: Request, caller: ApiCaller = Depends(api_caller),
                           db: Database = Depends(get_db)):
    _require(caller, "chat")
    started = time.perf_counter()
    settings = request.app.state.settings
    system, messages = _split(body, max(settings.chat_max_message_chars * 8, 32000))
    selection = _selection(body)
    ai: AIRouter = request.app.state.ai_router
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if not body.stream:
        try:
            reply = await ai.generate(messages, system, selection)
        except (NoProviderError, AllProvidersFailed) as exc:
            err = _error_for(exc)
            await _log(db, caller, "chat/completions", err.status_code, started, model=body.model)
            raise err from exc
        await _log(db, caller, "chat/completions", 200, started, reply=reply)
        usage = {"prompt_tokens": reply.input_tokens or 0, "completion_tokens": reply.output_tokens or 0}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return {"id": completion_id, "object": "chat.completion", "created": created, "model": reply.model,
                "provider": reply.provider,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply.text},
                             "finish_reason": "stop"}],
                "usage": usage}

    try:  # erros de configuração em JSON, antes de abrir o stream
        if not await ai.candidates(selection):
            raise NoProviderError()
    except NoProviderError as exc:
        err = _error_for(exc)
        await _log(db, caller, "chat/completions", err.status_code, started, model=body.model, streamed=True)
        raise err from exc

    def chunk(model: str, delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
        data: dict = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            data["usage"] = usage
        return sse_event(None, data)

    async def events():
        model = body.model
        first = True
        status_code = 200
        reply: AIReply | None = None
        try:
            yield SSE_PING
            async for item in with_heartbeat(ai.stream(messages, system, selection)):
                if item is HEARTBEAT:
                    yield SSE_PING
                elif isinstance(item, AIReply):
                    reply = item
                    usage = {"prompt_tokens": item.input_tokens or 0, "completion_tokens": item.output_tokens or 0}
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    yield chunk(item.model, {}, "stop", usage)
                else:
                    delta: dict = {"content": item}
                    if first:
                        delta["role"] = "assistant"
                        first = False
                    yield chunk(model, delta)
            yield "data: [DONE]\n\n"
        except (NoProviderError, AllProvidersFailed) as exc:
            err = _error_for(exc)
            status_code = err.status_code
            yield sse_event(None, {"error": {"code": err.code, "message": err.message, "type": "server_error"}})
            yield "data: [DONE]\n\n"
        finally:
            # numa tarefa à parte: se o cliente cortou a ligação, este gerador está a ser cancelado
            final_status = status_code if (reply or status_code != 200) else 499
            task = asyncio.create_task(_log_quietly(db, caller, "chat/completions", final_status, started,
                                                    reply=reply, model=body.model, streamed=True))
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)
