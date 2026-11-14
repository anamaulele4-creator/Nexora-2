from __future__ import annotations

import uuid

import asyncpg


async def record_audit(
    conn: asyncpg.Connection,
    event_type: str,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    actor_type: str = "user",
    ip: str | None = None,
    user_agent: str | None = None,
    details: dict | None = None,
) -> None:
    """Escreve no registo de auditoria (só acrescento). Deve correr em modo serviço."""
    await conn.execute(
        """insert into public.audit_logs (user_id, workspace_id, actor_type, event_type, ip, user_agent, details)
           values ($1, $2, $3, $4, $5::inet, $6, $7)""",
        user_id, workspace_id, actor_type, event_type, ip, (user_agent or "")[:500] or None, details or {},
    )
