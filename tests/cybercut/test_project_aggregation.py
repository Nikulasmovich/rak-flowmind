"""WB-03R2-复审 · 多 Run Project 状态聚合（真实 PostgreSQL）。

语义（ADR-0004 决策 34）：
- **Project 状态由项目下全部 Run 聚合得出，与 `current_run_id` 无关**；
- 只要存在任一非终态 Run，项目就不得进入 `completed` / `failed` / `cancelled`；
- 全部 Run 终态时：任一 `failed` ⇒ `failed`；否则任一 `cancelled` ⇒ `cancelled`；
  否则（全部 `succeeded`）⇒ `completed`；
- 若项目曾被误标为某终态而聚合结果不同，必须做**受控修正**，
  不得保持错误的 `completed` 状态。
"""
from __future__ import annotations

import pytest

from cybercut.contracts.enums import NodeRunStatus, ProjectStatus, RunStatus
from cybercut.contracts.errors import ValidationError
from cybercut.nodes.registry import build_test_registry
from cybercut.orchestrator.engine import Orchestrator
from cybercut.orchestrator.pipeline import TEST_PIPELINE
from cybercut.storage import projects, runs
from tests.cybercut.helpers import TENANT


@pytest.fixture
def orch(db):
    return Orchestrator(db, pipeline=TEST_PIPELINE)


@pytest.fixture
def seeded(db):
    """建项目（active）并返回 (project_id, Orchestrator, Worker)。"""
    from cybercut.executor.worker import Worker

    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="agg")
    orch = Orchestrator(db, pipeline=TEST_PIPELINE)
    worker = Worker(db, build_test_registry(), worker_id="w-agg",
                    data_root=None, lease_seconds=30, orchestrator=orch)
    return project["project_id"], orch, worker


def _force_succeed_all(db, run_id: str) -> None:
    """把 Run 的全部 node_run 经合法链推进到 `succeeded`（聚合测试用）。"""
    from cybercut.storage import node_runs as node_runs_repo

    with db.transaction() as conn:
        for nr in node_runs_repo.list_for_run(conn, run_id, TENANT):
            nid = nr["node_run_id"]
            status = nr["status"]
            if status == NodeRunStatus.BLOCKED.value:
                node_runs_repo.set_status(conn, nid, TENANT,
                                          to_status=NodeRunStatus.READY)
                status = NodeRunStatus.READY.value
            if status == NodeRunStatus.READY.value:
                node_runs_repo.set_status(conn, nid, TENANT,
                                          to_status=NodeRunStatus.RUNNING)
            node_runs_repo.set_status(conn, nid, TENANT,
                                      to_status=NodeRunStatus.SUCCEEDED)


def _force_fail_one(db, run_id: str) -> str:
    """把 Run 的第一个可推进 node_run 推进到 `failed`，返回其 node_run_id。"""
    from cybercut.storage import node_runs as node_runs_repo

    with db.transaction() as conn:
        for nr in node_runs_repo.list_for_run(conn, run_id, TENANT):
            if nr["status"] in (NodeRunStatus.READY.value,
                                NodeRunStatus.RUNNING.value,
                                NodeRunStatus.BLOCKED.value):
                nid = nr["node_run_id"]
                status = nr["status"]
                if status == NodeRunStatus.BLOCKED.value:
                    node_runs_repo.set_status(conn, nid, TENANT,
                                              to_status=NodeRunStatus.READY)
                if status != NodeRunStatus.RUNNING.value:
                    node_runs_repo.set_status(conn, nid, TENANT,
                                              to_status=NodeRunStatus.RUNNING)
                node_runs_repo.set_status(conn, nid, TENANT,
                                          to_status=NodeRunStatus.FAILED)
                return nid
    raise AssertionError("没有可推进到 failed 的 node_run")


def _project_status(db, project_id: str) -> str:
    with db.transaction() as conn:
        return projects.get(conn, project_id, TENANT)["status"]


def _run_status(db, run_id: str) -> str:
    with db.transaction() as conn:
        return runs.get(conn, run_id, TENANT)["status"]


