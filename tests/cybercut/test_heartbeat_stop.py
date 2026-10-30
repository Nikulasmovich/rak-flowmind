"""WB-01R3 §五：心跳停止失败的 **AttemptRunner 集成**处理（真实 PostgreSQL）。

覆盖：
1. 阻塞 beat -> 首次 join 失败 -> 中断有界 I/O -> 二次 join 成功 -> 无线程泄漏；
2. 心跳线程仍存活时**不得** close 其资源；
3. 真正无法停止 -> `HeartbeatStuckError`（进程级致命），且 worker 被 poisoned、
   不再领取新任务（不是伪装成普通 attempt 失败后继续）。
"""
from __future__ import annotations

import threading

import pytest

from cybercut.contracts.errors import HeartbeatStuckError
from cybercut.executor.worker import Worker
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from cybercut.storage import node_attempts
from tests.cybercut.helpers import TENANT, make_orchestrator, make_project, make_run

ONE_NODE = PipelineSpec(
    name="one-node", version="one-node-v1", nodes=(NodeSpec("node_a"),)
)


class _BlockingProbe:
    """可中断/不可中断的阻塞 beat（模拟有界 I/O 卡住）。"""

    def __init__(self, *, releasable: bool) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.releasable = releasable
        self.closed = False
        self.interrupted = False

    def __call__(self) -> bool:
        self.entered.set()
        self.release.wait(timeout=60)
        return True

    def interrupt(self) -> None:
        self.interrupted = True
        if self.releasable:
            self.release.set()

    def close(self) -> None:
        self.closed = True


class _SlowHandler:
    """先睡一会，让心跳线程有机会进入阻塞 beat。"""

    node_key = "node_a"

    def __init__(self, sleep_seconds: float = 0.6) -> None:
        self.sleep_seconds = sleep_seconds
        self._inner = EchoHandler("node_a")

    def execute(self, context, input_json):
        import time

        time.sleep(self.sleep_seconds)
        return self._inner.execute(context, input_json)


def _setup(db, data_root, *, probe, stop_timeout=0.2):
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    registry = NodeRegistry()
    registry.register(_SlowHandler())
    worker = Worker(
        db, registry, worker_id="w-hbstop", data_root=data_root, lease_seconds=30,
        heartbeat_interval=0.05, heartbeat_probe=probe,
        heartbeat_stop_timeout=stop_timeout, orchestrator=orch,
    )
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    return orch, worker, project, run, node_a


def test_runner_interrupts_blocking_beat_and_stops_cleanly(db, data_root) -> None:
    probe = _BlockingProbe(releasable=True)
    _orch, worker, _project, _run, _node_a = _setup(db, data_root, probe=probe)

    outcome = worker.step()

    assert probe.entered.is_set(), "心跳线程应已进入阻塞 beat"
    assert probe.interrupted is True, "首次 join 失败后必须中断有界 I/O"
    assert probe.closed is True, "线程退出后应释放心跳资源"
    assert outcome is not None and outcome.status == "succeeded"
    monitor = worker.runner.last_monitor
    assert monitor is not None
    assert monitor.running is False, "不得残留心跳线程"
    assert monitor.stop() is True


def test_runner_does_not_close_resources_while_thread_alive(db, data_root) -> None:
    """中断无效（线程仍存活）时不得 close，且必须抛进程级致命错误。"""
    probe = _BlockingProbe(releasable=False)
    _orch, worker, _project, _run, _node_a = _setup(db, data_root, probe=probe)

    try:
        with pytest.raises(HeartbeatStuckError):
            worker.step()
        assert probe.interrupted is True
        assert probe.closed is False, "线程仍存活时绝不释放其资源"
        assert worker.poisoned is not None

        # 已被 poison：不得再领取新任务
        with pytest.raises(HeartbeatStuckError):
            worker.step()
    finally:
        probe.release.set()  # 让守护线程得以退出，避免测试进程残留
        monitor = worker.runner.last_monitor
        if monitor is not None:
            monitor.stop(timeout=5)


def test_fatal_heartbeat_does_not_leave_attempt_succeeded(db, data_root) -> None:
    """致命心跳错误不得把 attempt 伪造成成功。"""
    probe = _BlockingProbe(releasable=False)
    orch, worker, _project, _run, node_a = _setup(db, data_root, probe=probe)
    try:
        with pytest.raises(HeartbeatStuckError):
            worker.step()
    finally:
        probe.release.set()
        monitor = worker.runner.last_monitor
        if monitor is not None:
            monitor.stop(timeout=5)

    with db.transaction() as conn:
        attempts = node_attempts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
    assert len(attempts) == 1
    assert attempts[0]["status"] in {"running", "failed"}
    assert attempts[0]["status"] != "succeeded"
