"""`cybercut.approvals` 仓储（人工审批）。

`expected_revision` 是**乐观并发守卫**：
- 唯一键 `(node_run_id, expected_revision)` 保证同一版本的审批唯一；
- 审批决定必须携带并匹配当前待审记录的 `expected_revision`，
  否则视为「基于旧产物的过期决定」→ `ConflictError`。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import ApprovalStatus
from cybercut.contracts.errors import ConflictError
from cybercut.contracts.state_machine import APPROVAL, assert_transition
from cybercut.storage._util import new_id, require_row

_COLS = (
    "approval_id, project_id, run_id, node_run_id, artifact_revision_id, tenant_id,"
    " status, feedback, scope_json, expected_revision, decided_by, decided_at,"
    " created_at, updated_at"
)


def create_pending(
    conn: Any,
    *,
    project_id: str,
    run_id: str,
    node_run_id: str,
    tenant_id: str,
    expected_revision: int,
    artifact_revision_id: str | None = None,
    scope: dict | None = None,
    approval_id: str | None = None,
) -> dict:
    aid = approval_id or new_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.approvals (approval_id, project_id, run_id, node_run_id,"
            " artifact_revision_id, tenant_id, status, scope_json, expected_revision)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLS}",
            (aid, project_id, run_id, node_run_id, artifact_revision_id, tenant_id,
             str(ApprovalStatus.PENDING), Json(scope or {}), int(expected_revision)),
        )
        return dict(cur.fetchone())


def get(conn: Any, approval_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.approvals"
            " WHERE approval_id = %s AND tenant_id = %s",
            (approval_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_pending(conn: Any, node_run_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.approvals"
            " WHERE node_run_id = %s AND tenant_id = %s AND status = 'pending'"
            " ORDER BY expected_revision DESC LIMIT 1",
            (node_run_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_by_revision(
    conn: Any, node_run_id: str, expected_revision: int, tenant_id: str
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.approvals"
            " WHERE node_run_id = %s AND expected_revision = %s AND tenant_id = %s",
            (node_run_id, expected_revision, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_node_run(conn: Any, node_run_id: str, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.approvals"
            " WHERE node_run_id = %s AND tenant_id = %s ORDER BY expected_revision",
            (node_run_id, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def decide(
    conn: Any,
    approval_id: str,
    tenant_id: str,
    *,
    to_status: ApprovalStatus | str,
    decided_by: str,
    feedback: str | None = None,
) -> dict:
    """落审批决定（状态机校验；仅 pending 可决）。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.approvals"
            " WHERE approval_id = %s AND tenant_id = %s FOR UPDATE",
            (approval_id, tenant_id),
        )
        row = cur.fetchone()
    current = require_row(dict(row) if row else None, what="approval")
    assert_transition(APPROVAL, current["status"], to_status)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.approvals SET status = %s, decided_by = %s, feedback = %s,"
            " decided_at = now(), updated_at = now()"
            " WHERE approval_id = %s AND tenant_id = %s AND status = %s"
            f" RETURNING {_COLS}",
            (str(to_status), decided_by, feedback, approval_id, tenant_id,
             current["status"]),
        )
        updated = cur.fetchone()
    if updated is None:
        raise ConflictError(f"approval {approval_id} 决定写入冲突（并发）")
    return dict(updated)


__all__ = [
    "create_pending",
    "decide",
    "find_by_revision",
    "find_pending",
    "get",
    "list_for_node_run",
]
