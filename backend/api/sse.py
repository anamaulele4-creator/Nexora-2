"""Ajudas para Server-Sent Events (chat em streaming, API v1 e progresso de downloads)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import TypeVar

T = TypeVar("T")

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # proxies (nginx/Render) não devem acumular a resposta
    "Connection": "keep-alive",
}

_DONE = object()


def sse_event(event: str | None, data: object) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {payload}\n\n"


SSE_PING = ": ping\n\n"


class Heartbeat:
    """Marcador devolvido por ``with_heartbeat`` quando não chegou nada em ``interval`` segundos."""


HEARTBEAT = Heartbeat()


async def with_heartbeat(source: AsyncIterator[T], interval: float = 15) -> AsyncIterator[T | Heartbeat]:
    """Lê ``source`` numa tarefa à parte e intercala HEARTBEAT enquanto espera.

    Modelos locais em CPU podem demorar dezenas de segundos até ao primeiro token (carregar
    o modelo); os pings mantêm a ligação viva através de proxies com tempo limite de inatividade.
    Se quem consome for cancelado (o cliente carregou em "Parar"), a fonte é cancelada também.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(("item", item))
            await queue.put(("end", _DONE))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reencaminhado para quem consome
            await queue.put(("error", exc))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield HEARTBEAT
                continue
            if kind == "item":
                yield value
            elif kind == "end":
                return
            else:
                raise value
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
