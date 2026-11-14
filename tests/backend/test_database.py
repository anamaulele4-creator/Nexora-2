from __future__ import annotations

import json
import shutil
import uuid

import asyncpg
import pytest

import migrate as migrate_module

EXPECTED_TABLES = {
    "users", "user_profiles", "workspaces", "connector_definitions", "connected_apps", "connector_permissions",
    "encrypted_credentials", "browser_sessions", "tasks", "task_steps", "action_logs", "approvals", "memories",
    "files", "execution_results", "audit_logs", "usage_metrics", "workflows", "workflow_runs", "notifications",
}


async def _make_user(conn, email):
    from authentication.accounts import create_account

    uid = await create_account(conn, email=email, display_name=None, email_verified=True)
    ws = await conn.fetchval("select default_workspace_id from user_profiles where user_id = $1", uid)
    return uid, ws


async def _as(conn, uid):
    await conn.execute("set local role authenticated")
    await conn.execute("select set_config('request.jwt.claims', $1, true)", json.dumps({"sub": str(uid)}))


async def test_all_twenty_tables_exist_with_rls(db_conn):
    rows = await db_conn.fetch(
        "select tablename, rowsecurity from pg_tables where schemaname = 'public' and tablename <> 'schema_migrations'")
    tables = {r["tablename"]: r["rowsecurity"] for r in rows}
    assert set(tables) == EXPECTED_TABLES
    assert all(tables.values()), "RLS desligado em alguma tabela"


async def test_every_table_has_user_id_and_utc_timestamps(db_conn):
    cols = await db_conn.fetch(
        """select table_name, column_name, data_type from information_schema.columns
           where table_schema = 'public' and table_name <> 'schema_migrations'""")
    by_table: dict[str, dict[str, str]] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], {})[c["column_name"]] = c["data_type"]
    for table, columns in by_table.items():
        owner_col = {"users": "id", "workspaces": "owner_user_id"}.get(table, "user_id")
        assert owner_col in columns, f"{table} sem {owner_col}"
        for name, dtype in columns.items():
            if name.endswith("_at"):
                assert dtype == "timestamp with time zone", f"{table}.{name} não é timestamptz"


async def test_migrations_are_idempotent(migrated_db):
    assert await migrate_module.migrate(migrated_db, seed=True, verbose=False) == []


async def test_modified_migration_is_detected(migrated_db, tmp_path, monkeypatch):
    target = tmp_path / "migrations"
    shutil.copytree(migrate_module.MIGRATIONS_DIR, target)
    first = sorted(target.glob("*.sql"))[0]
    first.write_text(first.read_text() + "\n-- edit\n")
    monkeypatch.setattr(migrate_module, "MIGRATIONS_DIR", target)
    with pytest.raises(migrate_module.MigrationError):
        await migrate_module.migrate(migrated_db, verbose=False)


async def test_seeded_connectors_are_all_planned(db_conn):
    rows = await db_conn.fetch("select key, status from connector_definitions where is_public")
    assert {r["key"] for r in rows} == {"browser_generic", "gmail", "google_drive", "github", "shopify", "custom_api"}
    assert {r["status"] for r in rows} == {"planned"}


async def test_rls_isolates_users(db_conn):
    async with db_conn.transaction():
        a, ws_a = await _make_user(db_conn, f"rls-a-{uuid.uuid4().hex[:6]}@example.com")
        b, ws_b = await _make_user(db_conn, f"rls-b-{uuid.uuid4().hex[:6]}@example.com")
        await db_conn.execute(
            "insert into tasks (user_id, workspace_id, title, instruction) values ($1, $2, 'A task', 'do it')", a, ws_a)

        await db_conn.execute("savepoint s")
        await _as(db_conn, b)
        assert await db_conn.fetchval("select count(*) from tasks") == 0
        assert await db_conn.fetchval("select count(*) from users") == 1
        assert await db_conn.fetchval("select count(*) from workspaces where id = $1", ws_a) == 0
        # escrever em nome de outro utilizador viola a política
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.execute(
                "insert into tasks (user_id, workspace_id, title, instruction) values ($1, $2, 'x', 'y')", a, ws_a)
        await db_conn.execute("rollback to savepoint s")

        await _as(db_conn, b)
        # escrever no workspace de outro com o próprio user_id viola a FK composta
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await db_conn.execute(
                "insert into tasks (user_id, workspace_id, title, instruction) values ($1, $2, 'x', 'y')", b, ws_a)
        await db_conn.execute("rollback to savepoint s")

        await _as(db_conn, a)
        assert await db_conn.fetchval("select count(*) from tasks") == 1
        # nem o próprio utilizador lê credenciais cifradas
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.fetch("select * from encrypted_credentials")
        await db_conn.execute("rollback to savepoint s")

        await _as(db_conn, a)
        # o utilizador não se promove a admin
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.execute("update users set is_admin = true where id = $1", a)
        await db_conn.execute("rollback to savepoint s")

        await _as(db_conn, a)
        # o esquema de autenticação local é inacessível
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.fetch("select * from local_auth.credentials")
        await db_conn.execute("rollback to savepoint s")


async def test_audit_logs_are_append_only(db_conn):
    async with db_conn.transaction():
        await db_conn.execute("insert into audit_logs (actor_type, event_type) values ('system', 'test.event')")
        await db_conn.execute("savepoint s")
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_conn.execute("delete from audit_logs where event_type = 'test.event'")
        await db_conn.execute("rollback to savepoint s")
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await db_conn.execute("update audit_logs set event_type = 'x' where event_type = 'test.event'")
        await db_conn.execute("rollback to savepoint s")


async def test_task_status_and_approval_rules(db_conn):
    async with db_conn.transaction():
        uid, ws = await _make_user(db_conn, f"rules-{uuid.uuid4().hex[:6]}@example.com")
        await db_conn.execute("savepoint s")
        with pytest.raises(asyncpg.CheckViolationError):
            await db_conn.execute(
                "insert into tasks (user_id, workspace_id, title, instruction, status) values ($1,$2,'t','i','done')",
                uid, ws)
        await db_conn.execute("rollback to savepoint s")
        tid = await db_conn.fetchval(
            "insert into tasks (user_id, workspace_id, title, instruction, idempotency_key) values ($1,$2,'t','i','k1') returning id",
            uid, ws)
        with pytest.raises(asyncpg.UniqueViolationError):
            await db_conn.execute(
                "insert into tasks (user_id, workspace_id, title, instruction, idempotency_key) values ($1,$2,'t','i','k1')",
                uid, ws)
        await db_conn.execute("rollback to savepoint s")
        tid = await db_conn.fetchval(
            "insert into tasks (user_id, workspace_id, title, instruction) values ($1,$2,'t','i') returning id", uid, ws)
        aid = await db_conn.fetchval(
            """insert into approvals (user_id, workspace_id, task_id, category, action_summary, risk_level)
               values ($1,$2,$3,'send_message','Send email to x','high') returning id""", uid, ws, tid)
        await _as(db_conn, uid)
        # o utilizador decide (pending -> approved) mas não pode reabrir nem mudar o resumo
        await db_conn.execute("update approvals set status='approved', decided_at=now() where id=$1", aid)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await db_conn.execute("update approvals set action_summary='changed' where id=$1", aid)
