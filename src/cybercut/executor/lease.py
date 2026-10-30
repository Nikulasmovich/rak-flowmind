"""租约管理：心跳续约 / claimed 超时恢复 / running 心跳超时判死（WB-01 §五.5-7）。"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.enums import NodeRunStatus
from cybercut.storage import node_attempts, node_runs
from cybercut.storage.node_attempts import DEFAULT_LEASE_SECONDS


def recover_expired_claims(
    db: Any, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> list[str]:
    """`claimed` 租约超时（无心跳）-> 恢复 `queued`，可被任意 worker 重新领取。"""
    with db.transaction() as conn:
        return node_attempts.recover_expired_claims(conn, lease_seconds)


def fail_lost_running_attempts(
    db: Any, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> list[dict]:
    """`running` 心跳超时 -> `failed / WORKER_LOST`，并把对应 node_run 置 `failed`。

    **不重跑**该 attempt：running 期间可能已产生副作用，直接重跑有重复副作用风险；
    新尝试由重试策略（orchestrator.advance_run）另行创建。
    """
    with db.transaction() as conn:
        lost = node_attempts.fail_lost_running(conn, lease_seconds)
        for item in lost:
            node_run = node_runs.get(conn, item["node_run_id"], item["tenant_id"])
            if node_run is not None and node_run["status"] == NodeRunStatus.RUNNING:
                node_runs.set_status(
                    conn, item["node_run_id"], item["tenant_id"],
                    to_status=NodeRunStatus.FAILED,
                )
    return lost


class LeaseManager:
    """持有 worker 身份的租约管理器（心跳 + 扫描恢复）。"""

    def __init__(
        self,
        db: Any,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._db = db
        self._worker_id = worker_id
        self.lease_seconds = int(lease_seconds)

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def heartbeat(self, attempt_id: str) -> bool:
        """续约当前 worker 持有的 attempt。"""
        with self._db.transaction() as conn:
            return node_attempts.heartbeat(conn, attempt_id, self._worker_id)

    def recover_expired_claims(self) -> list[str]:
        return recover_expired_claims(self._db, self.lease_seconds)

    def fail_lost_running(self) -> list[dict]:
        return fail_lost_running_attempts(self._db, self.lease_seconds)


__all__ = [
    "LeaseManager",
    "fail_lost_running_attempts",
    "recover_expired_claims",
]