def _queued_run(db, project_id: str) -> str:
    """直接登记一条**尚未启动**（queued）的 Run（无 node_run）。"""
    with db.transaction() as conn:
        run = runs.create(conn, project_id=project_id, tenant_id=TENANT,
                          pipeline_version="test-pipeline-v1",
                          status=RunStatus.QUEUED)
    return run["run_id"]


# ══════════════════════════════════════════════════════════════════
# 1) 旧 Run 先完成、新 Run 仍 queued / running ⇒ Project 必须保持 active
# ══════════════════════════════════════════════════════════════════


def test_older_run_completes_project_stays_active_when_new_run_queued(db, seeded) -> None:
    """旧 Run 完成、新 Run 仍 queued ⇒ 项目不得被标为 completed。"""
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = _queued_run(db, pid)

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)

    assert _run_status(db, run_a["run_id"]) == RunStatus.SUCCEEDED.value
    assert _run_status(db, run_b) == RunStatus.QUEUED.value
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value, (
        "仍有活动 Run（queued）时项目不得进入终态")


def test_older_run_completes_project_stays_active_when_new_run_running(
        db, seeded) -> None:
    """旧 Run 完成、新 Run 仍 running ⇒ 项目保持 active。"""
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)
    assert _run_status(db, run_b["run_id"]) == RunStatus.RUNNING.value

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)

    assert _run_status(db, run_a["run_id"]) == RunStatus.SUCCEEDED.value
    assert _run_status(db, run_b["run_id"]) == RunStatus.RUNNING.value
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value


def test_cancelled_older_run_does_not_terminalize_project(db, seeded) -> None:
    """旧 Run 被取消、新 Run 仍 running ⇒ 项目保持 active。"""
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)

    orch.request_run_cancel(run_a["run_id"], TENANT)

    assert _run_status(db, run_a["run_id"]) == RunStatus.CANCELLED.value
    assert _run_status(db, run_b["run_id"]) == RunStatus.RUNNING.value
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value


# ══════════════════════════════════════════════════════════════════
# 2) 新 Run 的两种终态 ⇒ 项目终态正确
# ══════════════════════════════════════════════════════════════════


def test_new_run_success_marks_project_completed(db, seeded) -> None:
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value

    _force_succeed_all(db, run_b["run_id"])
    orch.advance_run(run_b["run_id"], TENANT)

    assert _run_status(db, run_b["run_id"]) == RunStatus.SUCCEEDED.value
    assert _project_status(db, pid) == ProjectStatus.COMPLETED.value


def test_new_run_failure_marks_project_failed(db, seeded) -> None:
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value

    _force_fail_one(db, run_b["run_id"])
    orch.advance_run(run_b["run_id"], TENANT)

    assert _run_status(db, run_b["run_id"]) == RunStatus.FAILED.value
    assert _project_status(db, pid) == ProjectStatus.FAILED.value


def test_mixed_terminal_states_prefer_failed(db, seeded) -> None:
    """全部终态且混合 succeeded / failed ⇒ 项目取 failed（失败优先）。"""
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_c = orch.start_run(project_id=pid, tenant_id=TENANT)

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)
    _force_fail_one(db, run_b["run_id"])
    orch.advance_run(run_b["run_id"], TENANT)
    _force_succeed_all(db, run_c["run_id"])
    orch.advance_run(run_c["run_id"], TENANT)

    assert _run_status(db, run_a["run_id"]) == RunStatus.SUCCEEDED.value
    assert _run_status(db, run_b["run_id"]) == RunStatus.FAILED.value
    assert _run_status(db, run_c["run_id"]) == RunStatus.SUCCEEDED.value
    assert _project_status(db, pid) == ProjectStatus.FAILED.value


# ══════════════════════════════════════════════════════════════════
# 3) 错误的 completed 状态必须被修正
# ══════════════════════════════════════════════════════════════════


