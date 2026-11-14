from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import current_principal, get_db
from authentication.tokens import Principal
from core.db import Database

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


@router.get("")
async def list_connectors(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    """Catálogo visível ao utilizador. Na Etapa 1 todos estão 'planned' (nenhum funcional)."""
    async with db.as_user(principal.user_id) as conn:
        rows = await conn.fetch(
            """select key, name, description, category, auth_type, status, planned_stage, version
               from public.connector_definitions order by planned_stage nulls last, name""")
    return {"items": [dict(r) for r in rows]}
