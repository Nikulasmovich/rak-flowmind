"""`cybercut.events` 仓储（只追加）。

- `event_id` 为 `bigserial`，由 PG 单调分配（并发下亦稳定递增）；
- 表上有 `BEFORE UPDATE OR DELETE` 触发器，数据库层面禁止改写/删除；
- 应用层只提供 `append` 与读取接口。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

_COLS = (
    "event_id, tenant_id, project_id, run_id, node_run_id, event_type,"
    " payload_json, created_at"
)


def append(
    conn: Any,
    *,
    tenant_id: str,
    event_type: str,
    payload: dict | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    node_run_id: str | None = None,
) -> int:
    """追加一条事件，返回分配的 event_id。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.events (tenant_id, project_id, run_id, node_run_id,"
            " event_type, payload_json) VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING event_id",
            (tenant_id, project_id, run_id, node_run_id, event_type, Json(payload or {})),
        )
        return int(cur.fetchone()["event_id"])


def list_for_scope(
    conn: Any,
    tenant_id: str,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    after_event_id: int = 0,
    limit: int = 200,
) -> list[dict]:
    """按租户 +（可选）项目/运行过滤，按 event_id 升序返回。"""
    clauses = ["tenant_id = %s", "event_id > %s"]
    params: list[Any] = [tenant_id, int(after_event_id)]
    if project_id is not None:
        clauses.append("project_id = %s")
        params.append(project_id)
    if run_id is not None:
        clauses.append("run_id = %s")
        params.append(run_id)
    params.append(max(1, int(limit)))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.events WHERE {' AND '.join(clauses)}"
            " ORDER BY event_id ASC LIMIT %s",
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]


def get(conn: Any, event_id: int, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.events WHERE event_id = %s AND tenant_id = %s",
            (int(event_id), tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


__all__ = ["append", "get", "list_for_scope"]
