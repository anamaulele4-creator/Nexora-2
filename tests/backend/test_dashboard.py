from __future__ import annotations

from conftest import login, register_and_verify, unique_email


async def test_dashboard_requires_auth(client):
    assert (await client.get("/api/dashboard/summary")).status_code == 401


async def test_new_user_sees_real_zeros(client, app):
    email = unique_email("dash0")
    await register_and_verify(client, app, email)
    await login(client, email)
    r = await client.get("/api/dashboard/summary")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tasks"]["total"] == 0 and d["recent_tasks"] == []
    assert d["active_connections"] == 0 and d["pending_approvals"] == 0
    assert d["connectors"] == {"available": 0, "planned": 6}


async def test_dashboard_counts_come_from_database_and_respect_rls(client, app, db_conn):
    email_a, email_b = unique_email("dashA"), unique_email("dashB")
    uid_a = await register_and_verify(client, app, email_a)
    await register_and_verify(client, app, email_b)
    ws = await db_conn.fetchval("select default_workspace_id from user_profiles where user_id = $1::uuid", uid_a)
    for status in ("running", "completed", "completed", "waiting_approval"):
        await db_conn.execute(
            "insert into tasks (user_id, workspace_id, title, instruction, status) values ($1::uuid, $2, $3, 'x', $3)",
            uid_a, ws, status)
    tid = await db_conn.fetchval("select id from tasks where user_id = $1::uuid and status = 'waiting_approval'", uid_a)
    await db_conn.execute(
        """insert into approvals (user_id, workspace_id, task_id, category, action_summary, risk_level)
           values ($1::uuid, $2, $3, 'publish', 'Publish post', 'high')""", uid_a, ws, tid)
    await db_conn.execute(
        """insert into usage_metrics (user_id, workspace_id, metric, quantity, unit)
           values ($1::uuid, $2, 'tool_calls', 7, 'calls')""", uid_a, ws)

    await login(client, email_a)
    d = (await client.get("/api/dashboard/summary")).json()
    assert d["tasks"]["total"] == 4
    assert d["tasks"]["by_status"]["completed"] == 2
    assert d["tasks"]["in_progress"] == 1
    assert d["pending_approvals"] == 1
    assert d["usage_this_month"]["tool_calls"] == 7.0
    assert len(d["recent_tasks"]) == 4

    await client.post("/api/auth/logout")
    await login(client, email_b)
    d = (await client.get("/api/dashboard/summary")).json()
    assert d["tasks"]["total"] == 0 and d["pending_approvals"] == 0


async def test_connectors_catalog_lists_planned_only(client, app):
    email = unique_email("cat")
    await register_and_verify(client, app, email)
    await login(client, email)
    items = (await client.get("/api/connectors")).json()["items"]
    assert len(items) == 6
    assert all(i["status"] == "planned" for i in items)
