"""`cybercut.node_attempts` 仓储 + claim / 租约协议（WB-01 §五）。

claim 协议要点：
1. `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` 只挑**一条**最早可领取记录；
2. 同一语句内原子更新为 `claimed` 并写 `claimed_by/claimed_at/heartbeat_at`；
3. 多 worker 并发时 SKIP LOCKED 保证**同一 attempt 不会被重复领取**；
4. `claimed` 租约（`heartbeat_at`）超时 → 恢复为 `queued`（可被再次领取）；
5. `running` 心跳超时 → 标记 `failed / WORKER_LOST`（**不直接重跑**，
   以免重复副作用；新尝试由重试策略另行创建）。
"""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json

from cybercut.contracts.enums import AttemptStatus, ExecutorKind
from cybercut.contracts.errors import ConflictError, LeaseConflictError
from cybercut.contracts.state_machine import NODE_ATTEMPT, assert_transition
from cybercut.storage._util import UNSET, new_id, require_row

_COLS = (
    "attempt_id, node_run_id, tenant_id, attempt_no, status, executor_kind,"
    " executor_ref, input_json, output_json, claimed_by, claimed_at, heartbeat_at,"
    " available_at, started_at, finished_at, error_code, error_message,"
    " error_details, progress, stage, message, created_at, updated_at"
)

#: 默认租约秒数（WB-01 §五.6：claimed 超过 60s 无心跳即恢复）
DEFAULT_LEASE_SECONDS = 60

#: worker 丢心跳时的稳定错误码
ERROR_WORKER_LOST = "WORKER_LOST"


# ── 单语句原子 claim ──────────────────────────────────────────────
_CLAIM_SQL = """
WITH picked AS (
    SELECT attempt_id
      FROM cybercut.node_attempts AS candidate
     WHERE candidate.status = 'queued'
       AND candidate.available_at <= now()
       AND (CAST(%s AS text) IS NULL OR EXISTS (
           SELECT 1 FROM cybercut.node_runs nr
            WHERE nr.node_run_id = candidate.node_run_id
              AND nr.run_id = %s
       ))
     ORDER BY candidate.available_at, candidate.created_at
     LIMIT 1
     FOR UPDATE SKIP LOCKED
)
UPDATE cybercut.node_attempts AS a
   SET status = 'claimed',
       claimed_by = %s,
       claimed_at = now(),
       heartbeat_at = now(),
       updated_at = now()
  FROM picked
 WHERE a.attempt_id = picked.attempt_id
RETURNING a.attempt_id, a.node_run_id, a.tenant_id, a.attempt_no, a.status,
          a.executor_kind, a.executor_ref, a.input_json, a.available_at, a.created_at,
          a.claimed_by, a.claimed_at, a.heartbeat_at
"""


def create(
    conn: Any,
    *,
    node_run_id: str,
    tenant_id: str,
    attempt_no: int,
    executor_kind: ExecutorKind | str = ExecutorKind.CYBERCUT_WORKER,
    input_json: dict | None = None,
    status: AttemptStatus | str = AttemptStatus.QUEUED,
    available_at: Any = UNSET,
    executor_ref: str | None = None,
    attempt_id: str | None = None,
) -> dict:
    """新建一次尝试（默认 queued，立即可领取）。"""
    aid = attempt_id or new_id()
    with conn.cursor() as cur:
        if available_at is UNSET:
            cur.execute(
                "INSERT INTO cybercut.node_attempts (attempt_id, node_run_id, tenant_id,"
                " attempt_no, status, executor_kind, executor_ref, input_json)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_COLS}",
                (aid, node_run_id, tenant_id, attempt_no, str(status),
                 str(executor_kind), executor_ref, Json(input_json or {})),
            )
        else:
            cur.execute(
                "INSERT INTO cybercut.node_attempts (attempt_id, node_run_id, tenant_id,"
                " attempt_no, status, executor_kind, executor_ref, input_json, available_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_COLS}",
                (aid, node_run_id, tenant_id, attempt_no, str(status),
                 str(executor_kind), executor_ref, Json(input_json or {}), available_at),
            )
        return dict(cur.fetchone())


