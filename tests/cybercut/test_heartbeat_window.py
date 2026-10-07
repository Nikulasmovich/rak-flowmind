"""WB-01R2 §二：心跳窗口必须覆盖「校验 + 哈希」阶段（真实 PostgreSQL）。

场景：handler 很快返回，但 monkeypatch 的哈希过程持续超过多个租约周期；
并发运行 lease scanner；attempt 不得被判 WORKER_LOST，且哈希期间观察到多次心跳。
"""
from __future__ import annotations

import threading
import time

from cybercut.executor.lease import (
    fail_lost_running_attempts,
    recover_expired_claims,
)
from cybercut.executor.worker import Worker
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from tests.cybercut.helpers import TENANT, make_orchestrator, make_project, make_run

ONE_NODE = PipelineSpec(
    name="one-node", version="one-node-v1", nodes=(NodeSpec("node_a"),)
)

HASH_WINDOW_SECONDS = 1.4
LEASE_SECONDS = 2


def _start_scanner(db, stop_event, errors, *, interval: float = 0.5):
    """周期巡检（真实部署节奏；热循环建连会污染测量）。"""

    def _loop():
        while not stop_event.is_set():
            try:
                recover_expired_claims(db, LEASE_SECONDS)
                fail_lost_running_attempts(db, LEASE_SECONDS)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            stop_event.wait(interval)

    thread = threading.Thread(target=_loop, name="hash-lease-scanner", daemon=True)
    thread.start()
    return thread


def test_heartbeat_covers_hashing_window(db, data_root) -> None:
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))

    state: dict = {"entry": None, "exit": None, "calls": 0, "worker": None}

    def hash_hook(_relative_path: str) -> None:
        worker = state["worker"]
        monitor = worker.runner.last_monitor
        if monitor is None:  # pragma: no cover - 心跳必先启动
            return
        state["calls"] += 1
        if state["entry"] is None:
            state["entry"] = monitor.beats
        time.sleep(HASH_WINDOW_SECONDS)
        state["exit"] = monitor.beats

    worker = Worker(
        db, registry, worker_id="w-hash", data_root=data_root,
        lease_seconds=LEASE_SECONDS, heartbeat_interval=0.15, orchestrator=orch,
        hash_hook=hash_hook,
    )
    state["worker"] = worker

    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]

    stop = threading.Event()
    errors: list[Exception] = []
    scanner = _start_scanner(db, stop, errors)
    try:
        outcome = worker.step()
    finally:
        stop.set()
        scanner.join(timeout=5)

    assert not errors, f"巡检线程异常：{errors}"
    assert outcome is not None and outcome.status == "succeeded"

    # 哈希阶段被心跳覆盖：入口到出口之间观察到多次心跳
    assert state["calls"] == 2, "哈希钩子应对单个产物调用两次（前/后）"
    assert state["entry"] is not None and state["exit"] is not None
    assert state["exit"] - state["entry"] >= 2, (
        f"哈希期间应发生多次心跳，实际 {state['exit'] - state['entry']}"
    )

    # 巡检不得把该 attempt 判死
    attempts = orch.attempts_of(node_a["node_run_id"], TENANT)
    assert len(attempts) == 1, "不得因心跳陈旧新建 attempt"
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["error_code"] is None
    assert worker.runner.last_monitor is not None
    assert worker.runner.last_monitor.running is False


def test_hashed_file_registered_with_original_digest(db, data_root) -> None:
    """哈希窗口被拉长不影响产物登记内容（sha256/大小正确）。"""
    import hashlib

    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))

    sleep_once = {"done": False}

    def hash_hook(_relative_path: str) -> None:
        if not sleep_once["done"]:
            sleep_once["done"] = True
            time.sleep(0.3)

    worker = Worker(db, registry, worker_id="w-hash2", data_root=data_root,
                    lease_seconds=5, heartbeat_interval=0.2, orchestrator=orch,
                    hash_hook=hash_hook)
    project = make_project(orch)
    run = make_run(orch, project=project)
    assert worker.step() is not None

    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    from cybercut.storage import artifacts

    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
    assert len(rows) == 1
    project_root = data_root / "projects" / project["project_id"]
    raw = (project_root / rows[0]["relative_path"]).read_bytes()
    assert rows[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert rows[0]["size_bytes"] == len(raw)
