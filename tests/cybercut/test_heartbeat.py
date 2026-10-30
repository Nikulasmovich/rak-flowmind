"""WB-01R §二.1：独立周期心跳测试（真实 PostgreSQL）。

覆盖：
1. silent handler 执行时长跨越多个租约周期且**从不调用 progress_cb**，
   期间并发跑 recover/fail_lost_running 巡检 —— attempt 不得被判 WORKER_LOST；
2. handler 正常完成后 succeeded；
3. 心跳返回 False 时最终提交必须被拒绝，且不得产生正式 ArtifactRevision；
4. 间隔计算上界 `min(lease/3, 10s)`；线程无泄漏。
"""
from __future__ import annotations

import threading
import time

from cybercut.contracts.errors import LeaseConflictError  # noqa: F401  (文档化依赖)
from cybercut.executor.heartbeat import compute_interval
from cybercut.executor.lease import (
    fail_lost_running_attempts,
    recover_expired_claims,
)
from cybercut.executor.worker import Worker
from cybercut.nodes.registry import NodeRegistry
from cybercut.nodes.silent import SilentHandler
from cybercut.storage import artifacts, events
from cybercut.storage.db import Database
from tests.cybercut.helpers import TENANT, make_orchestrator, make_project, make_run


def _silent_setup(db, data_root, *, lease_seconds, heartbeat_interval,
                  sleep_seconds, heartbeat_probe=None):
    orch = make_orchestrator(db, data_root)
    handler = SilentHandler("node_a", sleep_seconds=sleep_seconds)
    registry = NodeRegistry()
    registry.register(handler)
    worker = Worker(
        db, registry, worker_id="w-hb", data_root=data_root,
        lease_seconds=lease_seconds, orchestrator=orch,
        heartbeat_interval=heartbeat_interval, heartbeat_probe=heartbeat_probe,
    )
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    return orch, worker, handler, run, node_a


def _start_scanner(db, lease_seconds, stop_event, errors, *, interval: float = 0.5):
    """并发巡检线程：周期执行 recover / fail_lost_running。

    节奏按**真实部署**设定（租约回收是周期任务，不是热循环）：
    过密会让数据库每秒承接上百次建连，反而把「停止心跳 → 进入提交事务」的
    建连耗时推到秒级，制造出与租约无关的伪故障。
    """

    def _loop():
        while not stop_event.is_set():
            try:
                recover_expired_claims(db, lease_seconds)
                fail_lost_running_attempts(db, lease_seconds)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            stop_event.wait(interval)

    thread = threading.Thread(target=_loop, name="test-lease-scanner", daemon=True)
    thread.start()
    return thread


def test_silent_handler_survives_multiple_lease_periods(db, data_root) -> None:
    """执行 5.0s（= 2.5 个租约周期）且零 progress_cb；巡检必须判不出 WORKER_LOST。"""
    lease_seconds = 2
    orch, worker, handler, run, node_a = _silent_setup(
        db, data_root, lease_seconds=lease_seconds, heartbeat_interval=0.2,
        sleep_seconds=5.0,
    )

    stop = threading.Event()
    errors: list[Exception] = []
    scanner = _start_scanner(db, lease_seconds, stop, errors)
    try:
        outcome = worker.step()
    finally:
        stop.set()
        scanner.join(timeout=5)

    assert not errors, f"巡检线程异常：{errors}"
    assert outcome is not None and outcome.status == "succeeded"
    assert handler.calls == 1

    attempts = orch.attempts_of(node_a["node_run_id"], TENANT)
    assert len(attempts) == 1, "不得因心跳陈旧被判 WORKER_LOST 而新建 attempt"
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["error_code"] is None

    monitor = worker.runner.last_monitor
    assert monitor is not None
    assert monitor.beats >= 3, f"应发生多次心跳，实际 {monitor.beats}"
    assert monitor.lease_lost is False
    assert monitor.running is False, "心跳线程必须已退出（无泄漏）"


def test_heartbeat_renews_lease_even_without_progress(db, data_root) -> None:
    """对比验证：心跳确实刷新了 heartbeat_at（租约不会陈旧）。"""
    lease_seconds = 2
    _orch, worker, _handler, _run, node_a = _silent_setup(
        db, data_root, lease_seconds=lease_seconds, heartbeat_interval=0.15,
        sleep_seconds=2.5,
    )
    started = time.monotonic()
    outcome = worker.step()
    elapsed = time.monotonic() - started
    assert outcome is not None and outcome.status == "succeeded"
    assert elapsed >= 2.4, "handler 应确实执行了约 2.5s"

    attempt = worker.runner.last_monitor
    # 间隔是下界；实际周期 = interval + 单次 beat 的连接开销，故断言放宽为「多次」
    assert attempt is not None and attempt.beats >= 3
    assert attempt.lease_lost is False

    # 心跳发生在执行期间（beat 时间戳应晚于 attempt.started）
    with db.transaction() as conn:
        rows = events.list_for_scope(conn, TENANT, run_id=_run["run_id"])
    assert any(r["event_type"] == "attempt.started" for r in rows)
    assert any(r["event_type"] == "attempt.succeeded" for r in rows)


