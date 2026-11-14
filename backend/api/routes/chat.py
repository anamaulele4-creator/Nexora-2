"""Chat com IA para utilizadores autenticados.

Conversas e mensagens são do utilizador: todas as leituras/escritas correm com
``db.as_user`` (RLS). A chamada ao fornecedor de IA acontece fora de qualquer transação;
a mensagem do utilizador e a resposta só são gravadas juntas quando a IA responde
(se a IA falhar, nada fica a meio e o utilizador pode reenviar).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import Field, field_validator

from ai_core.providers import ChatMessage
from ai_core.router import AIRouter, AllProvidersFailed, NoProviderError
from api.deps import client_info, current_principal, enforce_limit, get_db, safe_ip
from api.schemas import _In
from authentication.tokens import Principal
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

router = APIRouter(prefix="/api/chat", tags=["chat"])

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


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                       principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    settings: Settings = request.app.state.settings
    if len(body.content) > settings.chat_max_message_chars:
        raise ApiError(422, "message_too_long",
                       f"Mensagem demasiado longa (máximo {settings.chat_max_message_chars} caracteres)")
    await enforce_limit(request, "chat_message", str(principal.user_id), settings.chat_rate_limit_per_hour, 3600)

    # 1. histórico (sob RLS)
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

    # 2. IA (fora de transações)
    ai: AIRouter = request.app.state.ai_router
    try:
        reply = await ai.generate(messages, SYSTEM_PROMPT)
    except NoProviderError as exc:
        raise ApiError(503, "ai_unavailable", exc.message) from exc
    except AllProvidersFailed as exc:
        raise ApiError(502, "ai_provider_error", f"O fornecedor de IA falhou: {exc.message}"[:500]) from exc

    # 3. grava as duas mensagens juntas
    async with db.as_user(principal.user_id) as conn:
        await _load_conversation(conn, conversation_id)  # pode ter sido apagada entretanto
        user_row = await conn.fetchrow(
            """insert into public.messages (conversation_id, user_id, workspace_id, role, content)
               values ($1, $2, $3, 'user', $4)
               returning id, conversation_id, role, content, provider, model, created_at""",
            conversation_id, principal.user_id, conv["workspace_id"], body.content)
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
            conversation_id, bot_row["created_at"], _title_from(body.content))

    # 4. uso (tokens) para o Dashboard — escrito pelo serviço
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
