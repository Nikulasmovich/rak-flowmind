"""`cybercut.projects` 仓储。

所有读写均带 `tenant_id` 条件；跨租户与不存在**同形**返回 None / NotFound。
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import ProjectStatus
from cybercut.contracts.errors import ConflictError, ValidationError
from cybercut.contracts.state_machine import PROJECT, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

logger = logging.getLogger(__name__)

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


def reopen_for_revision(conn: Any, project_id: str, tenant_id: str) -> dict:
    """**受控修订路径**：把已完成的 Project 重新打开为 `active`（WB-03R2）。

    通用状态迁移仍拒绝从 `completed` 出发；本函数只接受 `completed`，
    并把 `archived_at` 清空，供定向修订重跑被修订节点。
    """
    row = require_row(get_for_update(conn, project_id, tenant_id), what="project")
    if row["status"] != ProjectStatus.COMPLETED.value:
        raise ConflictError(
            f"仅 completed 的 Project 可因定向修订重开，当前为 {row['status']}",
            details={"project_id": project_id, "status": row["status"]},
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.projects SET status = %s, archived_at = NULL,"
            " updated_at = now()"
            " WHERE project_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            (str(ProjectStatus.ACTIVE), project_id, tenant_id),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError(f"project {project_id} 并发更新冲突")
    return dict(updated)


_TERMINAL_STATUSES = frozenset({
    ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED,
})


def apply_aggregate_status(
    conn: Any,
    project_id: str,
    tenant_id: str,
    *,
    to_status: ProjectStatus | str,
    reason: str,
) -> dict:
    """**受控聚合路径**：按「项目下所有 Run」的聚合结果写入项目终态（WB-03R2）。

    与通用 `update_status` 的区别：

    - `to_status` 必须是终态（`completed` / `failed` / `cancelled`）；
    - 与当前状态一致 → **幂等返回**，不做任何写入；
    - 当前为 `active` → 走通用状态机迁移（`active → 终态`）；
    - 当前为**另一个终态** → 允许做**聚合修正**：项目状态是「项目下所有 Run」的
      聚合结果，而非某个 Run 单独决定；若先前被较早 Run 完成而误标为
      `completed`，随后有 Run 失败 ⇒ 必须修正为 `failed`，不得保持错误状态。

    这条路径**只**应由 Orchestrator 的项目状态聚合逻辑调用。
    """
    target = ProjectStatus(to_status)
    if target not in _TERMINAL_STATUSES:
        raise ValidationError(
            f"聚合终态只允许 completed/failed/cancelled：{target!r}",
            details={"allowed": sorted(s.value for s in _TERMINAL_STATUSES)},
        )
    row = require_row(get_for_update(conn, project_id, tenant_id), what="project")
    if row["status"] == target.value:
        return row  # 幂等
    if row["status"] == ProjectStatus.ACTIVE.value:
        return update_status(conn, project_id, tenant_id, to_status=target)

    # 终态之间的聚合修正（不走通用状态迁移；显式、带 reason 的受控例外）
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.projects SET status = %s, updated_at = now()"
            " WHERE project_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            (target.value, project_id, tenant_id),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError(f"project {project_id} 并发更新冲突")
    logger.warning(
        "项目终态聚合修正：%s -> %s（reason=%s）",
        row["status"], target.value, reason)
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
    "apply_aggregate_status",
    "reopen_for_revision",
    "set_current_run",
    "update_status",
]
