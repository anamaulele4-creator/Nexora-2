from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database_info import expected_migration

router = APIRouter(tags=["health"])
VERSION = "0.1.0"


@router.get("/health")
async def health() -> dict:
    """Liveness: o processo está vivo. Não toca em dependências."""
    return {"status": "ok", "service": "connector-ai-api", "version": VERSION}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: base de dados acessível e migrada, Redis acessível."""
    state = request.app.state
    checks: dict[str, dict] = {}

    try:
        await asyncio.wait_for(state.db.ping(), timeout=3)
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    if checks["database"]["ok"]:
        try:
            current = await state.db.latest_migration()
            expected = expected_migration()
            checks["migrations"] = {"ok": current == expected, "current": current, "expected": expected}
        except Exception as exc:  # noqa: BLE001
            checks["migrations"] = {"ok": False, "error": type(exc).__name__}
    else:
        checks["migrations"] = {"ok": False, "error": "database_unavailable"}

    try:
        pong = await asyncio.wait_for(state.redis.ping(), timeout=3)
        checks["redis"] = {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = {"ok": False, "error": type(exc).__name__}

    ok = all(c["ok"] for c in checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)
