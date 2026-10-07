"""WB-01R §二.4：并发幂等测试（多连接 + Barrier 同步起跑，真实 PostgreSQL）。"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from cybercut.contracts.errors import IdempotencyConflictError
from cybercut.orchestrator.engine import Orchestrator
from cybercut.storage import events, projects
from cybercut.storage.db import Database
from tests.cybercut.helpers import TENANT

N = 8


def _race(fn, n: int = N, timeout: float = 60.0) -> tuple[list[Any], list[BaseException]]:
    """用 Barrier 同步起跑 n 个线程（各自独立连接），不使用 sleep 碰运气。"""
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=timeout)
            results[idx] = fn(idx)
        except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(i,), name=f"idem-{i}") for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    return results, errors


def test_concurrent_same_key_same_payload_creates_one_project(db) -> None:
    """8 并发同 key 同 payload -> 只创建一个 Project，其余规范重放。"""
    key = f"key-{uuid.uuid4().hex}"

    def call(_idx: int):
        orch = Orchestrator(Database(db.dsn))
        return orch.create_project(
            tenant_id=TENANT, name="并发同名", goal={"v": 1}, idempotency_key=key
        )

    results, errors = _race(call)

    assert errors == [], f"不应出现异常：{errors}"
    project_ids = {r[0]["project_id"] for r in results}
    assert len(project_ids) == 1, f"应只创建一个项目，实际 {len(project_ids)} 个"
    created = [r for r in results if r[1] is False]
    replayed = [r for r in results if r[1] is True]
    assert len(created) == 1, "应恰好一个请求真正创建"
    assert len(replayed) == N - 1, "其余必须是规范重放"

    with db.transaction() as conn:
        rows = projects.list_for_tenant(conn, TENANT)
    assert len(rows) == 1, "数据库中不得出现重复 Project"

    # 事件也不得重复
    with db.transaction() as conn:
        evs = events.list_for_scope(conn, TENANT, project_id=rows[0]["project_id"])
    assert [e["event_type"] for e in evs].count("project.created") == 1


def test_concurrent_same_key_different_payload_one_winner(db) -> None:
    """8 并发同 key 不同 payload -> 只允许一个获胜，其余 IdempotencyConflictError。"""
    key = f"key-{uuid.uuid4().hex}"

    def call(idx: int):
        orch = Orchestrator(Database(db.dsn))
        return orch.create_project(
            tenant_id=TENANT, name=f"并发不同-{idx}", idempotency_key=key
        )

    results, errors = _race(call)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"应恰好一个获胜，实际 {len(winners)}"
    assert len(errors) == N - 1
    for exc in errors:
        assert isinstance(exc, IdempotencyConflictError), (
            f"必须抛领域异常而非驱动异常：{type(exc).__name__}: {exc}"
        )

    with db.transaction() as conn:
        rows = projects.list_for_tenant(conn, TENANT)
    assert len(rows) == 1, "冲突请求不得落库"


def test_replay_does_not_raise_driver_exception(db) -> None:
    """重放路径不得把 psycopg2 异常泄漏给调用方。"""
    key = f"key-{uuid.uuid4().hex}"
    orch = Orchestrator(db)
    first, _ = orch.create_project(tenant_id=TENANT, name="x", idempotency_key=key)
    second, replayed = orch.create_project(
        tenant_id=TENANT, name="x", idempotency_key=key
    )
    assert replayed is True
    assert second["project_id"] == first["project_id"]


def test_concurrent_replay_after_commit_is_stable(db) -> None:
    """先串行提交一次，再并发 8 次同键 —— 全部应为重放。"""
    key = f"key-{uuid.uuid4().hex}"
    orch = Orchestrator(db)
    created, _ = orch.create_project(
        tenant_id=TENANT, name="stable", idempotency_key=key
    )

    def call(_idx: int):
        return Orchestrator(Database(db.dsn)).create_project(
            tenant_id=TENANT, name="stable", idempotency_key=key
        )

    results, errors = _race(call)
    assert errors == []
    assert all(r[1] is True for r in results)
    assert {r[0]["project_id"] for r in results} == {created["project_id"]}
