"""Chat com IA para utilizadores autenticados.

Conversas e mensagens são do utilizador: todas as leituras/escritas correm com
``db.as_user`` (RLS). A chamada ao fornecedor de IA acontece fora de qualquer transação;
a mensagem do utilizador e a resposta só são gravadas juntas quando a IA responde
(se a IA falhar, nada fica a meio e o utilizador pode reenviar).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
import re
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator

from ai_core.agent import AgentEvent
from ai_core.ollama import NOT_CONFIGURED_MESSAGE, OllamaManager
from ai_core.providers import PROVIDERS, AIReply, ChatMessage, ProviderError
from ai_core.preferences import allow_paid_providers, load_preference, selection_for
from ai_core.router import AIRouter, AllProvidersFailed, ModelSelection, NoProviderError
from ai_core.system_prompt import build_system_prompt
from api.deps import client_info, current_principal, enforce_limit, get_db, safe_ip
from api.sse import HEARTBEAT, SSE_HEADERS, SSE_PING, sse_event, with_heartbeat
from api.schemas import _In
from authentication.tokens import Principal
from connector_hub import agent_tools
from connectors.base import ConnectorContext
from core.config import Settings
from core.db import Database
from core.errors import ApiError
from security.audit import record_audit

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = logging.getLogger("connector_ai.chat")

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
LOCAL_ENGINE_CODES = ("local_engine_unreachable", "local_engine_auth", "local_model_missing")

# Prompt de um utilizador sem contas ligadas (o real é montado por pedido: ``build_system_prompt``).
SYSTEM_PROMPT = build_system_prompt({}, [], tools_enabled=True)


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
        "metadata": (r["metadata"] if "metadata" in r.keys() else None) or {},
        "created_at": r["created_at"].isoformat(),
    }


def _approval_ids(metadata: dict | None) -> list[str]:
    return [a["id"] for a in (metadata or {}).get("actions") or [] if a.get("type") == "approval" and a.get("id")]


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
            """select id, conversation_id, role, content, provider, model, metadata, created_at from public.messages
               where conversation_id = $1 order by created_at, id limit 1000""", conversation_id)
        ids = [i for r in rows for i in _approval_ids(r["metadata"])]
        approvals = await agent_tools.load_approvals(conn, ids)
    return {"conversation": _conv_out(conv), "items": [_msg_out(r) for r in rows], "approvals": approvals}


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
                   reply: AIReply, metadata: dict | None = None) -> dict:
    """Grava a pergunta e a resposta juntas (e o uso de tokens)."""
    async with db.as_user(principal.user_id) as conn:
        await _load_conversation(conn, conversation_id)  # pode ter sido apagada entretanto
        user_row = await conn.fetchrow(
            """insert into public.messages (conversation_id, user_id, workspace_id, role, content)
               values ($1, $2, $3, 'user', $4)
               returning id, conversation_id, role, content, provider, model, metadata, created_at""",
            conversation_id, principal.user_id, conv["workspace_id"], content)
        bot_row = await conn.fetchrow(
            """insert into public.messages (conversation_id, user_id, workspace_id, role, content, provider, model,
                                            input_tokens, output_tokens, metadata, created_at)
               values ($1, $2, $3, 'assistant', $4, $5, $6, $7, $8, $9, clock_timestamp())
               returning id, conversation_id, role, content, provider, model, metadata, created_at""",
            conversation_id, principal.user_id, conv["workspace_id"], reply.text, reply.provider, reply.model,
            reply.input_tokens, reply.output_tokens, metadata or {})
        approvals = await agent_tools.load_approvals(conn, _approval_ids(metadata))
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
            "assistant_message": _msg_out(bot_row), "approvals": approvals}


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


class AgentRun:
    """Ferramentas e prompts de um pedido, e o registo do que o agente fez (vai para messages.metadata)."""

    def __init__(self, toolbox: agent_tools.ChatToolbox, system: str, plain_system: str) -> None:
        self.toolbox = toolbox
        self.system = system
        self.plain_system = plain_system
        self.tools: list[dict] = []
        self.actions: list[dict] = []

    def record(self, ev: AgentEvent) -> dict:
        """Guarda o evento e devolve o que vai no SSE."""
        if ev.kind == "tool":
            self.tools.append({"name": ev.data["name"], "label": ev.data.get("label"), "status": ev.data["status"]})
            return ev.data
        if ev.kind == "connect":
            if not any(a.get("type") == "connect" and a.get("connector") == ev.data["connector"] for a in self.actions):
                self.actions.append(ev.data)
            return ev.data
        if ev.kind == "approval":
            self.actions.append({"type": "approval", "id": ev.data["id"]})
            return ev.data
        return ev.data  # pragma: no cover

    def metadata(self) -> dict:
        out: dict = {}
        if self.tools:
            out["tools"] = self.tools
        if self.actions:
            out["actions"] = self.actions
        return out


async def _agent_run(request: Request, principal: Principal, db: Database, conversation_id: uuid.UUID,
                     conv: dict) -> AgentRun:
    """Contas ligadas do utilizador -> ferramentas disponíveis + prompt de sistema honesto."""
    settings: Settings = request.app.state.settings
    ai: AIRouter = request.app.state.ai_router
    connections = await agent_tools.load_connections(db, request.app.state.cipher, principal.user_id)
    toolbox = agent_tools.ChatToolbox(
        db, user_id=principal.user_id, workspace_id=conv["workspace_id"], conversation_id=conversation_id,
        connections=connections, http=getattr(request.app.state, "connector_http", None) or ai.http,
        timeout=settings.connector_http_timeout_seconds)
    connected = {k: c.label for k, c in connections.items() if c.status == "connected"
                 and (k not in agent_tools.TOOL_CONNECTORS or c.usable)}
    expired = [k for k, c in connections.items() if k not in connected]
    tool_connectors = toolbox.tool_connectors()
    system = build_system_prompt(connected, tool_connectors, tools_enabled=True, expired=expired)
    plain = build_system_prompt(connected, tool_connectors, tools_enabled=False, expired=expired)
    return AgentRun(toolbox, system, plain)


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                       principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    conv, messages = await _prepare(conversation_id, body, request, principal, db)
    ai: AIRouter = request.app.state.ai_router
    run = await _agent_run(request, principal, db, conversation_id, conv)
    reply: AIReply | None = None
    try:  # IA fora de transações
        async for item in ai.agent_stream(messages, run.system, run.plain_system,
                                          await _selection(body, db, principal), run.toolbox, stream=False):
            if isinstance(item, AIReply):
                reply = item
            elif isinstance(item, AgentEvent):
                run.record(item)
    except (NoProviderError, AllProvidersFailed) as exc:
        raise ai_error(exc) from exc
    if reply is None:  # pragma: no cover
        raise ai_error(AllProvidersFailed([ProviderError("ai", "Resposta vazia")]))
    return await _persist(db, principal, conversation_id, conv, body.content, reply, run.metadata())


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(conversation_id: uuid.UUID, body: MessageIn, request: Request,
                         principal: Principal = Depends(current_principal),
                         db: Database = Depends(get_db)) -> StreamingResponse:
    """Como ``send_message``, mas a resposta chega em Server-Sent Events.

    Eventos: ``delta`` {text} · ``tool`` {name, label, status} (o agente usou uma ferramenta) ·
    ``connect`` {connector, name, method, tools_available, reason} (mostrar botão "Ligar …") ·
    ``approval`` {id, summary, tool, arguments, status, …} (ação à espera de Confirmar/Recusar) ·
    ``done`` {conversation, user_message, assistant_message, approvals} · ``error`` {code, message}.
    Se o cliente cortar a ligação (botão Parar), o texto já recebido é gravado como resposta.
    """
    conv, messages = await _prepare(conversation_id, body, request, principal, db)
    ai: AIRouter = request.app.state.ai_router
    selection = await _selection(body, db, principal)
    try:  # erros de configuração respondem já em JSON, antes de abrir o stream
        if not await ai.candidates(selection):
            raise NoProviderError()
    except NoProviderError as exc:
        raise ai_error(exc) from exc
    run = await _agent_run(request, principal, db, conversation_id, conv)

    async def events():
        parts: list[str] = []
        reply: AIReply | None = None
        finished = False
        try:
            yield SSE_PING
            async for item in with_heartbeat(ai.agent_stream(messages, run.system, run.plain_system, selection,
                                                             run.toolbox)):
                if item is HEARTBEAT:
                    yield SSE_PING
                elif isinstance(item, AIReply):
                    reply = item
                elif isinstance(item, AgentEvent):
                    yield sse_event(item.kind, run.record(item))
                else:
                    parts.append(item)
                    yield sse_event("delta", {"text": item})
            finished = True
            if reply is None:  # pragma: no cover - os clientes terminam sempre com AIReply
                raise AllProvidersFailed([ProviderError("ai", "Resposta vazia")])
            saved = await _persist(db, principal, conversation_id, conv, body.content, reply, run.metadata())
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
                task = asyncio.create_task(_persist_quietly(db, principal, conversation_id, conv, body.content,
                                                            partial, run.metadata()))
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


_BACKGROUND: set[asyncio.Task] = set()


async def _persist_quietly(db, principal, conversation_id, conv, content, reply, metadata=None) -> None:
    try:
        await _persist(db, principal, conversation_id, conv, content, reply, metadata)
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


# --- ações à espera de confirmação (ex.: criar issue no GitHub) ---------------------------------------
class DecisionIn(_In):
    decision: str = Field(pattern="^(approve|reject)$")


def _result_message(summary: str, ok: bool | None, data: dict | None, error: str | None) -> str:
    if ok is None:
        return f"Ação cancelada: {summary}. Nada foi alterado."
    if not ok:
        return f"Não foi possível concluir: {summary}. {error or ''}".strip()
    url = (data or {}).get("url")
    number = (data or {}).get("number")
    done = f"Feito: {summary}"
    if number:
        done += f" (#{number})"
    return done + (f" — {url}" if url else ".")


@router.post("/approvals/{approval_id}")
async def decide_approval(approval_id: uuid.UUID, body: DecisionIn, request: Request,
                          principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    """O utilizador confirma ou recusa uma ação proposta pelo agente. Só aqui uma escrita é executada."""
    await enforce_limit(request, "chat_approval", str(principal.user_id), 60, 3600)
    async with db.as_user(principal.user_id) as conn:  # RLS: só as aprovações do próprio
        row = await agent_tools.get_approval_row(conn, approval_id)
        if row is None:
            raise ApiError(404, "not_found", "Ação não encontrada")
        if row["status"] != "pending":
            raise ApiError(409, "approval_decided", "Esta ação já foi decidida")
        if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
            expired = True
        else:
            expired = False
            new_status = "approved" if body.decision == "approve" else "rejected"
            updated = await conn.fetchval(
                """update public.approvals set status = $2, decided_at = now()
                   where id = $1 and status = 'pending' returning id""", approval_id, new_status)
            if updated is None:
                raise ApiError(409, "approval_decided", "Esta ação já foi decidida")
    if expired:
        async with db.service() as conn:
            await conn.execute("update public.approvals set status = 'expired' where id = $1 and status = 'pending'",
                               approval_id)
            await agent_tools.finish_approval(conn, row, user_id=principal.user_id, ok=False, output=None,
                                              error="expirou sem confirmação")
        raise ApiError(409, "approval_expired", "Esta ação expirou. Pede de novo ao DÁRIO.")

    preview = row["payload_preview"] or {}
    tool, args = preview.get("tool") or "", preview.get("arguments") or {}
    ok: bool | None = None
    data: dict | None = None
    error: str | None = None
    if body.decision == "approve":
        key = tool.split(".", 1)[0]
        connections = await agent_tools.load_connections(db, request.app.state.cipher, principal.user_id)
        conn_info = connections.get(key)
        if conn_info is None or not conn_info.usable:
            ok, error = False, "A conta já não está ligada: liga-a em /conectores e pede de novo."
        else:
            assert conn_info.payload is not None
            settings: Settings = request.app.state.settings
            ai: AIRouter = request.app.state.ai_router
            connector = agent_tools.build_connector(
                key, conn_info.payload, getattr(request.app.state, "connector_http", None) or ai.http,
                settings.connector_http_timeout_seconds)
            ctx = ConnectorContext(user_id=principal.user_id, workspace_id=conn_info.workspace_id,
                                   connected_app_id=conn_info.app_id,
                                   granted_permissions=agent_tools.connector_permissions(key),
                                   task_id=row["task_id"], approval_id=approval_id)
            result = await connector.execute(tool, ctx, args)
            ok, data = result.ok, result.data
            if not result.ok:
                error = result.error_message
    async with db.service() as conn:
        await agent_tools.finish_approval(conn, row, user_id=principal.user_id, ok=ok, output=data, error=error)

    message = None
    text = _result_message(row["action_summary"], ok, data, error)
    conv_id = preview.get("conversation_id")
    async with db.as_user(principal.user_id) as conn:
        if conv_id:
            exists = await conn.fetchval("select workspace_id from public.conversations where id = $1",
                                         uuid.UUID(conv_id))
            if exists is not None:
                msg_row = await conn.fetchrow(
                    """insert into public.messages (conversation_id, user_id, workspace_id, role, content, metadata)
                       values ($1, $2, $3, 'assistant', $4, $5)
                       returning id, conversation_id, role, content, provider, model, metadata, created_at""",
                    uuid.UUID(conv_id), principal.user_id, exists, text,
                    {"approval_result": str(approval_id), "ok": ok})
                await conn.execute("update public.conversations set last_message_at = $2 where id = $1",
                                   uuid.UUID(conv_id), msg_row["created_at"])
                message = _msg_out(msg_row)
        approval = (await agent_tools.load_approvals(conn, [str(approval_id)])).get(str(approval_id))
    client = client_info(request)
    async with db.service() as conn:
        await record_audit(conn, "chat.approval_decided", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent,
                           details={"approval_id": str(approval_id), "decision": body.decision, "tool": tool,
                                    "ok": ok})
    return {"approval": approval, "message": message, "ok": ok}
