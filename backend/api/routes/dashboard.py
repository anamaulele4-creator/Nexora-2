"""Resumo do Dashboard: todos os números vêm da base de dados, sob RLS."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from api.deps import current_principal, get_db
from authentication.tokens import Principal
from core.db import Database

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

TASK_STATUSES = ("queued", "planning", "running", "waiting_approval", "completed", "failed", "cancelled", "paused")


@router.get("/summary")
async def summary(principal: Principal = Depends(current_principal), db: Database = Depends(get_db)) -> dict:
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with db.as_user(principal.user_id) as conn:
        by_status = {r["status"]: r["n"] for r in await conn.fetch(
            "select status, count(*)::int as n from public.tasks group by status")}
        counts = await conn.fetchrow(
            """select
                 (select count(*)::int from public.connected_apps where status = 'connected') as active_connections,
                 (select count(*)::int from public.approvals where status = 'pending') as pending_approvals,
                 (select count(*)::int from public.browser_sessions where status in ('starting','active','idle')) as active_browser_sessions,
                 (select count(*)::int from public.notifications where read_at is null) as unread_notifications,
                 (select count(*)::int from public.workflows where deleted_at is null) as workflows,
                 (select count(*)::int from public.connector_definitions where status in ('available','beta')) as connectors_available,
                 (select count(*)::int from public.connector_definitions where status = 'planned') as connectors_planned"""
        )
        usage = {r["metric"]: float(r["total"]) for r in await conn.fetch(
            """select metric, sum(quantity) as total from public.usage_metrics
               where recorded_at >= $1 group by metric""", month_start)}
        recent = await conn.fetch(
            """select id, title, status, mode, created_at, updated_at from public.tasks
               order by created_at desc limit 5""")
    tasks = {s: by_status.get(s, 0) for s in TASK_STATUSES}
    return {
        "generated_at": now.isoformat(),
        "tasks": {
            "total": sum(tasks.values()),
            "in_progress": tasks["queued"] + tasks["planning"] + tasks["running"],
            "by_status": tasks,
        },
        "active_connections": counts["active_connections"],
        "pending_approvals": counts["pending_approvals"],
        "active_browser_sessions": counts["active_browser_sessions"],
        "unread_notifications": counts["unread_notifications"],
        "workflows": counts["workflows"],
        "connectors": {"available": counts["connectors_available"], "planned": counts["connectors_planned"]},
        "usage_this_month": {
            "period_start": month_start.isoformat(),
            "tool_calls": usage.get("tool_calls", 0.0),
            "llm_input_tokens": usage.get("llm_input_tokens", 0.0),
            "llm_output_tokens": usage.get("llm_output_tokens", 0.0),
            "browser_minutes": usage.get("browser_minutes", 0.0),
            "tasks_run": usage.get("tasks_run", 0.0),
        },
        "recent_tasks": [
            {"id": str(r["id"]), "title": r["title"], "status": r["status"], "mode": r["mode"],
             "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}
            for r in recent
        ],
    }
