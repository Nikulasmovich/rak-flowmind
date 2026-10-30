"""worker 循环（进程内）：领取 -> 执行 -> 交回编排层推进。

进程模型说明（WB-01 §五）：
- 本对象是**单个 worker 进程**的代表；多进程场景下每个进程持有不同 `worker_id`，
  通过 `SELECT ... FOR UPDATE SKIP LOCKED` 天然互斥，不会重复执行同一 attempt；
- 领取与执行分离：claim 事务提交后才执行 handler；
- 进程重启后，`queued` 记录仍在 PG 中，新 worker 可直接领取（见重启恢复测试）。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from cybercut.contracts.errors import HeartbeatStuckError
from cybercut.executor.lease import LeaseManager
from cybercut.executor.runner import AttemptOutcome, AttemptRunner
from cybercut.storage.node_attempts import DEFAULT_LEASE_SECONDS

logger = logging.getLogger(__name__)


class Worker:
    """单进程 worker。"""

    def __init__(
        self,
        db: Any,
        registry: Any,
        *,
        worker_id: str | None = None,
        data_root: Path | str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        orchestrator: Any = None,
        heartbeat_interval: float | None = None,
        heartbeat_probe: Any = None,
        hash_hook: Any = None,
        heartbeat_stop_timeout: float | None = None,
        run_id: str | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._data_root = data_root
        self.lease_seconds = int(lease_seconds)
        self._orchestrator = orchestrator
        self.run_id = run_id
        #: 进程级致命错误标记（如心跳无法停止）：置位后拒绝继续领取任务
        self.poisoned: HeartbeatStuckError | None = None
        self.runner = AttemptRunner(
            db, registry, data_root=data_root, lease_seconds=lease_seconds,
            heartbeat_interval=heartbeat_interval, heartbeat_probe=heartbeat_probe,
            hash_hook=hash_hook, heartbeat_stop_timeout=heartbeat_stop_timeout,
            run_id=run_id,
        )
        self.leases = LeaseManager(db, self.worker_id, lease_seconds)

    # ── 单步 ──

    def step(self) -> AttemptOutcome | None:
        """领取并执行一条 attempt；随后（若装配了编排层）推进 Run 状态。

        `HeartbeatStuckError` 是**进程级致命错误**：worker 被标记为 poisoned 并向上抛出，
        绝不再领取新任务（避免在心跳线程仍存活时继续消费）。
        """
        if self.poisoned is not None:
            raise self.poisoned
        try:
            outcome = self.runner.run_once(self.worker_id)
        except HeartbeatStuckError as exc:
            self.poisoned = exc
            logger.error("worker %s 被标记为不可用（心跳无法停止）", self.worker_id)
            raise
        if outcome is None:
            return None
        self._advance(outcome.run_id, outcome.tenant_id)
        return outcome

    def drain(self, max_steps: int = 100) -> list[AttemptOutcome]:
        """持续领取直到无任务或达到步数上限（防死循环）。"""
        outcomes: list[AttemptOutcome] = []
        for _ in range(max(1, int(max_steps))):
            outcome = self.step()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # ── 租约恢复（进程启动 / 定期巡检时调用）──

    def recover(self) -> dict[str, Any]:
        """恢复过期 claimed；把心跳超时的 running 判死并交由编排层重试。

        返回 {"requeued": [...], "worker_lost": [...]} 便于测试断言。
        """
        requeued = self.leases.recover_expired_claims()
        lost = self.leases.fail_lost_running()
        touched: set[tuple[str, str]] = set()
        if self._orchestrator is not None:
            for item in lost:
                run_id, tenant_id = self._run_of(item["node_run_id"], item["tenant_id"])
                if run_id and (run_id, tenant_id) not in touched:
                    touched.add((run_id, tenant_id))
                    self._orchestrator.advance_run(run_id, tenant_id)
        return {"requeued": requeued, "worker_lost": lost}

    def _run_of(self, node_run_id: str, tenant_id: str) -> tuple[str | None, str]:
        from cybercut.storage import node_runs

        with self._db.transaction() as conn:
            nr = node_runs.get(conn, node_run_id, tenant_id)
        return (nr["run_id"] if nr else None), tenant_id

    def _advance(self, run_id: str, tenant_id: str) -> None:
        if self._orchestrator is None:
            return
        try:
            self._orchestrator.advance_run(run_id, tenant_id)
        except Exception:  # noqa: BLE001 - 编排推进失败不得吞掉 attempt 结果
            logger.exception("编排推进失败 run=%s", run_id)


__all__ = ["Worker"]
