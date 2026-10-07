"""`cybercut.node_runs` 仓储。"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import NodeRunStatus
from cybercut.contracts.errors import ConflictError
from cybercut.contracts.state_machine import NODE_RUN, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "node_run_id, run_id, tenant_id, node_key, status, depends_on,"
    " input_revision_refs, latest_attempt_no, approval_required,"
    " invalidated_at, invalidated_reason, created_at, updated_at"
)


def create(
    conn: Any,
    *,
    run_id: str,
    tenant_id: str,
    node_key: str,
    status: NodeRunStatus | str = NodeRunStatus.BLOCKED,
    depends_on: list[str] | None = None,
    approval_required: bool = False,
    input_revision_refs: dict | None = None,
    node_run_id: str | None = None,
) -> dict:
    nid = node_run_id or new_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.node_runs (node_run_id, run_id, tenant_id, node_key,"
            " status, depends_on, input_revision_refs, approval_required)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLS}",
            (nid, run_id, tenant_id, node_key, str(status),
             Json(list(depends_on or [])), Json(input_revision_refs or {}),
             bool(approval_required)),
        )
        return dict(cur.fetchone())


def get(conn: Any, node_run_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_runs"
            " WHERE node_run_id = %s AND tenant_id = %s",
            (node_run_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_for_update(conn: Any, node_run_id: str, tenant_id: str) -> dict | None:
    """加行锁读取（WB-01R2：统一锁顺序 run -> node_run -> node_attempt -> project）。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_runs"
            " WHERE node_run_id = %s AND tenant_id = %s FOR UPDATE",
            (node_run_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_by_key(conn: Any, run_id: str, node_key: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_runs"
            " WHERE run_id = %s AND node_key = %s AND tenant_id = %s",
            (run_id, node_key, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_run(conn: Any, run_id: str, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_runs"
            " WHERE run_id = %s AND tenant_id = %s ORDER BY node_key",
            (run_id, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def set_status(
    conn: Any,
    node_run_id: str,
    tenant_id: str,
    *,
    to_status: NodeRunStatus | str,
    invalidated_reason: Any = UNSET,
    clear_invalidated: bool = False,
) -> dict:
    """状态迁移（状态机校验）。`invalidated` 落时间戳，回到 ready 时清空。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_runs"
            " WHERE node_run_id = %s AND tenant_id = %s FOR UPDATE",
            (node_run_id, tenant_id),
        )
        row = cur.fetchone()
    current = require_row(dict(row) if row else None, what="node_run")
    target = str(to_status)
    assert_transition(NODE_RUN, current["status"], target)

    sets = ["status = %s", "updated_at = now()"]
    params: list[Any] = [target]
    if target == NodeRunStatus.INVALIDATED.value:
        sets.append("invalidated_at = now()")
        sets.append("invalidated_reason = %s")
        params.append(None if invalidated_reason is UNSET else invalidated_reason)
    elif clear_invalidated:
        sets.append("invalidated_at = NULL")
        sets.append("invalidated_reason = NULL")
    params.extend([node_run_id, tenant_id])

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.node_runs SET {', '.join(sets)}"
            " WHERE node_run_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            tuple(params),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError("node_run 并发更新冲突")
    return dict(updated)


def bump_attempt_no(conn: Any, node_run_id: str, tenant_id: str) -> int:
    """递增并返回 `latest_attempt_no`（在同一事务内原子完成）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_runs SET latest_attempt_no = latest_attempt_no + 1,"
            " updated_at = now()"
            " WHERE node_run_id = %s AND tenant_id = %s"
            " RETURNING latest_attempt_no",
            (node_run_id, tenant_id),
        )
        row = cur.fetchone()
    return int(require_row(dict(row) if row else None, what="node_run")["latest_attempt_no"])


__all__ = [
    "bump_attempt_no",
    "create",
    "get",
    "get_by_key",
    "get_for_update",
    "list_for_run",
    "set_status",
]
