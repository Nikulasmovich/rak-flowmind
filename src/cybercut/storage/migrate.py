"""纯 SQL 迁移执行器（不引入 Alembic）。

契约：
- 迁移文件为 `migrations/cybercut/*.sql`，按**文件名升序**执行；
- 已应用版本记录在 `cybercut.schema_migrations`，**重复执行跳过**（幂等）；
- 同时所有 DDL 自身也写成 `IF NOT EXISTS` 形式，故即便账本丢失、直接重跑
  同一批 SQL 也不会报错（双保险）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cybercut.storage.db import Database

logger = logging.getLogger(__name__)

#: 仓库根 = <repo>/src/cybercut/storage/migrate.py 上溯四级
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MIGRATIONS_DIR = _REPO_ROOT / "migrations" / "cybercut"

_LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS cybercut;
CREATE TABLE IF NOT EXISTS cybercut.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def ensure_ledger(conn: Any) -> None:
    """确保迁移账本存在（幂等）。"""
    with conn.cursor() as cur:
        cur.execute(_LEDGER_DDL)


def applied_versions(conn: Any) -> set[str]:
    """已应用版本集合。"""
    ensure_ledger(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM cybercut.schema_migrations")
        return {row["version"] for row in cur.fetchall()}


def discover_migrations(migrations_dir: Path | str | None = None) -> list[Path]:
    """按文件名升序返回迁移文件列表。"""
    directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.sql"), key=lambda p: p.name)


def apply_migrations(
    db: Database, migrations_dir: Path | str | None = None
) -> list[str]:
    """应用尚未执行的迁移，返回**本次新应用**的版本列表。

    - 首次调用：应用全部并记录；
    - 再次调用：返回空列表（账本命中）；
    - SQL 执行失败：该版本整体回滚（一个文件 = 一个事务），异常向上抛。
    """
    applied_now: list[str] = []
    for path in discover_migrations(migrations_dir):
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        with db.transaction() as conn:
            if version in applied_versions(conn):
                continue
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO cybercut.schema_migrations (version) VALUES (%s)"
                    " ON CONFLICT (version) DO NOTHING",
                    (version,),
                )
            applied_now.append(version)
            logger.info("已应用迁移：%s", version)
    return applied_now


__all__ = [
    "DEFAULT_MIGRATIONS_DIR",
    "applied_versions",
    "apply_migrations",
    "discover_migrations",
    "ensure_ledger",
]
