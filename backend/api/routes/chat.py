"""Chat com IA para utilizadores autenticados.

Conversas e mensagens são do utilizador: todas as leituras/escritas correm com
``db.as_user`` (RLS). A chamada ao fornecedor de IA acontece fora de qualquer transação;
a mensagem do utilizador e a resposta só são gravadas juntas quando a IA responde
(se a IA falhar, nada fica a meio e o utilizador pode reenviar).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator

from ai_core.ollama import NOT_CONFIGURED_MESSAGE, OllamaManager
from ai_core.providers import PROVIDERS, AIReply, ChatMessage, ProviderError
from ai_core.preferences import allow_paid_providers, load_preference, selection_for
from ai_core.router import AIRouter, AllProvidersFailed, ModelSelection, NoProviderError
from api.deps import client_info, current_principal, enforce_limit, get_db, safe_ip
from api.sse import HEARTBEAT, SSE_HEADERS, SSE_PING, sse_event, with_heartbeat
from api.schemas import _In
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = logging.getLogger("connector_ai.chat")

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
LOCAL_ENGINE_CODES = ("local_engine_unreachable", "local_engine_auth", "local_model_missing")

SYSTEM_PROMPT = (
    "És o assistente do CONNECTOR AI. Responde de forma clara, útil e concisa, "
    "na língua em que o utilizador escrever (por defeito, português). "
    "Nesta fase só conversas: ainda não executas ações em apps, navegador ou contas, "
    "por isso nunca digas que fizeste algo fora desta conversa."
)


class ConversationIn(_In):
    title: str | None = Field(default=None, max_length=200)


class MessageIn(_In):
    content: str = Field(min_length=1, max_length=32000)
    # Opcional: None = automático (motor local primeiro). "ollama" ou um fornecedor na nuvem.
    provider: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str | None) -> str | None:
        if v in (None, "", "auto"):
            return None
        if v != "ollama" and v not in PROVIDERS:
            raise ValueError("fornecedor desconhecido")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _MODEL_RE.match(v):
            raise ValueError("nome de modelo inválido")
        return v

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("mensagem vazia")
        return v


def _conv_out(r) -> dict:
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
        "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
    }


def _msg_out(r) -> dict:
    return {
        "id": str(r["id"]),
        "conversation_id": str(r["conversation_id"]),
        "role": r["role"],
        "content": r["content"],
        "provider": r["provider"],
        "model": r["model"],
        "created_at": r["created_at"].isoformat(),
    }


def _title_from(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else line[:57].rstrip() + "…"


def ai_error(exc: Exception) -> ApiError:
    """Erros da IA em respostas claras (nunca uma resposta inventada)."""
    if isinstance(exc, NoProviderError):
        return ApiError(503, exc.code, exc.message)
    if isinstance(exc, AllProvidersFailed):
        if exc.code in LOCAL_ENGINE_CODES:
            return ApiError(503, exc.code or "local_engine_unreachable", exc.errors[-1].message[:500])
        return ApiError(502, "ai_provider_error", f"O fornecedor de IA falhou: {exc.message}"[:500])
    raise exc  # pragma: no cover


async def _load_conversation(conn, conversation_id: uuid.UUID):
    row = await conn.fetchrow(
        "select id, workspace_id, title, created_at, updated_at, last_message_at from public.conversations where id = $1",
        conversation_id)
    if row is None:  # inexistente ou de outro utilizador (RLS): mesma resposta
        raise ApiError(404, "not_found", "Conversa não encontrada")
    return row


@router.get("/conversations")
async def list_conversations(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    async with db.as_user(principal.user_id) as conn:
        rows = await conn.fetch(
            """select id, title, created_at, updated_at, last_message_at from public.conversations
               order by coalesce(last_message_at, created_at) desc limit 200""")
    return {"items": [_conv_out(r) for r in rows]}


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(request: Request, body: ConversationIn | None = None,
                              principal: Principal = Depends(current_principal),
                              db: Database = Depends(get_db)) -> dict:
    await enforce_limit(request, "chat_create", str(principal.user_id), 120, 3600)
    title = (body.title.strip() if body and body.title else None) or None
    async with db.as_user(principal.user_id) as conn:
        ws = await conn.fetchval("select default_workspace_id from public.user_profiles where user_id = $1",
                                 principal.user_id)
        if ws is None:
            raise ApiError(409, "no_workspace", "A conta não tem workspace por defeito")
        row = await conn.fetchrow(
            """insert into public.conversations (user_id, workspace_id, title) values ($1, $2, $3)
               returning id, title, created_at, updated_at, last_message_at""",
            principal.user_id, ws, title)
    return _conv_out(row)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, request: Request,
                              principal: Principal = Depends(current_principal),
                              db: Database = Depends(get_db)) -> Response:
    async with db.as_user(principal.user_id) as conn:
        deleted = await conn.fetchval("delete from public.conversations where id = $1 returning id", conversation_id)
    if deleted is None:
        raise ApiError(404, "not_found", "Conversa não encontrada")
    client = client_info(request)
    async with db.service() as conn:
        await record_audit(conn, "chat.conversation_deleted", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"conversation_id": str(conversation_id)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: uuid.UUID, principal: Principal = Depends(current_principal),
                        db: Database = Depends(get_db)) -> dict:
    async with db.as_user(principal.user_id) as conn:
        conv = await _load_conversation(conn, conversation_id)
        rows = await conn.fetch(
            """select id, conversation_id, role, content, provider, model, created_at from public.messages
               where conversation_id = $1 order by created_at, id limit 1000""", conversation_id)
    return {"conversation": _conv_out(conv), "items": [_msg_out(r) for r in rows]}


async def _prepare(conversation_id: uuid.UUID, body: MessageIn, request: Request, principal: Principal,
                   db: Database) -> tuple[dict, list[ChatMessage]]:
    settings: Settings = request.app.state.settings
    if len(body.content) > settings.chat_max_message_chars:
        raise ApiError(422, "message_too_long",
                       f"Mensagem demasiado longa (máximo {settings.chat_max_message_chars} caracteres)")
    await enforce_limit(request, "chat_message", str(principal.user_id), settings.chat_rate_limit_per_hour, 3600)
    async with db.as_user(principal.user_id) as conn:
        conv = await _load_conversation(conn, conversation_id)
        history = await conn.fetch(
            """select role, content from (
                 select role, content, created_at, id from public.messages where conversation_id = $1
                 order by created_at desc, id desc limit $2) h
               order by created_at, id""",
            conversation_id, settings.chat_history_messages)
    messages = [ChatMessage(r["role"], r["content"]) for r in history]
    messages.append(ChatMessage("user", body.content))
    return dict(conv), messages


async def _persist(db: Database, principal: Principal, conversation_id: uuid.UUID, conv: dict, content: str,
                   reply: AIReply) -> dict:
    """Grava a pergunta e a resposta juntas (e o uso de tokens)."""
    async with db.as_user(principal.user_id) as conn:
        await _load_conversation(conn, conversation_id)  # pode ter sido apagada entretanto
        user_row = await conn.fetchrow(
            """insert into public.messages (conversation_id, user_id, workspace_id, role, content)
               values ($1, $2, $3, 'user', $4)
               returning id, conversation_id, role, content, provider, model, created_at""",
            conversation_id, principal.user_id, conv["workspace_id"], content)
        bot_row = await conn.fetchrow(
            """insert into public.messages (conversation_id, user_id, workspace_id, role, content, provider, model,
                                            input_tokens, output_tokens, created_at)
               values ($1, $2, $3, 'assistant', $4, $5, $6, $7, $8, clock_timestamp())
               returning id, conversation_id, role, content, provider, model, created_at""",
            conversation_id, principal.user_id, conv["workspace_id"], reply.text, reply.provider, reply.model,
            reply.input_tokens, reply.output_tokens)
        conv_row = await conn.fetchrow(
            """update public.conversations set last_message_at = $2, title = coalesce(title, $3)
               where id = $1 returning id, title, created_at, updated_at, last_message_at""",
            conversation_id, bot_row["created_at"], _title_from(content))

    # uso (tokens) para o Dashboard — escrito pelo serviço
    if reply.input_tokens or reply.output_tokens:
        async with db.service() as conn:
            for metric, qty in (("llm_input_tokens", reply.input_tokens), ("llm_output_tokens", reply.output_tokens)):
                if qty:
                    await conn.execute(
                        """insert into public.usage_metrics (user_id, workspace_id, metric, quantity, unit)
                           values ($1, $2, $3, $4, 'tokens')""",
                        principal.user_id, conv["workspace_id"], metric, qty)

    return {"conversation": _conv_out(conv_row), "user_message": _msg_out(user_row),
            "assistant_message": _msg_out(bot_row)}


async def _selection(body: MessageIn, db: Database, principal: Principal) -> ModelSelection:
    """Escolha explícita do pedido; sem ela, a preferência do utilizador (Definições)."""
    if body.provider:
        return ModelSelection(provider=body.provider, model=body.model)
    async with db.as_user(principal.user_id) as conn:
        pref = await load_preference(conn, principal.user_id)
    allow_paid = False
    if pref.saved:
        async with db.service() as conn:  # ai_policy é tabela de sistema
            allow_paid = await allow_paid_providers(conn)
    return selection_for(pref, allow_paid) or ModelSelection()


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                       principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    conv, messages = await _prepare(conversation_id, body, request, principal, db)
    ai: AIRouter = request.app.state.ai_router
    try:  # IA fora de transações
        reply = await ai.generate(messages, SYSTEM_PROMPT, await _selection(body, db, principal))
    except (NoProviderError, AllProvidersFailed) as exc:
        raise ai_error(exc) from exc
    return await _persist(db, principal, conversation_id, conv, body.content, reply)


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                         principal: Principal = Depends(current_principal),
                         db: Database = Depends(get_db)) -> StreamingResponse:
    """Como ``send_message``, mas a resposta chega em Server-Sent Events.

    Eventos: ``delta`` {text} · ``done`` {conversation, user_message, assistant_message} ·
    ``error`` {code, message}. Se o cliente cortar a ligação (botão Parar), o texto já
    recebido é gravado como resposta (se houver algum).
    """
    conv, messages = await _prepare(conversation_id, body, request, principal, db)
    ai: AIRouter = request.app.state.ai_router
    selection = await _selection(body, db, principal)
    try:  # erros de configuração respondem já em JSON, antes de abrir o stream
        if not await ai.candidates(selection):
            raise NoProviderError()
    except NoProviderError as exc:
        raise ai_error(exc) from exc

    async def events():
        parts: list[str] = []
        reply: AIReply | None = None
        finished = False
        try:
            yield SSE_PING
            async for item in with_heartbeat(ai.stream(messages, SYSTEM_PROMPT, selection)):
                if item is HEARTBEAT:
                    yield SSE_PING
                elif isinstance(item, AIReply):
                    reply = item
                else:
                    parts.append(item)
                    yield sse_event("delta", {"text": item})
            finished = True
            if reply is None:  # pragma: no cover - os clientes terminam sempre com AIReply
                raise AllProvidersFailed([ProviderError("ai", "Resposta vazia")])
            saved = await _persist(db, principal, conversation_id, conv, body.content, reply)
            yield sse_event("done", saved)
        except (NoProviderError, AllProvidersFailed) as exc:
            finished = True
            err = ai_error(exc)
            yield sse_event("error", {"code": err.code, "message": err.message})
        except ApiError as exc:
            finished = True
            yield sse_event("error", {"code": exc.code, "message": exc.message})
        finally:
            text = "".join(parts).strip()
            if not finished and text:
                # o cliente parou a geração: guarda o que já tinha sido mostrado
                partial = AIReply(text=text, provider=reply.provider if reply else (selection.provider or "ollama"),  # type: ignore[arg-type]
                                  model=reply.model if reply else (selection.model or ""))
                task = asyncio.create_task(_persist_quietly(db, principal, conversation_id, conv, body.content, partial))
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


_BACKGROUND: set[asyncio.Task] = set()


async def _persist_quietly(db, principal, conversation_id, conv, content, reply) -> None:
    try:
        await _persist(db, principal, conversation_id, conv, content, reply)
    except Exception:  # noqa: BLE001 - conversa apagada entretanto, etc.
        log.warning("não foi possível gravar a resposta interrompida")


@router.get("/models")
async def chat_models(request: Request, _: Principal = Depends(current_principal)) -> dict:
    """Modelos que o utilizador pode escolher no chat (sem URLs nem chaves)."""
    ai: AIRouter = request.app.state.ai_router
    engine = await ai.local_engine()
    local: dict = {"configured": bool(engine and engine.configured), "enabled": bool(engine and engine.enabled),
                   "reachable": None, "default_model": engine.default_model if engine else None, "models": [],
                   "warning": None}
    if engine is None or not engine.configured or not engine.enabled:
        local["warning"] = (engine.error if engine and engine.error else None) or NOT_CONFIGURED_MESSAGE
    else:
        assert engine.config is not None
        mgr = OllamaManager(engine.config.base_url, engine.config.token, timeout=5, http=ai.http)
        try:
            local["models"] = [m["name"] for m in await mgr.tags() if m.get("name")]
            local["reachable"] = True
        except ProviderError as exc:
            local["reachable"] = False
            local["warning"] = exc.message
    cloud = [{"provider": p.provider, "model": p.model} for p in await ai.active_providers()]
    return {"local": local, "cloud": cloud}
