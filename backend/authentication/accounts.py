"""Criação da conta de domínio (users + user_profiles + workspace por defeito).

Usado pelos dois fornecedores de autenticação: no modo Supabase o id vem do
Supabase Auth; no modo local é gerado pela base de dados.
"""

from __future__ import annotations

import re
import uuid

import asyncpg


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
