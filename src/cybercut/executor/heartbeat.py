"""独立周期心跳（WB-01R §二.1）。

背景：WB-01 的续租只发生在 `progress_cb` 内 —— 若 handler 长时间不报进度
（silent handler），`heartbeat_at` 会陈旧，巡检会把在跑的 attempt 误判为
`WORKER_LOST`。本模块把「续租」从进度回调中独立出来。

设计要点：
- **独立线程 + 独立数据库事务**：每次 beat 走 `db.transaction()`（新连接），
  不与 handler 或 progress_cb 共享连接；
- **间隔**：`min(lease_seconds / 3, 10s)`，测试可注入更短间隔。
  实际周期按**节拍起点**计算（等待 = `interval - 单次 beat 耗时`），
  因此实际周期 ≈ `interval` 而非 `interval + 连接开销`，从而稳定保持与 `lease` 的倍数余量；
- **失败即标记**：`beat()` 返回 False（续租失败 / 租约已易主）时置 `lease_lost`、
  写审计事件，并停止继续 beat；调用方据此**拒绝最终提交**；
- **无泄漏**：`stop()` 幂等，`join()` 等待线程真正退出（循环用 `Event.wait(interval)`，
  最长阻塞一个 interval）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: 心跳间隔上限（秒）
MAX_INTERVAL_SECONDS = 10.0
#: 心跳间隔下限（防止测试注入 0 造成忙等）
MIN_INTERVAL_SECONDS = 0.01


def compute_interval(
    lease_seconds: float, override: float | None = None
) -> float:
    """心跳间隔：`min(lease_seconds / 3, 10s)`；`override` 用于测试注入。"""
    if override is not None:
        return max(MIN_INTERVAL_SECONDS, float(override))
    return max(MIN_INTERVAL_SECONDS, min(float(lease_seconds) / 3.0, MAX_INTERVAL_SECONDS))


class HeartbeatMonitor:
    """在 handler 执行期间周期性续租的后台执行单元。"""

    def __init__(
        self,
        beat: Callable[[], bool],
        interval: float,
        *,
        on_lease_lost: Callable[[], None] | None = None,
    ) -> None:
        self._beat = beat
        self.interval = float(interval)
        self._on_lease_lost = on_lease_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._lease_lost = False
        self._beats = 0
        self._lost_reported = False

    # ── 生命周期 ──

    def start(self) -> None:
        """启动心跳线程（幂等：重复调用不重复起线程）。"""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="cybercut-heartbeat", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float | None = None) -> bool:
        """停止并**等待线程退出**；返回是否已可靠停止（幂等、可重复调用）。

        WB-01R2 §四：
        - `join` 超时且线程仍存活时 **不清空线程引用**（否则 `running` 会失真、
          也无法再次 join）；
        - 后续可再次调用 `stop()` 重试；
        - 仅在线程确已退出时清除引用。
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout if timeout is not None else self.interval + 10.0)
        if thread.is_alive():
            logger.error(
                "心跳线程未在超时内退出（interval=%.3fs）；保留线程引用以便重试 stop()",
                self.interval,
            )
            return False
        with self._lock:
            if self._thread is thread:
                self._thread = None
        return True

    # ── 观测 ──

    @property
    def lease_lost(self) -> bool:
        """续租是否已失败（失败后调用方必须拒绝最终提交）。"""
        return self._lease_lost

    @property
    def beats(self) -> int:
        """已执行的心跳次数（测试断言用）。"""
        return self._beats

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ── 内部 ──

    def _run(self) -> None:
        # 首个节拍也先等一个 interval（让 mark_running 写入的 heartbeat_at 生效）
        if self._stop.wait(self.interval):
            return
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                ok = bool(self._beat())
            except Exception:  # noqa: BLE001 - 续租异常视同续租失败
                logger.warning("周期心跳异常（视同续租失败）", exc_info=True)
                ok = False
            self._beats += 1
            if not ok:
                self._mark_lease_lost()
                break
            # 关键：按「节拍起点」计算等待，使**实际周期 ≈ interval**
            # （而非 interval + beat 的连接开销），从而保证与 lease 之间有足够倍数余量。
            elapsed = time.monotonic() - started
            if self._stop.wait(max(0.0, self.interval - elapsed)):
                break

    def _mark_lease_lost(self) -> None:
        if self._lost_reported:
            return
        self._lost_reported = True
        self._lease_lost = True
        logger.warning("租约丢失（heartbeat 返回 False），将拒绝本次最终提交")
        if self._on_lease_lost is not None:
            try:
                self._on_lease_lost()
            except Exception:  # noqa: BLE001 - 审计回调失败不得影响主流程
                logger.warning("lease_lost 审计回调失败", exc_info=True)


__all__ = ["MAX_INTERVAL_SECONDS", "MIN_INTERVAL_SECONDS", "HeartbeatMonitor", "compute_interval"]
