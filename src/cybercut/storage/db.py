"""PostgreSQL 连接与事务基础设施。

约定（WB-01 §二/§八）：
- 沿用现有 `psycopg2` 连接能力（不引入 ORM / 迁移框架）；
- **短连接**：每次 `transaction()` 开一条新连接，提交/回滚后关闭——
  与基座 `TaskStore` 的 PgBouncer 事务模式口径一致，且天然支持多 worker 并发；
- 连接级 `SET TIME ZONE 'UTC'`：保证 `now()` 与所有 timestamptz 呈现为 UTC；
- **SQL 参数化**：所有值经 psycopg2 占位符（`%s`）传递；标识符固定写死在代码中，
  绝不接受请求输入作为标识符。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg2.extras import RealDictCursor

from cybercut.contracts.errors import CyberCutError

logger = logging.getLogger(__name__)

DEFAULT_APPLICATION_NAME = "cybercut-control-plane"


class DatabaseError(CyberCutError):
    """数据库不可用 / 连接失败 / SQL 执行错误。"""

    code = "DATABASE_ERROR"


def resolve_dsn() -> str:
    """连接串解析（优先专用变量，其次复用基座的通用 PG 变量）。

    顺序：`CYBERCUT_PG_DSN` -> `FLOWMIND_PG_DSN` -> `RAK_PG_*` 组合。
    均未配置时抛 `DatabaseError`（部署错误，显式失败，不静默降级）。
    """
    dsn = os.environ.get("CYBERCUT_PG_DSN", "").strip() or os.environ.get(
        "FLOWMIND_PG_DSN", ""
    ).strip()
    if dsn:
        return dsn

    host = os.environ.get("RAK_PG_HOST", "").strip()
    user = os.environ.get("RAK_PG_APP_USER", "").strip()
    password = os.environ.get("RAK_PG_APP_PASS", "")
    if host and user:
        port = os.environ.get("RAK_PG_PORT", "5432").strip() or "5432"
        dbname = os.environ.get("CYBERCUT_PG_DB", "").strip() or os.environ.get(
            "FLOWMIND_PG_DB", ""
        ).strip() or "rak"
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    raise DatabaseError(
        "PostgreSQL 未配置：请设置 CYBERCUT_PG_DSN（或 FLOWMIND_PG_DSN / RAK_PG_*）"
    )


class Database:
    """短连接数据库门面。"""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = (dsn or "").strip() or None
        self._resolved: str | None = None

    @property
    def dsn(self) -> str:
        if self._resolved is None:
            self._resolved = self._dsn or resolve_dsn()
        return self._resolved

    # ── 连接 ──

    def connect(self) -> Any:
        """建立一条新连接（autocommit=False，UTC，RealDictCursor）。"""
        import psycopg2  # 懒 import：无 PG 环境下仍可 import 本模块做单元测试

        try:
            conn = psycopg2.connect(
                self.dsn,
                cursor_factory=RealDictCursor,
                application_name=DEFAULT_APPLICATION_NAME,
                connect_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装为领域错误
            raise DatabaseError(f"PostgreSQL 连接失败: {type(exc).__name__}: {exc}") from exc
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        conn.commit()
        return conn

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """事务上下文：正常退出提交，异常回滚；无论如何关闭连接。

        用法::

            with db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("...", (value,))
        """
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # pragma: no cover - 回滚本身失败
                logger.warning("回滚失败（连接可能已断开）", exc_info=True)
            raise
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    def ping(self) -> bool:
        """连通性探测（供测试 skip 判定使用，不抛异常）。"""
        try:
            conn = self.connect()
        except Exception:
            return False
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass
        return True


__all__ = ["Database", "DatabaseError", "DEFAULT_APPLICATION_NAME", "resolve_dsn"]
