"""claim 协议（WB-01 §五）。

规则：
1. 只挑 `status='queued' AND available_at<=now()` 的**最早一条**（`LIMIT 1`），
   禁止一次锁住批量任务；
2. 同一语句内原子更新为 `claimed` 并写 `claimed_by/claimed_at/heartbeat_at`；
3. `FOR UPDATE SKIP LOCKED` 使并发 worker 跳过已锁行 —— **同一 attempt 不会被重复领取**；
4. 领取与执行分离：事务提交后才允许执行 handler。
"""
from __future__ import annotations

from typing import Any

from cybercut.storage import node_attempts


def claim_next_attempt(
    db: Any, worker_id: str, *, run_id: str | None = None
) -> dict | None:
    """在**独立事务**中领取一条 attempt；无任务返回 None。

    事务边界（关键）：claim 必须在 handler 执行**之前**提交，
    这样长时间运行的 handler 不会持锁阻塞其它 worker 领取其它任务。
    """
    with db.transaction() as conn:
        row = node_attempts.claim_next(conn, worker_id, run_id=run_id)
    return row


__all__ = ["claim_next_attempt"]