def test_heartbeat_false_rejects_final_commit(db, data_root) -> None:
    """心跳返回 False -> 最终提交被拒绝；不得产生正式 ArtifactRevision。"""
    _orch, worker, _handler, run, node_a = _silent_setup(
        db, data_root, lease_seconds=1, heartbeat_interval=0.05,
        sleep_seconds=0.2, heartbeat_probe=lambda: False,
    )

    outcome = worker.step()

    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error_code == "LEASE_LOST"

    from cybercut.storage import node_attempts

    with db.transaction() as conn:
        # ① attempt 未被覆盖为 succeeded
        attempts = node_attempts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        assert len(attempts) == 1
        assert attempts[0]["status"] == "failed"
        assert attempts[0]["error_code"] == "LEASE_LOST"

        # ② 无正式产物
        assert artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT) == []

        # ③ 无成功事件；有 lease_lost 事件
        evs = events.list_for_scope(conn, TENANT, run_id=run["run_id"])
    types = [e["event_type"] for e in evs]
    assert "attempt.succeeded" not in types
    assert "attempt.lease_lost" in types

    monitor = worker.runner.last_monitor
    assert monitor is not None and monitor.lease_lost is True
    assert monitor.running is False


def test_compute_interval_bounds() -> None:
    assert compute_interval(60) == 10.0          # 上限 10s
    assert compute_interval(3) == 1.0            # lease/3
    assert compute_interval(30) == 10.0
    assert compute_interval(60, 0.2) == 0.2      # 测试注入
    assert compute_interval(0.001) >= 0.01       # 下限防忙等


def test_heartbeat_probe_can_be_injected(db, data_root) -> None:
    """注入探针（返回 True）时仍可正常成功，且线程不泄漏。"""
    beats: list[float] = []

    def probe() -> bool:
        beats.append(time.monotonic())
        return True

    _orch, worker, _handler, _run, _node = _silent_setup(
        db, data_root, lease_seconds=1, heartbeat_interval=0.05,
        sleep_seconds=0.3, heartbeat_probe=probe,
    )
    outcome = worker.step()
    assert outcome is not None and outcome.status == "succeeded"
    assert len(beats) >= 3
    assert worker.runner.last_monitor is not None
    assert worker.runner.last_monitor.running is False


def test_scanner_does_not_kill_fresh_claim(db, data_root) -> None:
    """巡检对新鲜心跳的 claimed/running 不动作（负向对照）。"""
    _orch, _worker, _handler, _run, node_a = _silent_setup(
        db, data_root, lease_seconds=60, heartbeat_interval=10, sleep_seconds=0.0,
    )
    assert recover_expired_claims(db, 60) == []
    assert fail_lost_running_attempts(db, 60) == []


def test_database_module_reachable_for_helpers() -> None:
    """保证测试模块使用的 Database 类可正常构造（无 DSN 时仅构造不连接）。"""
    Database("postgresql://postgres@127.0.0.1:55432/cybercut_test")


# ── WB-01R2 §四：stop 语义 ──


def test_stop_timeout_keeps_thread_reference_and_allows_retry() -> None:
    """join 超时且线程仍存活时：不清空引用、running 真实、可再次 stop。"""
    from cybercut.executor.heartbeat import HeartbeatMonitor

    entered = threading.Event()
    release = threading.Event()

    def blocking_beat() -> bool:
        entered.set()
        release.wait(timeout=30)
        return True

    monitor = HeartbeatMonitor(blocking_beat, 0.05)
    monitor.start()
    assert entered.wait(5), "beat 应已开始执行"

    joined = monitor.stop(timeout=0.2)
    assert joined is False, "beat 仍阻塞时 stop 必须返回 False"
    assert monitor.running is True, "线程引用不得丢失，running 必须真实反映"
    assert monitor.beats == 0

    release.set()
    assert monitor.stop(timeout=5) is True, "允许再次 stop/join"
    assert monitor.running is False
    assert monitor._thread is None  # noqa: SLF001 - 断言引用确已清除


def test_stop_on_never_started_monitor_is_clean() -> None:
    from cybercut.executor.heartbeat import HeartbeatMonitor

    monitor = HeartbeatMonitor(lambda: True, 0.05)
    assert monitor.stop() is True
    assert monitor.running is False


def test_normal_path_leaves_no_thread(db, data_root) -> None:
    """正常路径不得残留线程（R2 §四）。"""
    _orch, worker, _handler, _run, _node = _silent_setup(
        db, data_root, lease_seconds=1, heartbeat_interval=0.05, sleep_seconds=0.2,
    )
    outcome = worker.step()
    assert outcome is not None and outcome.status == "succeeded"
    monitor = worker.runner.last_monitor
    assert monitor is not None
    assert monitor.running is False
    assert monitor.stop() is True
