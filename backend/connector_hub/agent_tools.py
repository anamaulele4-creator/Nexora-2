"""Ferramentas do agente no chat: o que cada utilizador pode usar e como cada chamada corre.

* Só são expostas ferramentas de conectores que ESTE utilizador tem ligados (estado ``connected``
  e token decifrável). A lista vem do ``ToolRegistry`` — um nome fora dele nunca corre.
* ``request_connection`` está sempre disponível: quando um pedido precisa de uma conta que não está
  ligada, o modelo chama-a e o chat mostra o botão "Ligar <app>" (nunca recusa sem oferecer isto).
* Leitura corre logo. Escrita (``requires_approval``) não corre: cria uma aprovação pendente
  (``tasks`` + ``task_steps`` + ``approvals``) e o utilizador confirma ou recusa com um botão
  (``POST /api/chat/approvals/{id}``). Só aí a ferramenta é executada, com ``approval_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from ai_core.agent import AgentEvent, ToolOutcome
from connector_hub import vault
from connector_hub.registry import CONNECTION_SPECS, ToolRegistry
from connectors.base import ConnectorContext, ToolResult
from connectors.github import GitHubConnector
from connectors.github.connector import PERMISSIONS as GITHUB_PERMISSIONS
from connectors.github.connector import describe_action
from core.db import Database
from security.crypto import CredentialCipher

log = logging.getLogger("connector_ai.agent_tools")

APPROVAL_TTL = timedelta(hours=1)

# Conectores que o utilizador pode ligar em /conectores (nome mostrado).
CONNECTOR_NAMES: dict[str, str] = {"github": "GitHub", "gmail": "Gmail", "google_drive": "Google Drive",
                                   "shopify": "Shopify"}
# Conectores que já têm ferramentas do agente.
TOOL_CONNECTORS: frozenset[str] = frozenset({"github"})

REQUEST_CONNECTION = "request_connection"

TOOL_LABELS: dict[str, str] = {
    "github__get_me": "GitHub · perfil", "github__list_repos": "GitHub · repositórios",
    "github__get_repo": "GitHub · repositório", "github__list_issues": "GitHub · issues",
    "github__get_issue": "GitHub · issue", "github__list_pull_requests": "GitHub · pull requests",
    "github__read_file": "GitHub · ficheiro", "github__search_code": "GitHub · pesquisa de código",
    "github__create_issue": "GitHub · criar issue", "github__comment_issue": "GitHub · comentar",
    REQUEST_CONNECTION: "Ligar conta",
}


@dataclass(frozen=True)
class Connection:
    key: str
    app_id: uuid.UUID
    workspace_id: uuid.UUID
    status: str
    label: str
    payload: dict | None = None  # credencial decifrada (só conectores com ferramentas)

    @property
    def usable(self) -> bool:
        return self.status == "connected" and self.payload is not None


async def load_connections(db: Database, cipher: CredentialCipher | None, user_id: uuid.UUID) -> dict[str, Connection]:
    """Contas ligadas pelo utilizador (sem revogadas). Decifra só o necessário para as ferramentas."""
    async with db.service() as conn:
        rows = await conn.fetch(
            """select a.id, a.workspace_id, a.status, a.external_account_label, d.key
               from public.connected_apps a join public.connector_definitions d on d.id = a.connector_definition_id
               where a.user_id = $1 and a.status <> 'revoked' and a.revoked_at is null""", user_id)
        out: dict[str, Connection] = {}
        for r in rows:
            payload = None
            if r["key"] in TOOL_CONNECTORS and r["status"] == "connected":
                stored = await vault.load_connection(conn, cipher, user_id=user_id, connector_key=r["key"])
                payload = stored.payload if stored else None
            out[r["key"]] = Connection(r["key"], r["id"], r["workspace_id"], r["status"],
                                       r["external_account_label"] or "", payload)
    return out


def build_connector(key: str, payload: dict, http: httpx.AsyncClient | None, timeout: float):
    if key == "github":
        return GitHubConnector(payload["access_token"], http=http, timeout=timeout)
    raise KeyError(key)  # pragma: no cover


def connector_permissions(key: str) -> frozenset[str]:
    return GITHUB_PERMISSIONS if key == "github" else frozenset()


def request_connection_tool() -> dict:
    return {"type": "function", "function": {
        "name": REQUEST_CONNECTION,
        "description": ("Mostra ao utilizador um botão para ligar uma conta (GitHub, Gmail, Google Drive ou Shopify). "
                        "Usa SEMPRE que o pedido precisar de uma conta que não está ligada, em vez de recusar."),
        "parameters": {"type": "object", "additionalProperties": False, "required": ["connector"], "properties": {
            "connector": {"type": "string", "enum": list(CONNECTOR_NAMES),
                          "description": "Conta a ligar"},
            "reason": {"type": "string", "maxLength": 300,
                       "description": "Porque é precisa (curto, na língua do utilizador)"},
        }},
        "strict": False,
    }}


def connect_action(key: str, reason: str = "") -> dict:
    spec = CONNECTION_SPECS.get(key)
    return {"type": "connect", "connector": key, "name": CONNECTOR_NAMES[key],
            "method": spec.method if spec else "oauth", "tools_available": key in TOOL_CONNECTORS,
            "reason": reason[:300]}


class ChatToolbox:
    """Ferramentas de um utilizador numa conversa."""

    def __init__(self, db: Database, *, user_id: uuid.UUID, workspace_id: uuid.UUID, conversation_id: uuid.UUID,
                 connections: dict[str, Connection], http: httpx.AsyncClient | None = None,
                 timeout: float = 20) -> None:
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.conversation_id = conversation_id
        self.connections = connections
        self.registry = ToolRegistry()
        self._ctx: dict[str, ConnectorContext] = {}
        for key, c in connections.items():
            if key in TOOL_CONNECTORS and c.usable:
                assert c.payload is not None
                self.registry.register(build_connector(key, c.payload, http, timeout))
                self._ctx[key] = ConnectorContext(user_id=user_id, workspace_id=c.workspace_id,
                                                  connected_app_id=c.app_id,
                                                  granted_permissions=connector_permissions(key))

    # --- o que o modelo vê ------------------------------------------------------------------------
    def definitions(self) -> list[dict]:
        return [request_connection_tool(), *self.registry.openai_tools()]

    def label(self, name: str) -> str:
        return TOOL_LABELS.get(name, name)

    def tool_connectors(self) -> list[str]:
        return [k for k in self._ctx]

    # --- execução ---------------------------------------------------------------------------------
    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        if name == REQUEST_CONNECTION:
            return self._request_connection(arguments)
        try:
            connector, spec = self.registry.resolve(name)
        except Exception:  # noqa: BLE001 - ConnectorError unknown_tool
            known = ", ".join(self.registry.names()) or "nenhuma (só request_connection)"
            return ToolOutcome({"ok": False, "error": "unknown_tool",
                                "message": f"Ferramenta indisponível: {name}. Disponíveis: {known}"}, ok=False)
        if spec.requires_approval:
            return await self._propose(spec, arguments)
        ctx = self._ctx[spec.connector_key]
        try:
            result = await asyncio.wait_for(connector.execute(spec.name, ctx, arguments), spec.timeout_seconds)
        except TimeoutError:
            result = ToolResult(False, error_code="timeout", error_message="A ferramenta demorou demasiado")
        if not result.ok and result.error_code == "token_invalid":
            await self._mark_expired(spec.connector_key)
            return ToolOutcome({"ok": False, "error": "token_invalid", "message": result.error_message},
                               events=[AgentEvent("connect", connect_action(spec.connector_key,
                                                                            "O acesso expirou: volta a ligar"))],
                               ok=False)
        if result.ok:
            return ToolOutcome({"ok": True, "data": result.data})
        return ToolOutcome({"ok": False, "error": result.error_code, "message": result.error_message}, ok=False)

    def _request_connection(self, arguments: dict) -> ToolOutcome:
        key = str(arguments.get("connector") or "")
        if key not in CONNECTOR_NAMES:
            return ToolOutcome({"ok": False, "error": "invalid_arguments",
                                "message": f"connector tem de ser um de: {', '.join(CONNECTOR_NAMES)}"}, ok=False)
        name = CONNECTOR_NAMES[key]
        current = self.connections.get(key)
        if current is not None and current.status == "connected" and (key not in TOOL_CONNECTORS or current.usable):
            note = "" if key in TOOL_CONNECTORS else " As ferramentas do agente para esta app ainda não existem."
            return ToolOutcome({"ok": True, "status": "already_connected", "account": current.label,
                                "message": f"{name} já está ligado ({current.label}).{note}"})
        action = connect_action(key, str(arguments.get("reason") or ""))
        msg = (f"A conta {name} AINDA NÃO está ligada. Foi mostrado ao utilizador um botão 'Ligar {name}'. "
               "Diz-lhe que precisas dessa conta, para carregar no botão e voltar a pedir depois. "
               "Não digas que a conta foi ligada nem que mostraste dados dela.")
        if key not in TOOL_CONNECTORS:
            msg += (f" Sê honesto: depois de ligar, as ações com {name} ainda não estão disponíveis no chat "
                    "(chegam numa próxima versão).")
        return ToolOutcome({"ok": True, "status": "connect_button_shown", "message": msg},
                           events=[AgentEvent("connect", action)])

    async def _propose(self, spec, arguments: dict) -> ToolOutcome:
        try:
            params = spec.parameters.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            return ToolOutcome({"ok": False, "error": "invalid_arguments", "message": str(exc)[:800]}, ok=False)
        args = params.model_dump(mode="json")
        summary = describe_action(spec.name, args)
        async with self.db.service() as conn:
            approval = await create_pending_approval(
                conn, user_id=self.user_id, workspace_id=self.workspace_id, conversation_id=self.conversation_id,
                tool=spec.name, arguments=args, summary=summary, risk=spec.risk.value,
                category=spec.approval_category.value if spec.approval_category else "other")
        return ToolOutcome(
            {"ok": True, "status": "pending_user_confirmation", "approval_id": approval["id"],
             "message": ("A ação NÃO foi executada. O utilizador vê um cartão com Confirmar/Recusar. "
                         "Explica o que vai acontecer e diz que fica à espera da confirmação.")},
            events=[AgentEvent("approval", approval)])

    async def _mark_expired(self, key: str) -> None:
        c = self.connections.get(key)
        if c is None:
            return
        async with self.db.service() as conn:
            await vault.record_health(conn, user_id=self.user_id, app_id=c.app_id, ok=False, status="expired")


# --- aprovações (tasks + task_steps + approvals) ---------------------------------------------------------
def approval_out(row) -> dict:
    preview = row["payload_preview"]
    if isinstance(preview, str):
        preview = json.loads(preview)
    output = row["output"] if "output" in row.keys() else None
    if isinstance(output, str):
        output = json.loads(output)
    return {
        "id": str(row["id"]), "status": row["status"], "summary": row["action_summary"],
        "tool": preview.get("tool"), "arguments": preview.get("arguments") or {},
        "risk": row["risk_level"], "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "result": output,
        "task_status": row["task_status"] if "task_status" in row.keys() else None,
    }


_APPROVAL_SQL = """select a.id, a.status, a.action_summary, a.payload_preview, a.risk_level, a.expires_at,
                          a.decided_at, a.task_id, a.task_step_id, a.workspace_id, s.output, t.status as task_status
                   from public.approvals a left join public.task_steps s on s.id = a.task_step_id
                   left join public.tasks t on t.id = a.task_id"""


async def create_pending_approval(conn, *, user_id: uuid.UUID, workspace_id: uuid.UUID, conversation_id: uuid.UUID,
                                  tool: str, arguments: dict, summary: str, risk: str, category: str) -> dict:
    expires = datetime.now(UTC) + APPROVAL_TTL
    instruction = json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False)[:20000]
    task_id = await conn.fetchval(
        """insert into public.tasks (user_id, workspace_id, title, instruction, mode, status, max_attempts)
           values ($1, $2, $3, $4, 'manual', 'waiting_approval', 1) returning id""",
        user_id, workspace_id, summary[:300], instruction)
    step_id = await conn.fetchval(
        """insert into public.task_steps (task_id, user_id, workspace_id, step_index, kind, tool_name, status,
                                          risk_level, input)
           values ($1, $2, $3, 0, 'tool_call', $4, 'waiting_approval', $5, $6) returning id""",
        task_id, user_id, workspace_id, tool, risk,
        {"arguments": arguments, "conversation_id": str(conversation_id)})
    preview = {"conversation_id": str(conversation_id), "tool": tool, "arguments": arguments}
    approval_id = await conn.fetchval(
        """insert into public.approvals (user_id, workspace_id, task_id, task_step_id, category, action_summary,
                                         risk_level, payload_preview, expires_at)
           values ($1, $2, $3, $4, $5, $6, $7, $8, $9) returning id""",
        user_id, workspace_id, task_id, step_id, category, summary[:1000], risk if risk != "low" else "medium",
        preview, expires)
    await conn.execute(
        """insert into public.action_logs (user_id, workspace_id, task_id, task_step_id, actor, action, target,
                                           risk_level, outcome, details)
           values ($1, $2, $3, $4, 'agent', $5, $6, $7, 'pending', $8)""",
        user_id, workspace_id, task_id, step_id, f"propose:{tool}", summary[:300], risk,
        {"approval_id": str(approval_id)})
    row = await conn.fetchrow(_APPROVAL_SQL + " where a.id = $1", approval_id)
    return approval_out(row)


async def load_approvals(conn, ids: list[str]) -> dict[str, dict]:
    valid = []
    for i in ids:
        try:
            valid.append(uuid.UUID(str(i)))
        except ValueError:
            continue
    if not valid:
        return {}
    rows = await conn.fetch(_APPROVAL_SQL + " where a.id = any($1::uuid[])", valid)
    return {str(r["id"]): approval_out(r) for r in rows}


async def get_approval_row(conn, approval_id: uuid.UUID):
    return await conn.fetchrow(_APPROVAL_SQL + " where a.id = $1", approval_id)


async def finish_approval(conn, row, *, user_id: uuid.UUID, ok: bool | None, output: dict | None,
                          error: str | None) -> None:
    """Fecha a tarefa: ok=True executada, ok=False falhou, ok=None recusada pelo utilizador."""
    if ok is None:
        task_status, step_status, outcome = "cancelled", "cancelled", "blocked"
    elif ok:
        task_status, step_status, outcome = "completed", "succeeded", "success"
    else:
        task_status, step_status, outcome = "failed", "failed", "failure"
    preview = row["payload_preview"]
    if isinstance(preview, str):
        preview = json.loads(preview)
    await conn.execute(
        """update public.task_steps set status = $3, output = $4, error_message = $5, started_at = coalesce(started_at, now()),
                  finished_at = now() where id = $1 and user_id = $2""",
        row["task_step_id"], user_id, step_status, output,
        (error or "")[:1000] or None)
    await conn.execute(
        """update public.tasks set status = $3, error_message = $4,
                  completed_at = case when $3 = 'completed' then now() else completed_at end,
                  cancelled_at = case when $3 = 'cancelled' then now() else cancelled_at end
           where id = $1 and user_id = $2""",
        row["task_id"], user_id, task_status, (error or "")[:1000] or None)
    await conn.execute(
        """insert into public.action_logs (user_id, workspace_id, task_id, task_step_id, actor, action, target,
                                           risk_level, outcome, details)
           values ($1, $2, $3, $4, 'user', $5, $6, $7, $8, $9)""",
        user_id, row["workspace_id"], row["task_id"], row["task_step_id"],
        f"{'reject' if ok is None else 'execute'}:{preview.get('tool')}", row["action_summary"][:300],
        row["risk_level"], outcome, {"approval_id": str(row["id"]), "error": error})
