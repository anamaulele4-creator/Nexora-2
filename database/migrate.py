"""Executor de migrações SQL do CONNECTOR AI.

Uso:
    python database/migrate.py up [--seed]   # aplica migrações pendentes (e seeds)
    python database/migrate.py status        # mostra o estado

Lê DATABASE_URL do ambiente. Cada ficheiro corre numa transação própria;
o checksum de cada migração aplicada é guardado e verificado, para detetar
ficheiros alterados depois de aplicados (nunca se edita uma migração aplicada).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "migrations"
SEEDS_DIR = ROOT / "seeds"

BOOTSTRAP = """
create table if not exists public.schema_migrations (
  version     text primary key,
  checksum    text not null,
  applied_at  timestamptz not null default now()
);
"""


class MigrationError(RuntimeError):
    pass


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def latest_version() -> str:
    files = migration_files()
    return files[-1].stem if files else ""


def _normalise_dsn(dsn: str) -> str:
    # aceita URLs no formato SQLAlchemy (postgresql+asyncpg://)
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def applied_versions(conn: asyncpg.Connection) -> dict[str, str]:
    exists = await conn.fetchval("select to_regclass('public.schema_migrations') is not null")
    if not exists:
        return {}
    rows = await conn.fetch("select version, checksum from public.schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def migrate(dsn: str, seed: bool = False, verbose: bool = True) -> list[str]:
    conn = await asyncpg.connect(_normalise_dsn(dsn))
    applied_now: list[str] = []
    try:
        await conn.execute("set time zone 'UTC'")
        # lock consultivo: duas instâncias nunca migram ao mesmo tempo
        await conn.execute("select pg_advisory_lock(727274)")
        try:
            await conn.execute(BOOTSTRAP)
            done = await applied_versions(conn)
            for path in migration_files():
                sql = path.read_text(encoding="utf-8")
                version, checksum = path.stem, _checksum(sql)
                if version in done:
                    if done[version] != checksum:
                        raise MigrationError(
                            f"A migração {version} foi alterada depois de aplicada. "
                            "Cria uma nova migração em vez de editar esta."
                        )
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "insert into public.schema_migrations (version, checksum) values ($1, $2)",
                        version,
                        checksum,
                    )
                applied_now.append(version)
                if verbose:
                    print(f"aplicada  {version}")
            if seed:
                for path in sorted(SEEDS_DIR.glob("*.sql")):
                    async with conn.transaction():
                        await conn.execute(path.read_text(encoding="utf-8"))
                    if verbose:
                        print(f"seed      {path.stem}")
        finally:
            await conn.execute("select pg_advisory_unlock(727274)")
    finally:
        await conn.close()
    if verbose and not applied_now:
        print("Base de dados já está atualizada.")
    return applied_now


async def status(dsn: str) -> None:
    conn = await asyncpg.connect(_normalise_dsn(dsn))
    try:
        done = await applied_versions(conn)
    finally:
        await conn.close()
    for path in migration_files():
        mark = "x" if path.stem in done else " "
        print(f"[{mark}] {path.stem}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "status"])
    parser.add_argument("--seed", action="store_true", help="aplica também database/seeds/*.sql")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("Erro: define DATABASE_URL.", file=sys.stderr)
        return 2
    try:
        if args.command == "up":
            asyncio.run(migrate(dsn, seed=args.seed))
        else:
            asyncio.run(status(dsn))
    except MigrationError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
