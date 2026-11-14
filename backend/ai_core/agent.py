"""Ciclo de ferramentas do chat (agente).

O modelo recebe as ferramentas disponíveis para ESTE utilizador. Enquanto pedir ferramentas,
o backend executa-as (pela ``Toolbox``) e devolve o resultado; para quando o modelo responde só
com texto ou quando se atinge ``max_iterations`` (nesse caso há uma última volta sem ferramentas).

Devolve, por ordem: pedaços de texto (str), ``AgentEvent`` (atividade de ferramentas, pedidos
para ligar uma conta, ações à espera de confirmação) e, no fim, um ``AIReply``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from ai_core.providers import AIReply, ChatMessage, ProviderError
from ai_core.tool_calling import ToolCall, Turn

log = logging.getLogger("connector_ai.agent")

MAX_ITERATIONS = 5
MAX_RESULT_CHARS = 24_000


@dataclass(frozen=True)
class AgentEvent:
    kind: str  # "tool" | "connect" | "approval"
    data: dict


@dataclass
class ToolOutcome:
    result: dict
    events: list[AgentEvent] = field(default_factory=list)
    ok: bool = True


class Toolbox(Protocol):
    def definitions(self) -> list[dict]: ...

    def label(self, name: str) -> str: ...

    async def call(self, name: str, arguments: dict) -> ToolOutcome: ...


class TurnClient(Protocol):
    provider: str
    model: str

    def stream_turn(self, messages: list[dict], tools: list[dict], allow_tools: bool = True
                    ) -> AsyncIterator[str | Turn]: ...


def _result_text(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = json.dumps({"truncated": True, "partial": text[:MAX_RESULT_CHARS]}, ensure_ascii=False)
    return text


FALLBACK_TEXT = {
    "limit": "Não consegui concluir dentro do limite de passos desta resposta. Diz-me como queres continuar.",
    "connect": "Para isso preciso que ligues a conta: usa o botão abaixo e depois volta a pedir.",
    "approval": "Preparei a ação: confirma ou recusa no cartão abaixo. Nada é feito sem a tua confirmação.",
}


async def run_agent(client: TurnClient, system: str, history: list[ChatMessage], toolbox: Toolbox,
                    max_iterations: int = MAX_ITERATIONS) -> AsyncIterator[str | AgentEvent | AIReply]:
    convo: list[dict] = [{"role": "system", "content": system}]
    convo.extend({"role": m.role, "content": m.content} for m in history)
    tools = toolbox.definitions()
    texts: list[str] = []
    tin = tout = 0
    have_usage = False
    model = client.model
    kinds: set[str] = set()
    rounds = 0

    while True:
        allow = bool(tools) and rounds < max_iterations
        if rounds >= max_iterations:
            convo.append({"role": "system", "content": (
                "Limite de ferramentas desta resposta atingido. Responde agora ao utilizador com o que já "
                "sabes, sem chamar mais ferramentas.")})
        turn: Turn | None = None
        sep_pending = bool(texts) and bool(texts[-1].strip())
        async for item in client.stream_turn(convo, tools, allow):
            if isinstance(item, Turn):
                turn = item
            elif item:
                if sep_pending:
                    texts.append("\n\n")
                    yield "\n\n"
                    sep_pending = False
                texts.append(item)
                yield item
        if turn is None:  # pragma: no cover - os adaptadores terminam sempre com Turn
            raise ProviderError(client.provider, "Resposta incompleta do modelo")
        model = turn.model or model
        if turn.input_tokens is not None or turn.output_tokens is not None:
            have_usage = True
            tin += turn.input_tokens or 0
            tout += turn.output_tokens or 0
        if not turn.tool_calls or not allow:
            break
        rounds += 1
        assistant: dict = {"role": "assistant", "content": turn.text, "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls]}
        if turn.raw is not None:
            assistant["_raw"] = turn.raw
        convo.append(assistant)
        for call in turn.tool_calls:
            outcome = await _call(toolbox, call)
            for ev in outcome.events:
                kinds.add(ev.kind)
                yield ev
            yield AgentEvent("tool", {"name": call.name, "label": toolbox.label(call.name),
                                      "status": "ok" if outcome.ok else "error"})
            convo.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                          "content": _result_text(outcome.result)})

    text = "".join(texts).strip()
    if not text and rounds >= max_iterations:
        text = FALLBACK_TEXT["limit"]
        yield text
    if not text:
        for kind in ("approval", "connect"):
            if kind in kinds:
                text = FALLBACK_TEXT[kind]
                yield text
                break
    if not text:
        raise ProviderError(client.provider, "Resposta vazia do modelo", retryable=False)
    yield AIReply(text=text, provider=client.provider, model=model,  # type: ignore[arg-type]
                  input_tokens=tin if have_usage else None, output_tokens=tout if have_usage else None)


async def _call(toolbox: Toolbox, call: ToolCall) -> ToolOutcome:
    if call.arguments_error:
        return ToolOutcome({"ok": False, "error": "invalid_arguments", "message": call.arguments_error}, ok=False)
    try:
        return await toolbox.call(call.name, call.arguments)
    except Exception as exc:  # noqa: BLE001 - uma ferramenta nunca derruba a conversa
        log.exception("ferramenta falhou", extra={"fields": {"tool": call.name}})
        return ToolOutcome({"ok": False, "error": "tool_failed", "message": type(exc).__name__}, ok=False)
