"""Criação da conta de domínio (users + user_profiles + workspace por defeito).

Usado pelos dois fornecedores de autenticação: no modo Supabase o id vem do
Supabase Auth; no modo local é gerado pela base de dados.
"""

from __future__ import annotations

import re
import uuid

import asyncpg

from security.audit import record_audit


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return slug or "workspace"


async def create_account(
    conn: asyncpg.Connection,
    *,
    email: str,
    display_name: str | None,
    user_id: uuid.UUID | None = None,
    email_verified: bool = False,
) -> uuid.UUID:
    uid = await conn.fetchval(
        """insert into public.users (id, email, email_verified_at)
           values (coalesce($1, gen_random_uuid()), $2, case when $3 then now() end)
           returning id""",
        user_id, email, email_verified,
    )
    ws_name = f"{display_name}'s workspace" if display_name else "My workspace"
    ws_id = await conn.fetchval(
        "insert into public.workspaces (owner_user_id, name, slug) values ($1, $2, $3) returning id",
        uid, ws_name[:120], _slugify(ws_name),
    )
    await conn.execute(
        "insert into public.user_profiles (user_id, display_name, default_workspace_id) values ($1, $2, $3)",
        uid, display_name, ws_id,
    )
    return uid


async def sync_admin_flag(conn: asyncpg.Connection, user_id: uuid.UUID, admin_emails: list[str]) -> bool:
    """Promove a admin uma conta cujo email (confirmado) está em ADMIN_EMAILS.

    Só promove, nunca despromove (retirar admin é uma operação manual e auditada).
    Corre em modo serviço. Devolve True se a conta ficou promovida agora.
    """
    if not admin_emails:
        return False
    promoted = await conn.fetchval(
        """update public.users set is_admin = true
           where id = $1 and is_admin = false and email_verified_at is not null
             and lower(email::text) = any($2::text[])
           returning true""",
        user_id, [e.lower() for e in admin_emails],
    )
    if promoted:
        await record_audit(conn, "auth.admin_granted", user_id=user_id, actor_type="system",
                           details={"reason": "ADMIN_EMAILS"})
    return bool(promoted)
