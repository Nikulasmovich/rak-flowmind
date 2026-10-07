"""WB-01R2 §三：统一锁顺序与并发「提交 vs 取消」无死锁（真实 PostgreSQL）。

锁顺序（唯一约定）：`run -> node_run -> node_attempt -> project`。
"""
from __future__ import annotations

import threading
import time

import psycopg2
import pytest

import cybercut.executor.runner as runner_mod
from cybercut.contracts.errors import CyberCutError
from cybercut.executor.worker import Worker
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from cybercut.storage import approvals, artifacts, events, node_attempts
from tests.cybercut.helpers import TENANT, make_orchestrator, make_project, make_run

ONE_NODE = PipelineSpec(
    name="one-node", version="one-node-v1", nodes=(NodeSpec("node_a"),)
)


class _ThenCancel:
    """handler 执行后取消自身 attempt（模拟提交事务开始前的并发取消）。"""

    def __init__(self, inner, mutate) -> None:
        self._inner = inner
        self._mutate = mutate

    @property
    def node_key(self) -> str:
        return self._inner.node_key

    def execute(self, context, input_json):
        result = self._inner.execute(context, input_json)
        self._mutate()
        return result


def _setup(db, data_root, registry, *, worker_id="w-lock"):
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker = Worker(db, registry, worker_id=worker_id, data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]
    return orch, worker, run, node_a, attempt


def test_concurrent_commit_and_cancel_locks_overlap_without_deadlock(
    db, data_root, monkeypatch
) -> None:
    """提交事务持锁期间并发取消同一 attempt：锁真实重叠、无死锁、终态完整。"""
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    orch, worker, run, node_a, attempt = _setup(db, data_root, registry)

    started = threading.Event()
    proceed = threading.Event()
    real_register = runner_mod.artifacts_repo.register

    def slow_register(conn, **kwargs):
        # 此时提交事务已持有 run / node_run / node_attempt 行锁
        started.set()
        proceed.wait(timeout=15)
        return real_register(conn, **kwargs)

    monkeypatch.setattr(runner_mod.artifacts_repo, "register", slow_register)

    commit_box: dict = {}

    def commit_worker() -> None:
        try:
            commit_box["outcome"] = worker.step()
        except BaseException as exc:  # noqa: BLE001
            commit_box["error"] = exc

    cancel_box: dict = {}

    def canceller() -> None:
        try:
            cancel_box["result"] = orch.cancel_attempt(attempt["attempt_id"], TENANT)
        except BaseException as exc:  # noqa: BLE001
            cancel_box["error"] = exc

    t_commit = threading.Thread(target=commit_worker, name="commit")
    t_commit.start()
    assert started.wait(15), "提交事务应已开始并持锁"

    barrier = threading.Barrier(1)
    t_cancel = threading.Thread(target=canceller, name="cancel")
    t_cancel.start()
    barrier.wait(timeout=1)
    time.sleep(0.4)
    assert t_cancel.is_alive(), "取消线程应被行锁阻塞 —— 证明锁真实重叠"

    proceed.set()
    t_commit.join(timeout=30)
    t_cancel.join(timeout=30)
    assert not t_commit.is_alive() and not t_cancel.is_alive()

    # ── 不得出现裸 psycopg2 异常 / 死锁 ──
    for box in (commit_box, cancel_box):
        err = box.get("error")
        if err is not None:
            assert not isinstance(err, psycopg2.Error), f"不得外泄 psycopg2 异常：{err!r}"
            assert isinstance(err, CyberCutError), f"应为领域异常：{type(err).__name__}"
    assert "error" not in commit_box, f"提交不得失败：{commit_box.get('error')}"

    outcome = commit_box["outcome"]
    assert outcome is not None and outcome.status == "succeeded"

    # ── 终态必须「完整 succeeded」，不得出现部分产物/审批/事件 ──
    with db.transaction() as conn:
        arts = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        atts = node_attempts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        apps = approvals.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        evs = events.list_for_scope(conn, TENANT, run_id=run["run_id"])
    assert len(arts) == 1, "成功路径必须恰好登记 1 条产物"
    assert len(atts) == 1
    assert atts[0]["status"] == "succeeded"
    assert apps == []
    assert "attempt.succeeded" in [e["event_type"] for e in evs]
    # 取消尝试未产生任何副作用（attempt 已 succeeded，取消被领域异常拒绝）
    assert "result" not in cancel_box
    assert isinstance(cancel_box.get("error"), CyberCutError)


def test_cancel_before_commit_leaves_no_partial_state(db, data_root) -> None:
    """取消先发生（提交事务开始前）：终态为「完整 cancelled」，无部分产物/审批/事件。"""
    holder: dict = {}

    def mutate() -> None:
        with db.transaction() as conn:
            node_attempts.cancel(conn, holder["attempt_id"], TENANT)

    registry = NodeRegistry()
    registry.register(_ThenCancel(EchoHandler("node_a"), mutate))
    orch, worker, run, node_a, attempt = _setup(db, data_root, registry)
    holder["attempt_id"] = attempt["attempt_id"]

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    with db.transaction() as conn:
        arts = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        atts = node_attempts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        apps = approvals.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        evs = events.list_for_scope(conn, TENANT, run_id=run["run_id"])
    assert arts == [], "取消路径不得留下产物"
    assert apps == [], "取消路径不得留下审批"
    assert atts[0]["status"] == "cancelled"
    assert "attempt.succeeded" not in [e["event_type"] for e in evs]


@pytest.mark.parametrize("repeat", [1, 2])
def test_repeated_commit_cancel_race_is_stable(db, data_root, repeat) -> None:
    """重复运行并发竞争（不同 attempt）保持稳定，无死锁/裸异常。"""
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    orch, _worker, run, node_a, attempt = _setup(
        db, data_root, registry, worker_id=f"w-lock-{repeat}"
    )

    errors: list[BaseException] = []

    def commit_and_cancel() -> None:
        try:
            Worker(db, registry, worker_id=f"w-{repeat}-c", data_root=data_root,
                   lease_seconds=30, orchestrator=orch).step()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        try:
            orch.cancel_attempt(attempt["attempt_id"], TENANT)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=commit_and_cancel, name=f"race-{repeat}-{i}")
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    for exc in errors:
        assert not isinstance(exc, psycopg2.Error), f"不得外泄 psycopg2 异常：{exc!r}"

    with db.transaction() as conn:
        arts = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        atts = node_attempts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
        evs = events.list_for_scope(conn, TENANT, run_id=run["run_id"])
    types = [e["event_type"] for e in evs]
    if atts[0]["status"] == "succeeded":
        assert len(arts) == 1
        assert "attempt.succeeded" in types
    else:
        assert arts == []
        assert "attempt.succeeded" not in types
