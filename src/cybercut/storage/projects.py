"""`cybercut.projects` 仓储。

所有读写均带 `tenant_id` 条件；跨租户与不存在**同形**返回 None / NotFound。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import ProjectStatus
from cybercut.contracts.errors import ConflictError
from cybercut.contracts.state_machine import PROJECT, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "project_id, tenant_id, name, goal_json, automation_policy_json, status, "
    "current_run_id, version, idempotency_key, archived_at, created_at, updated_at"
)


def create(
    conn: Any,
    *,
    tenant_id: str,
    name: str,
    goal: dict | None = None,
    automation_policy: dict | None = None,
    status: ProjectStatus | str = ProjectStatus.DRAFT,
    idempotency_key: str | None = None,
    project_id: str | None = None,
) -> dict:
    """新建项目（初始状态默认 draft）。"""
    pid = project_id or new_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.projects (project_id, tenant_id, name, goal_json,"
            " automation_policy_json, status, idempotency_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLS}",
            (
                pid,
                tenant_id,
                name,
                Json(goal or {}),
                Json(automation_policy or {}),
                str(status),
                idempotency_key,
            ),
        )
        return dict(cur.fetchone())


def get(conn: Any, project_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.projects"
            " WHERE project_id = %s AND tenant_id = %s",
            (project_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_for_update(conn: Any, project_id: str, tenant_id: str) -> dict | None:
    """加行锁读取（用于乐观锁 / 状态迁移的读-改-写）。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.projects"
            " WHERE project_id = %s AND tenant_id = %s FOR UPDATE",
            (project_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_tenant(conn: Any, tenant_id: str, limit: int = 100) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.projects WHERE tenant_id = %s"
            " ORDER BY created_at DESC LIMIT %s",
            (tenant_id, max(1, int(limit))),
        )
        return [dict(r) for r in cur.fetchall()]


def update_status(
    conn: Any,
    project_id: str,
    tenant_id: str,
    *,
    to_status: ProjectStatus | str,
    expected_version: int | None = None,
    current_run_id: Any = UNSET,
    archived: bool | None = None,
) -> dict:
    """状态迁移（含乐观锁 + 状态机校验）。

    - 状态机非法 -> `InvalidTransitionError`；
    - `expected_version` 不匹配 / 并发已被改 -> `ConflictError`。
    """
    row = require_row(get_for_update(conn, project_id, tenant_id), what="project")
    assert_transition(PROJECT, row["status"], to_status)
    if expected_version is not None and row["version"] != expected_version:
        raise ConflictError(
            f"project 版本冲突：期望 {expected_version}，实际 {row['version']}",
            details={"project_id": project_id, "expected": expected_version,
                     "actual": row["version"]},
        )

    sets = ["status = %s", "version = version + 1", "updated_at = now()"]
    params: list[Any] = [str(to_status)]
    if current_run_id is not UNSET:
        sets.append("current_run_id = %s")
        params.append(current_run_id)
    if archived is not None:
        sets.append("archived_at = CASE WHEN %s THEN now() ELSE NULL END")
        params.append(archived)
    params.extend([project_id, tenant_id, row["version"]])

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.projects SET {', '.join(sets)}"
            " WHERE project_id = %s AND tenant_id = %s AND version = %s"
            f" RETURNING {_COLS}",
            tuple(params),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover - 行锁下理论不可达
        raise ConflictError("project 并发更新冲突")
    return dict(updated)


def set_current_run(
    conn: Any,
    project_id: str,
    tenant_id: str,
    run_id: str,
    *,
    expected_version: int | None = None,
) -> dict:
    """仅改写 `current_run_id`（不触发状态迁移，仍走乐观锁 version+1）。"""
    row = require_row(get_for_update(conn, project_id, tenant_id), what="project")
    if expected_version is not None and row["version"] != expected_version:
        raise ConflictError(
            f"project 版本冲突：期望 {expected_version}，实际 {row['version']}"
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.projects SET current_run_id = %s, version = version + 1,"
            " updated_at = now()"
            " WHERE project_id = %s AND tenant_id = %s AND version = %s"
            f" RETURNING {_COLS}",
            (run_id, project_id, tenant_id, row["version"]),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError("project 并发更新冲突")
    return dict(updated)


def lock(conn: Any, project_id: str, tenant_id: str) -> dict:
    """以 `FOR UPDATE` 锁定项目行（WB-01R：串行化同项目的产物版本号分配等）。

    锁顺序约定见 ADR-0002 §决策 9：唯一全序 **`run → node_run → node_attempt → project`**，
    Project 为**最内层**（调用方应已按序持有 run / node_run / node_attempt 的锁）。
    """
    return require_row(get_for_update(conn, project_id, tenant_id), what="project")


__all__ = [
    "create",
    "get",
    "get_for_update",
    "list_for_tenant",
    "lock",
    "set_current_run",
    "update_status",
]
