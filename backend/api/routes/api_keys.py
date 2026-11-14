"""Administração dos tokens da API pública v1 (criar, listar, revogar). Só administradores."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from api.deps import client_info, get_db, require_admin, safe_ip
from api.schemas import _In
from authentication.tokens import Principal
from core.db import Database
from core.errors import ApiError
from security.api_keys import generate_token
from security.audit import record_audit

router = APIRouter(prefix="/api/admin/api-keys", tags=["admin", "api-keys"])

ALLOWED_SCOPES = ("models", "chat")


class ApiKeyIn(_In):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    scopes: list[str] = Field(default_factory=lambda: list(ALLOWED_SCOPES), min_length=1, max_length=5)


def _out(r) -> dict:
    return {
        "id": str(r["id"]),
        "name": r["name"],
        "key_prefix": r["key_prefix"],
        "scopes": list(r["scopes"]),
        "created_at": r["created_at"].isoformat(),
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
        "revoked_at": r["revoked_at"].isoformat() if r["revoked_at"] else None,
        "created_by_email": r["created_by_email"],
        "requests_30d": r["requests_30d"],
        "tokens_30d": r["tokens_30d"],
    }


_SELECT = """select k.id, k.name, k.key_prefix, k.scopes, k.created_at, k.expires_at, k.last_used_at, k.revoked_at,
                    u.email::text as created_by_email,
                    (select count(*)::int from public.api_usage_logs l
                      where l.api_key_id = k.id and l.created_at >= now() - interval '30 days') as requests_30d,
                    (select coalesce(sum(coalesce(l.input_tokens, 0) + coalesce(l.output_tokens, 0)), 0)::bigint
                       from public.api_usage_logs l
                      where l.api_key_id = k.id and l.created_at >= now() - interval '30 days') as tokens_30d
             from public.api_keys k left join public.users u on u.id = k.user_id"""


@router.get("")
async def list_keys(_: Principal = Depends(require_admin), db: Database = Depends(get_db)) -> dict:
    async with db.service() as conn:
        rows = await conn.fetch(_SELECT + " order by k.revoked_at nulls first, k.created_at desc limit 200")
    return {"items": [_out(r) for r in rows]}


@router.post("", status_code=201)
async def create_key(body: ApiKeyIn, request: Request, principal: Principal = Depends(require_admin),
                     db: Database = Depends(get_db)) -> dict:
    scopes = sorted(set(body.scopes))
    bad = [s for s in scopes if s not in ALLOWED_SCOPES]
    if bad:
        raise ApiError(422, "invalid_scope", f"Âmbitos permitidos: {', '.join(ALLOWED_SCOPES)}")
    token, prefix, digest = generate_token()
    expires = datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    client = client_info(request)
    async with db.service() as conn:
        key_id = await conn.fetchval(
            """insert into public.api_keys (user_id, name, key_prefix, key_hash, scopes, expires_at)
               values ($1, $2, $3, $4, $5, $6) returning id""",
            principal.user_id, body.name.strip(), prefix, digest, scopes, expires)
        await record_audit(conn, "admin.api_key_created", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent,
                           details={"api_key_id": str(key_id), "name": body.name, "scopes": scopes})
        row = await conn.fetchrow(_SELECT + " where k.id = $1", key_id)
    out = _out(row)
    out["token"] = token  # só agora: nunca mais é mostrado
    return out


@router.delete("/{key_id}")
async def revoke_key(key_id: uuid.UUID, request: Request, principal: Principal = Depends(require_admin),
                     db: Database = Depends(get_db)) -> dict:
    client = client_info(request)
    async with db.service() as conn:
        done = await conn.fetchval(
            "update public.api_keys set revoked_at = coalesce(revoked_at, now()) where id = $1 returning id", key_id)
        if done is None:
            raise ApiError(404, "not_found", "Chave não encontrada")
        await record_audit(conn, "admin.api_key_revoked", user_id=principal.user_id, ip=safe_ip(client.ip),
                           user_agent=client.user_agent, details={"api_key_id": str(key_id)})
        row = await conn.fetchrow(_SELECT + " where k.id = $1", key_id)
    return _out(row)
