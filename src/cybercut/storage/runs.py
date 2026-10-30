"""`cybercut.runs` 仓储。"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import RUN_TERMINAL, RunStatus
from cybercut.contracts.errors import ConflictError
from cybercut.contracts.state_machine import RUN, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "run_id, project_id, tenant_id, run_no, status, pipeline_version,"
    " requested_from_node, started_at, finished_at, cancel_requested_at,"
    " failure_code, failure_message, input_json, created_at, updated_at"
)


def next_run_no(conn: Any, project_id: str, tenant_id: str) -> int:
    """下一个 run_no（项目内单调递增）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(run_no), 0) + 1 AS n FROM cybercut.runs"
            " WHERE project_id = %s AND tenant_id = %s",
            (project_id, tenant_id),
        )
        return int(cur.fetchone()["n"])


def create(
    conn: Any,
    *,
    project_id: str,
    tenant_id: str,
    pipeline_version: str,
    status: RunStatus | str = RunStatus.QUEUED,
    requested_from_node: str | None = None,
    run_no: int | None = None,
    run_id: str | None = None,
    input_json: dict | None = None,
) -> dict:
    rid = run_id or new_id()
    rno = run_no if run_no is not None else next_run_no(conn, project_id, tenant_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.runs (run_id, project_id, tenant_id, run_no, status,"
            " pipeline_version, requested_from_node, input_json)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLS}",
            (rid, project_id, tenant_id, rno, str(status), pipeline_version,
             requested_from_node, Json(input_json) if input_json else None),
        )
        return dict(cur.fetchone())


def get(conn: Any, run_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.runs WHERE run_id = %s AND tenant_id = %s",
            (run_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_for_update(conn: Any, run_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.runs WHERE run_id = %s AND tenant_id = %s"
            " FOR UPDATE",
            (run_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_project(conn: Any, project_id: str, tenant_id: str,
                     limit: int = 100) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.runs WHERE project_id = %s AND tenant_id = %s"
            " ORDER BY run_no DESC LIMIT %s",
            (project_id, tenant_id, max(1, int(limit))),
        )
        return [dict(r) for r in cur.fetchall()]


def statuses_for_project(conn: Any, project_id: str, tenant_id: str) -> list[str]:
    """项目下**全部** Run 的状态（无 limit；用于项目状态聚合，WB-03R2-复审）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM cybercut.runs"
            " WHERE project_id = %s AND tenant_id = %s",
            (project_id, tenant_id),
        )
        return [str(r["status"]) for r in cur.fetchall()]


def reopen_for_revision(conn: Any, run_id: str, tenant_id: str) -> dict:
    """**受控修订路径**：把已成功的 Run 重新打开为 `running`（WB-03R2）。

    与通用 `update_status` 的区别：
    - 通用状态迁移仍**拒绝**从终态（`succeeded` / `failed` / `cancelled`）出发；
    - 本函数只接受 `succeeded`，且同时清理终态元数据（`finished_at` /
      `failure_code` / `failure_message`），避免「running 却仍带 finished_at」的
      不一致状态；
    - 只应由 `Orchestrator.revise_node` / `revise_artifact` 调用。
    """
    row = require_row(get_for_update(conn, run_id, tenant_id), what="run")
    if row["status"] != RunStatus.SUCCEEDED.value:
        raise ConflictError(
            f"仅 succeeded 的 Run 可因定向修订重开，当前为 {row['status']}",
            details={"run_id": run_id, "status": row["status"]},
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.runs SET status = %s, finished_at = NULL,"
            " failure_code = NULL, failure_message = NULL, updated_at = now()"
            " WHERE run_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            (str(RunStatus.RUNNING), run_id, tenant_id),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError(f"run {run_id} 并发更新冲突")
    return dict(updated)


def set_input_json(
    conn: Any, run_id: str, tenant_id: str, input_json: dict | None
) -> dict:
    """整体替换 `runs.input_json`（WB-03R：写入创作层定向修订请求）。

    注意：`asset_ids` / `assets` / `creative_input` 等**已冻结**内容由调用方原样带回，
    本方法只做整字段写入，不做部分合并。
    """
    require_row(get_for_update(conn, run_id, tenant_id), what="run")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.runs SET input_json = %s, updated_at = now()"
            " WHERE run_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            (Json(input_json or {}), run_id, tenant_id),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError(f"run {run_id} 并发更新冲突")
    return dict(updated)


def update_status(
    conn: Any,
    run_id: str,
    tenant_id: str,
    *,
    to_status: RunStatus | str,
    mark_started: bool = False,
    mark_finished: bool = False,
    cancel_requested: bool = False,
    failure_code: Any = UNSET,
    failure_message: Any = UNSET,
) -> dict:
    """状态迁移（状态机校验 + 附加字段）。终态自动落 finished_at。"""
    row = require_row(get_for_update(conn, run_id, tenant_id), what="run")
    assert_transition(RUN, row["status"], to_status)

    target = str(to_status)
    sets = ["status = %s", "updated_at = now()"]
    params: list[Any] = [target]
    if mark_started and row["started_at"] is None:
        sets.append("started_at = now()")
    if mark_finished or target in {s.value for s in RUN_TERMINAL}:
        sets.append("finished_at = now()")
    if cancel_requested:
        sets.append("cancel_requested_at = coalesce(cancel_requested_at, now())")
    if failure_code is not UNSET:
        sets.append("failure_code = %s")
        params.append(failure_code)
    if failure_message is not UNSET:
        sets.append("failure_message = %s")
        params.append(failure_message)
    params.extend([run_id, tenant_id])

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.runs SET {', '.join(sets)}"
            " WHERE run_id = %s AND tenant_id = %s"
            f" RETURNING {_COLS}",
            tuple(params),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError("run 并发更新冲突")
    return dict(updated)


__all__ = [
    "create",
    "get",
    "get_for_update",
    "list_for_project",
    "next_run_no",
    "reopen_for_revision",
    "set_input_json",
    "statuses_for_project",
    "update_status",
]
