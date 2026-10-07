"""`cybercut.runs` 仓储。"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.enums import RUN_TERMINAL, RunStatus
from cybercut.contracts.errors import ConflictError
from cybercut.contracts.state_machine import RUN, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "run_id, project_id, tenant_id, run_no, status, pipeline_version,"
    " requested_from_node, started_at, finished_at, cancel_requested_at,"
    " failure_code, failure_message, created_at, updated_at"
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
) -> dict:
    rid = run_id or new_id()
    rno = run_no if run_no is not None else next_run_no(conn, project_id, tenant_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.runs (run_id, project_id, tenant_id, run_no, status,"
            " pipeline_version, requested_from_node)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_COLS}",
            (rid, project_id, tenant_id, rno, str(status), pipeline_version,
             requested_from_node),
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
    "update_status",
]