def get(conn: Any, attempt_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_attempts"
            " WHERE attempt_id = %s AND tenant_id = %s",
            (attempt_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_for_update(conn: Any, attempt_id: str, tenant_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_attempts"
            " WHERE attempt_id = %s AND tenant_id = %s FOR UPDATE",
            (attempt_id, tenant_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_for_node_run(conn: Any, node_run_id: str, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLS} FROM cybercut.node_attempts"
            " WHERE node_run_id = %s AND tenant_id = %s ORDER BY attempt_no",
            (node_run_id, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def count_active(conn: Any, node_run_id: str, tenant_id: str) -> int:
    """未完结尝试数（queued/claimed/running）——编排层据此避免重复入队。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM cybercut.node_attempts"
            " WHERE node_run_id = %s AND tenant_id = %s"
            "   AND status IN ('queued','claimed','running')",
            (node_run_id, tenant_id),
        )
        return int(cur.fetchone()["n"])


def claim_next(conn: Any, worker_id: str, run_id: str | None = None) -> dict | None:
    """领取一条可执行 attempt；无可用任务返回 None。

    **必须在事务中调用**（调用方负责 commit）——提交前不得执行 handler。
    """
    with conn.cursor() as cur:
        cur.execute(_CLAIM_SQL, (run_id, run_id, worker_id))
        row = cur.fetchone()
        return dict(row) if row else None


def heartbeat(conn: Any, attempt_id: str, worker_id: str) -> bool:
    """续约（仅限本人持有的 claimed/running）。返回是否续约成功。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET heartbeat_at = now(), updated_at = now()"
            " WHERE attempt_id = %s AND claimed_by = %s"
            "   AND status IN ('claimed','running')"
            " RETURNING attempt_id",
            (attempt_id, worker_id),
        )
        return cur.fetchone() is not None


def update_progress(
    conn: Any, attempt_id: str, *, progress: float, stage: str, message: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET progress = %s, stage = %s, message = %s,"
            " heartbeat_at = now(), updated_at = now()"
            " WHERE attempt_id = %s AND status = 'running'",
            (max(0.0, min(100.0, float(progress))), stage, message, attempt_id),
        )


def mark_running(conn: Any, attempt_id: str, tenant_id: str, worker_id: str) -> dict:
    """`claimed -> running`（要求 claimed_by 仍为本人，防租约易主后误启动）。"""
    row = require_row(get_for_update(conn, attempt_id, tenant_id), what="node_attempt")
    assert_transition(NODE_ATTEMPT, row["status"], AttemptStatus.RUNNING)
    if row["claimed_by"] != worker_id:
        raise LeaseConflictError(
            f"attempt {attempt_id} 的租约不属于 {worker_id}（当前 {row['claimed_by']}）"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE cybercut.node_attempts SET status = 'running', started_at = now(),"
            " heartbeat_at = now(), updated_at = now()"
            " WHERE attempt_id = %s AND tenant_id = %s AND status = 'claimed'"
            "   AND claimed_by = %s"
            f" RETURNING {_COLS}",
            (attempt_id, tenant_id, worker_id),
        )
        updated = cur.fetchone()
    if updated is None:
        raise LeaseConflictError(f"attempt {attempt_id} 转为 running 失败（租约竞争）")
    return dict(updated)


def finish(
    conn: Any,
    attempt_id: str,
    tenant_id: str,
    *,
    to_status: AttemptStatus | str,
    output_json: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_details: dict | None = None,
) -> dict:
    """落终态（`succeeded` / `failed` / `cancelled`）；CAS 防并发覆盖。"""
    row = require_row(get_for_update(conn, attempt_id, tenant_id), what="node_attempt")
    assert_transition(NODE_ATTEMPT, row["status"], to_status)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET status = %s, output_json = %s,"
            " error_code = %s, error_message = %s, error_details = %s,"
            " finished_at = now(), heartbeat_at = now(), updated_at = now()"
            " WHERE attempt_id = %s AND tenant_id = %s AND status = %s"
            f" RETURNING {_COLS}",
            (
                str(to_status),
                Json(output_json) if output_json is not None else None,
                error_code,
                error_message,
                Json(error_details) if error_details is not None else None,
                attempt_id,
                tenant_id,
                row["status"],
            ),
        )
        updated = cur.fetchone()
    if updated is None:
        raise ConflictError(f"attempt {attempt_id} 终态写入冲突（并发修改）")
    return dict(updated)


def cancel(conn: Any, attempt_id: str, tenant_id: str) -> dict:
    """取消尝试：`queued/claimed/running -> cancelled`。"""
    row = require_row(get_for_update(conn, attempt_id, tenant_id), what="node_attempt")
    assert_transition(NODE_ATTEMPT, row["status"], AttemptStatus.CANCELLED)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET status = 'cancelled', finished_at = now(),"
            " updated_at = now()"
            " WHERE attempt_id = %s AND tenant_id = %s AND status = %s"
            f" RETURNING {_COLS}",
            (attempt_id, tenant_id, row["status"]),
        )
        updated = cur.fetchone()
    if updated is None:  # pragma: no cover
        raise ConflictError(f"attempt {attempt_id} 取消失败（并发）")
    return dict(updated)


def recover_expired_claims(
    conn: Any, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> list[str]:
    """`claimed` 租约超时（无心跳）→ 恢复 `queued`。返回被恢复的 attempt_id 列表。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET status = 'queued', claimed_by = NULL,"
            " claimed_at = NULL, heartbeat_at = NULL, updated_at = now()"
            " WHERE status = 'claimed'"
            "   AND heartbeat_at < now() - make_interval(secs => %s)"
            " RETURNING attempt_id",
            (int(lease_seconds),),
        )
        return [r["attempt_id"] for r in cur.fetchall()]


def fail_lost_running(
    conn: Any, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> list[dict]:
    """`running` 心跳超时 → `failed / WORKER_LOST`。

    返回 [{'attempt_id', 'node_run_id', 'tenant_id', 'attempt_no'}]，
    供重试策略创建**新的 attempt**（不重跑本条，避免重复副作用）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cybercut.node_attempts SET status = 'failed', error_code = %s,"
            " error_message = %s, finished_at = now(), updated_at = now()"
            " WHERE status = 'running'"
            "   AND heartbeat_at < now() - make_interval(secs => %s)"
            " RETURNING attempt_id, node_run_id, tenant_id, attempt_no",
            (ERROR_WORKER_LOST, "worker 心跳超时，执行结果不可知", int(lease_seconds)),
        )
        return [dict(r) for r in cur.fetchall()]


def require_running_owned(
    conn: Any, attempt_id: str, tenant_id: str, worker_id: str
) -> dict:
    """锁定并校验 attempt 仍为**本 worker 持有的 running**；否则抛领域异常。

    用于终态提交前的守卫（WB-01R §二.2）：
    - attempt 已被取消/判死（非 running）-> `ConflictError`，拒绝提交终态；
    - 租约易主（`claimed_by` 不是自己）-> `LeaseConflictError`，拒绝提交终态。
    """
    row = require_row(get_for_update(conn, attempt_id, tenant_id), what="node_attempt")
    if row["status"] != AttemptStatus.RUNNING.value:
        raise ConflictError(
            f"attempt {attempt_id} 当前状态为 {row['status']}（非 running），拒绝提交终态",
            details={"attempt_id": attempt_id, "status": row["status"]},
        )
    if row["claimed_by"] != worker_id:
        raise LeaseConflictError(
            f"attempt {attempt_id} 的租约不属于 {worker_id}（当前 {row['claimed_by']}）"
        )
    return row


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "ERROR_WORKER_LOST",
    "cancel",
    "claim_next",
    "count_active",
    "create",
    "fail_lost_running",
    "finish",
    "get",
    "get_for_update",
    "heartbeat",
    "list_for_node_run",
    "mark_running",
    "recover_expired_claims",
    "require_running_owned",
    "update_progress",
]
