"""Versão de migração esperada pelo código (a última em database/migrations)."""

from __future__ import annotations

import os
from pathlib import Path


def _migrations_dir() -> Path:
    env = os.environ.get("MIGRATIONS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "database" / "migrations"


def expected_migration() -> str | None:
    files = sorted(_migrations_dir().glob("*.sql"))
    return files[-1].stem if files else None