def test_stale_completed_is_corrected_when_current_run_fails(db, seeded) -> None:
    """当前 Run 失败时，不得让 Project 保持错误的 completed 状态。"""
    from cybercut.contracts.errors import ValidationError

    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value

    # 复现修复前的历史错误状态：项目被（错误的早期完成逻辑）标记为 completed
    with pytest.raises(ValidationError):
        with db.transaction() as conn:
            projects.apply_aggregate_status(
                conn, pid, TENANT, to_status=ProjectStatus.ACTIVE,
                reason="非终态目标必须被拒绝")
    with db.transaction() as conn:
        projects.apply_aggregate_status(
            conn, pid, TENANT, to_status=ProjectStatus.COMPLETED,
            reason="simulate-stale-completed")
    assert _project_status(db, pid) == ProjectStatus.COMPLETED.value

    # 当前 Run 失败 ⇒ 聚合必须把项目修正为 failed
    _force_fail_one(db, run_b["run_id"])
    orch.advance_run(run_b["run_id"], TENANT)

    assert _run_status(db, run_b["run_id"]) == RunStatus.FAILED.value
    assert _project_status(db, pid) == ProjectStatus.FAILED.value, (
        "不得保持错误的 completed 状态")


def test_aggregate_status_is_idempotent_and_validated(db) -> None:
    """聚合路径：幂等；非终态目标被拒绝。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="agg-guard")
    pid = project["project_id"]

    with pytest.raises(ValidationError):
        with db.transaction() as conn:
            projects.apply_aggregate_status(
                conn, pid, TENANT, to_status=ProjectStatus.ACTIVE, reason="x")

    with db.transaction() as conn:
        projects.apply_aggregate_status(
            conn, pid, TENANT, to_status=ProjectStatus.FAILED,
            reason="first")
    with db.transaction() as conn:
        projects.apply_aggregate_status(
            conn, pid, TENANT, to_status=ProjectStatus.FAILED,
            reason="idempotent")
    assert _project_status(db, pid) == ProjectStatus.FAILED.value


# ══════════════════════════════════════════════════════════════════
# 4) `current_run_id` 只是「最近启动 Run」指针，不决定项目状态
# ══════════════════════════════════════════════════════════════════


def test_current_run_id_is_a_pointer_not_the_status_source(db, seeded) -> None:
    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    run_b = orch.start_run(project_id=pid, tenant_id=TENANT)

    with db.transaction() as conn:
        assert projects.get(conn, pid, TENANT)["current_run_id"] == \
            run_b["run_id"]

    _force_succeed_all(db, run_a["run_id"])
    orch.advance_run(run_a["run_id"], TENANT)

    with db.transaction() as conn:
        row = projects.get(conn, pid, TENANT)
    # current_run_id 仍指向最近启动的 Run（指针语义）
    assert row["current_run_id"] == run_b["run_id"]
    # 但项目状态由聚合决定：仍有活动 Run ⇒ 保持 active
    assert row["status"] == ProjectStatus.ACTIVE.value


# ══════════════════════════════════════════════════════════════════
# 5) 竞态回归：「Run 状态快照 → 新 Run 提交 → 聚合落终态」
# ══════════════════════════════════════════════════════════════════

_RUN_ACTIVE = frozenset({
    RunStatus.QUEUED.value, RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value, RunStatus.CANCELLING.value,
})


def test_new_run_commit_between_snapshot_and_terminal_write_keeps_project_active(
        db, seeded, monkeypatch) -> None:
    """稳定复现「Run 状态快照 → 新 Run 提交 → 聚合落终态」竞态。

    在**第一次** Run 状态快照（此时快照显示全部 Run 已终态）之后、
    聚合落终态之前，从独立事务提交一个新 Run（复现竞态窗口），
    并断言：

    1. 聚合在 **Project 锁**下**重新读取**了 Run 状态（快照至少读取两次）；
    2. 重读发现了新提交的活动 Run ⇒ Project 保持 `active`（不落终态）；
    3. 顺序为「快照 → 新 Run 提交 → Project 锁 → 重读」；
    4. 新 Run 仍然存在且可执行（未被丢弃）。

    若实现退化为「只用快照判定、不加项目锁直接落终态」，本测试必然失败。
    """
    from cybercut.orchestrator import engine as engine_module

    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    _force_succeed_all(db, run_a["run_id"])

    original_statuses = runs.statuses_for_project
    original_project_lock = projects.get_for_update

    reads: list[list[str]] = []
    events: list[str] = []
    created: list[str] = []

    def interposed_statuses(conn, project_id, tenant_id):
        result = original_statuses(conn, project_id, tenant_id)
        reads.append(list(result))
        events.append("snapshot")
        # 复现竞态窗口：快照显示「全部 Run 已终态」且尚未注入新 Run 时，
        # 从独立事务提交一个新 Run（与 start_run 相同的创建路径）
        if not created and result and not any(s in _RUN_ACTIVE for s in result):
            run_b = orch.start_run(project_id=pid, tenant_id=TENANT)
            created.append(run_b["run_id"])
            events.append("new_run_committed")
        return result

    def interposed_project_lock(conn, project_id, tenant_id):
        events.append("project_lock")
        return original_project_lock(conn, project_id, tenant_id)

    monkeypatch.setattr(engine_module.runs, "statuses_for_project",
                        interposed_statuses)
    monkeypatch.setattr(engine_module.projects, "get_for_update",
                        interposed_project_lock)

    orch.advance_run(run_a["run_id"], TENANT)

    # ① 竞态窗口被稳定复现
    assert created, "必须复现竞态窗口：新 Run 在快照之后提交"
    all_terminal_snapshots = [i for i, r in enumerate(reads)
                              if r and not any(s in _RUN_ACTIVE for s in r)]
    assert all_terminal_snapshots, "必须出现「全部 Run 已终态」的快照"
    first_stale = all_terminal_snapshots[0]
    commit_index = events.index("new_run_committed")
    assert commit_index > first_stale, "新 Run 提交必须发生在快照之后"

    # ② 聚合在 Project 锁下重读：新 Run 提交之后仍有 Project 锁与快照
    assert events.count("project_lock") >= 2
    last_lock = max(i for i, o in enumerate(events) if o == "project_lock")
    assert last_lock > commit_index, (
        "聚合必须在拿到 Project 锁之后重读 Run 状态")

    # ③ 权威判定基于锁内重读：最后一次读取必须包含新 Run 的活动状态
    assert any(s in _RUN_ACTIVE for s in reads[-1]), (
        f"锁内重读必须看到新 Run 活动，实际 {reads[-1]}")
    assert _run_status(db, created[0]) in (
        RunStatus.QUEUED.value, RunStatus.RUNNING.value)

    # ④ 最终：Project 保持 active（不得落为 completed 等终态）
    assert _project_status(db, pid) == ProjectStatus.ACTIVE.value


def test_aggregate_takes_project_lock_before_authoritative_read(
        db, seeded, monkeypatch) -> None:
    """Project 锁必须先于权威 Run 状态读取（与 start_run 同一把锁）。"""
    from cybercut.orchestrator import engine as engine_module

    pid, orch, worker = seeded
    run_a = orch.start_run(project_id=pid, tenant_id=TENANT)
    _force_succeed_all(db, run_a["run_id"])

    order: list[str] = []
    original_statuses = runs.statuses_for_project
    original_lock = projects.get_for_update

    def interposed_statuses(conn, project_id, tenant_id):
        order.append("read")
        return original_statuses(conn, project_id, tenant_id)

    def interposed_lock(conn, project_id, tenant_id):
        order.append("lock")
        return original_lock(conn, project_id, tenant_id)

    monkeypatch.setattr(engine_module.runs, "statuses_for_project",
                        interposed_statuses)
    monkeypatch.setattr(engine_module.projects, "get_for_update", interposed_lock)

    orch.advance_run(run_a["run_id"], TENANT)

    assert "lock" in order and "read" in order
    first_read = order.index("read")
    # 聚合必须在首次读取之后**重新获取** Project 锁（两段式判定）
    locks_after_first_read = [i for i, o in enumerate(order)
                              if o == "lock" and i > first_read]
    assert locks_after_first_read, "聚合必须在首次读取之后重新获取 Project 锁"
    # 最末次（权威）Run 状态读取必须发生在 Project 锁之内
    last_read = max(i for i, o in enumerate(order) if o == "read")
    last_lock_before_last_read = max(
        i for i, o in enumerate(order[:last_read]) if o == "lock")
    assert last_lock_before_last_read < last_read, (
        "权威读取必须在 Project 锁内进行（先锁后读）")
