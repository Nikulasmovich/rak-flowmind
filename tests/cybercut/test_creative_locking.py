"""WB-03R2 · 锁序、版本守卫作用域、版本-审批状态联动与受控重开。

覆盖：
- `revise_node` 锁序统一为 Run → NodeRun → NodeAttempt → Project；
- 修订与成功提交并发时不死锁、不出现部分更新；
- `expected_revision` 以**目标 Run** 的当前版本为基准（多 Run 项目仍可修订）；
- 创作版本状态随审批联动（draft / active / superseded / rejected）；
- 终态重开仅限受控修订路径，并清理终态元数据。
"""
from __future__ import annotations

import threading

import pytest

from cybercut.contracts.enums import NodeRunStatus, ProjectStatus, RunStatus
from cybercut.contracts.errors import (
    ConflictError,
    InvalidTransitionError,
)
from cybercut.creative.policy import AUTO_POLICY, HYBRID_POLICY
from cybercut.orchestrator.pipeline import CREATIVE_PIPELINE
from cybercut.storage import creative_revisions, projects, runs
from tests.cybercut.helpers import TENANT
from tests.cybercut.test_creative_pipeline_e2e import (
    _build,
    _start,
)

KINDS = ("brief", "script", "storyboard", "match", "edit_plan", "template_bind")


# ══════════════════════════════════════════════════════════════════
# 夹具与工具
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def auto_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)


@pytest.fixture
def hybrid_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, HYBRID_POLICY)


def _rows(db, project_id: str, kind: str, *, run_id: str | None = None
          ) -> list[dict]:
    with db.transaction() as conn:
        return creative_revisions.list_for_project(
            conn, kind, project_id, TENANT, run_id=run_id)


def _node_run_id(orch, run_id: str, key: str) -> str:
    return orch.run_state(run_id, TENANT)["nodes"][key]["node_run_id"]


def _statuses(rows: list[dict]) -> list[str]:
    return [r["status"] for r in rows]


def _active(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] == creative_revisions.STATUS_ACTIVE]


def _run_row(db, run_id: str) -> dict:
    with db.transaction() as conn:
        return runs.get(conn, run_id, TENANT)


def _project_row(db, project_id: str) -> dict:
    with db.transaction() as conn:
        return projects.get(conn, project_id, TENANT)


# ══════════════════════════════════════════════════════════════════
# 1) 锁序：Run → NodeRun → NodeAttempt → Project
# ══════════════════════════════════════════════════════════════════


def test_revise_node_acquires_locks_in_global_order(auto_e2e, monkeypatch) -> None:
    """白盒断言：`revise_node` 先锁 Run，再锁 NodeRun，Project 最内层。"""
    from cybercut.orchestrator import engine as engine_module
    from cybercut.storage import node_runs as node_runs_repo

    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    order: list[str] = []

    def _record(name: str, original):
        def wrapper(conn, *args, **kwargs):
            order.append(name)
            return original(conn, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(engine_module.runs, "get_for_update",
                        _record("run", runs.get_for_update))
    monkeypatch.setattr(engine_module.node_runs, "get_for_update",
                        _record("node_run", node_runs_repo.get_for_update))
    monkeypatch.setattr(engine_module.projects, "get_for_update",
                        _record("project", projects.get_for_update))

    orch.revise_node(script_node, TENANT, expected_revision=1,
                     feedback="锁序检查")

    assert order, "必须观察到加锁调用"
    assert order[0] == "run", f"必须先锁 Run，实际 {order}"
    assert "node_run" in order, f"必须锁 NodeRun，实际 {order}"
    assert order.index("node_run") > order.index("run"), (
        f"NodeRun 必须在 Run 之后加锁，实际 {order}")
    if "project" in order:
        assert order.index("project") > order.index("node_run"), (
            f"Project 必须是锁序最内层，实际 {order}")


def test_revise_node_rereads_node_status_under_lock(auto_e2e) -> None:
    """节点状态必须**在持锁后重读**（判别性断言）。

    把节点合法地推进到 `invalidated` 后发起修订：若实现复用「加锁前的
    succeeded 快照」，修订会被接受；只有**在锁内重读**当前状态才会正确拒绝。
    """
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    from cybercut.storage import node_runs as node_runs_repo

    with db.transaction() as conn:
        node_runs_repo.set_status(conn, script_node, TENANT,
                                  to_status=NodeRunStatus.INVALIDATED)

    with pytest.raises(ConflictError, match="不可发起定向修订"):
        orch.revise_node(script_node, TENANT, expected_revision=1,
                         feedback="状态已变更")


def test_revise_accepts_waiting_approval_under_lock(hybrid_e2e) -> None:
    """持锁后读到 `waiting_approval` 时，修订走该分支并回到 ready。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    assert orch.run_state(run["run_id"], TENANT)["statuses"][
        "creative.script"] == "waiting_approval"

    orch.revise_node(script_node, TENANT, expected_revision=1,
                     feedback="待审阶段直接修订")
    worker.drain()
    rows = _rows(db, pid, "script")
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert rows[-1]["payload_json"]["revision_note"] == "待审阶段直接修订"


def test_revise_rejects_cancelled_node(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    from cybercut.storage import node_runs as node_runs_repo

    with db.transaction() as conn:
        node_runs_repo.set_status(conn, script_node, TENANT,
                                  to_status=NodeRunStatus.INVALIDATED)
        node_runs_repo.set_status(conn, script_node, TENANT,
                                  to_status=NodeRunStatus.READY)
        node_runs_repo.set_status(conn, script_node, TENANT,
                                  to_status=NodeRunStatus.RUNNING)
        node_runs_repo.set_status(conn, script_node, TENANT,
                                  to_status=NodeRunStatus.CANCELLED)
    with pytest.raises(ConflictError):
        orch.revise_node(script_node, TENANT, expected_revision=1,
                         feedback="不可修订已取消节点")


def test_revise_concurrent_with_worker_commit_no_deadlock(auto_e2e) -> None:
    """修订与成功提交并发：无死锁、无部分更新、数据库不变量成立。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _revise() -> None:
        try:
            barrier.wait(timeout=30)
            for index in range(3):
                try:
                    orch.revise_node(
                        script_node, TENANT,
                        expected_revision=index + 1,
                        feedback=f"并发修订 {index}")
                    worker.drain()
                except ConflictError:
                    pass  # 版本守卫冲突属预期（并发下版本已推进）
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _drain() -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(20):
                worker.drain()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_revise)] + [
        threading.Thread(target=_drain) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert all(not t.is_alive() for t in threads), "不得出现死锁（线程必须全部退出）"
    assert errors == [], f"不得有未处理异常：{errors}"

    # ── 不变量：每个 kind 至多一个 active；身份仍四处一致；版本号唯一 ──
    for kind in KINDS:
        rows = _rows(db, pid, kind)
        assert rows, kind
        assert len(_active(rows)) <= 1, f"{kind} 的 active 版本必须唯一"
        revision_nos = [r["revision_no"] for r in rows]
        assert len(set(revision_nos)) == len(revision_nos), f"{kind} 版本号重复"
        id_field = creative_revisions.id_field(kind)
        for row in rows:
            assert row[id_field] == row["payload_json"][id_field]
            assert row["revision_no"] == row["payload_json"]["revision_no"]

    # 节点状态必须是合法值（不出现半更新状态）
    state = orch.run_state(run["run_id"], TENANT)
    allowed = {"blocked", "ready", "running", "waiting_approval",
               "succeeded", "failed", "invalidated", "cancelled"}
    assert set(state["statuses"].values()) <= allowed


# ══════════════════════════════════════════════════════════════════
# 2) 版本守卫作用域：按 Run 而非按 Project
# ══════════════════════════════════════════════════════════════════


def test_expected_revision_is_run_scoped(db, data_root, source_root) -> None:
    """同项目两个 Run：较旧 Run 在较新 Run 抬高项目版本后仍可用自己的版本修订。"""
    orch, worker, db, data_root, pid, assets, policy = _build(
        db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)
    run_a = _start(orch, pid, assets, policy=policy)
    run_b = _start(orch, pid, assets, policy=policy)   # 项目仍 active
    worker.drain()

    rows_a = _rows(db, pid, "script", run_id=run_a["run_id"])
    rows_b = _rows(db, pid, "script", run_id=run_b["run_id"])
    assert [r["revision_no"] for r in rows_a] == [1]
    assert [r["revision_no"] for r in rows_b] == [2], "项目级版本号应连续递增"

    # 较旧 Run 用自己的当前版本（1）修订 —— 必须成功
    script_a = _node_run_id(orch, run_a["run_id"], "creative.script")
    orch.revise_node(script_a, TENANT, expected_revision=1,
                     feedback="旧 Run 自修订")
    worker.drain()

    rows_a_after = _rows(db, pid, "script", run_id=run_a["run_id"])
    assert [r["revision_no"] for r in rows_a_after] == [1, 3], (
        "旧 Run 的新版本应取下一个**项目级**版本号，且其守卫按 Run 内版本判定")
    assert rows_a_after[-1]["payload_json"]["revision_note"] == "旧 Run 自修订"
    # 较新 Run 的版本未被触碰
    assert [r["revision_no"] for r in _rows(db, pid, "script",
                                            run_id=run_b["run_id"])] == [2]


def test_expected_revision_rejects_project_level_confusion(db, data_root,
                                                           source_root) -> None:
    """把 project 级最新版本当作守卫会被拒绝（必须以本 Run 版本为准）。"""
    orch, worker, db, data_root, pid, assets, policy = _build(
        db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)
    run_a = _start(orch, pid, assets, policy=policy)
    _start(orch, pid, assets, policy=policy)   # 第二个 Run 抬高项目级版本号
    worker.drain()

    script_a = _node_run_id(orch, run_a["run_id"], "creative.script")
    with pytest.raises(ConflictError) as excinfo:
        orch.revise_node(script_a, TENANT, expected_revision=2,  # 项目级最新
                         feedback="用错作用域")
    assert excinfo.value.details["expected_revision"] == 2
    assert excinfo.value.details["current_revision"] == 1
    assert excinfo.value.details["project_latest"] == 2
    assert excinfo.value.details["run_id"] == run_a["run_id"]


def test_expected_revision_advances_within_same_run(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.revise_node(script_node, TENANT, expected_revision=1, feedback="第一次")
    worker.drain()
    with pytest.raises(ConflictError):
        orch.revise_node(script_node, TENANT, expected_revision=1,
                         feedback="重复使用旧守卫")
    orch.revise_node(script_node, TENANT, expected_revision=2, feedback="第二次")
    worker.drain()
    assert [r["revision_no"] for r in _rows(db, pid, "script")] == [1, 2, 3]


# ══════════════════════════════════════════════════════════════════
# 3) 版本状态 ↔ 审批联动
# ══════════════════════════════════════════════════════════════════


def test_auto_mode_versions_are_active_and_supersede(hybrid_e2e) -> None:
    """自动模式（无门）产物直接 active；新版本使旧 active 变为 superseded。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    # brief 在 hybrid 下无审批门 ⇒ 直接 active
    rows = _rows(db, pid, "brief")
    assert _statuses(rows) == [creative_revisions.STATUS_ACTIVE]

    brief_node = _node_run_id(orch, run["run_id"], "creative.brief")
    orch.revise_node(brief_node, TENANT, expected_revision=1, feedback="改卖点")
    worker.drain()

    rows = _rows(db, pid, "brief")
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert _statuses(rows) == [creative_revisions.STATUS_SUPERSEDED,
                               creative_revisions.STATUS_ACTIVE]
    assert len(_active(rows)) == 1


def test_pending_version_is_draft_not_active(hybrid_e2e) -> None:
    """待审批版本不得标记为 active。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    assert orch.run_state(run["run_id"], TENANT)["statuses"][
        "creative.script"] == "waiting_approval"

    rows = _rows(db, pid, "script")
    assert len(rows) == 1
    assert rows[0]["status"] == creative_revisions.STATUS_DRAFT, (
        "待审批版本必须是 draft，绝不能是 active")
    assert _active(rows) == []


def test_approval_activates_and_supersedes_previous(hybrid_e2e) -> None:
    """批准后：当前版本 active、旧 active 版本 superseded。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.approve(script_node, TENANT, expected_revision=1, decided_by="reviewer")
    worker.drain()
    rows = _rows(db, pid, "script")
    assert _statuses(rows) == [creative_revisions.STATUS_ACTIVE]

    # 修订后再次进入待审：新版本 draft，旧 active 暂不变
    orch.revise_node(script_node, TENANT, expected_revision=1, feedback="再改")
    worker.drain()
    rows = _rows(db, pid, "script")
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert _statuses(rows) == [creative_revisions.STATUS_ACTIVE,
                               creative_revisions.STATUS_DRAFT]

    orch.approve(script_node, TENANT, expected_revision=2, decided_by="reviewer")
    rows = _rows(db, pid, "script")
    assert _statuses(rows) == [creative_revisions.STATUS_SUPERSEDED,
                               creative_revisions.STATUS_ACTIVE]
    assert _active(rows)[0]["revision_no"] == 2


def test_rejection_marks_version_rejected(hybrid_e2e) -> None:
    """驳回：该版本置 rejected，既有 active 版本不受影响。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.approve(script_node, TENANT, expected_revision=1, decided_by="reviewer")
    worker.drain()
    assert _statuses(_rows(db, pid, "script")) == [
        creative_revisions.STATUS_ACTIVE]

    orch.revise_node(script_node, TENANT, expected_revision=1, feedback="改坏了")
    worker.drain()
    orch.reject(script_node, TENANT, expected_revision=2, decided_by="reviewer",
                feedback="不通过")

    rows = _rows(db, pid, "script")
    assert _statuses(rows) == [creative_revisions.STATUS_ACTIVE,
                               creative_revisions.STATUS_REJECTED]
    assert _active(rows)[0]["revision_no"] == 1, "被驳版本不得影响既有 active"


def test_auto_pipeline_all_versions_active(auto_e2e) -> None:
    """全自动管线：四个 kind 各恰有一个 active 版本。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    for kind in KINDS:
        rows = _rows(db, pid, kind)
        assert _statuses(rows) == [creative_revisions.STATUS_ACTIVE], kind


def test_status_transitions_recorded_in_events(hybrid_e2e) -> None:
    """审批事件必须记录版本状态迁移结果（可审计）。"""
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")

    orch.approve(script_node, TENANT, expected_revision=1, decided_by="reviewer")
    worker.drain()

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM cybercut.events"
                " WHERE node_run_id = %s AND event_type = 'approval.approved'",
                (script_node,))
            payload = cur.fetchone()["payload_json"]
    assert payload["activated"]["kind"] == "script"
    assert payload["activated"]["status"] == creative_revisions.STATUS_ACTIVE
    assert payload["activated"]["revision_no"] == 1


def test_rejection_event_records_rejected_version(hybrid_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = hybrid_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    orch.reject(script_node, TENANT, expected_revision=1, decided_by="reviewer",
                feedback="不行")

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM cybercut.events"
                " WHERE node_run_id = %s AND event_type = 'approval.rejected'",
                (script_node,))
            payload = cur.fetchone()["payload_json"]
    assert payload["rejected_version"]["status"] == \
        creative_revisions.STATUS_REJECTED


# ══════════════════════════════════════════════════════════════════
# 4) 受控重开：终态仍拒绝通用迁移
# ══════════════════════════════════════════════════════════════════


def test_generic_transition_still_rejects_terminal_reopen(auto_e2e) -> None:
    """通用 `update_status` 仍拒绝终态出边（未通过全局表开放）。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    assert _run_row(db, run["run_id"])["status"] == RunStatus.SUCCEEDED.value
    assert _project_row(db, pid)["status"] == ProjectStatus.COMPLETED.value

    with pytest.raises(InvalidTransitionError):
        with db.transaction() as conn:
            runs.update_status(conn, run["run_id"], TENANT,
                               to_status=RunStatus.RUNNING)
    with pytest.raises(InvalidTransitionError):
        with db.transaction() as conn:
            projects.update_status(conn, pid, TENANT,
                                   to_status=ProjectStatus.ACTIVE)


def test_reopen_for_revision_resets_terminal_metadata(auto_e2e) -> None:
    """受控重开清理终态元数据（finished_at / failure_* / archived_at）。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    before = _run_row(db, run["run_id"])
    assert before["finished_at"] is not None

    script_node = _node_run_id(orch, run["run_id"], "creative.script")
    orch.revise_node(script_node, TENANT, expected_revision=1, feedback="重开")

    after = _run_row(db, run["run_id"])
    assert after["status"] == RunStatus.RUNNING.value
    assert after["finished_at"] is None, "重开必须清空 finished_at"
    assert after["failure_code"] is None and after["failure_message"] is None
    assert _project_row(db, pid)["status"] == ProjectStatus.ACTIVE.value
    assert _project_row(db, pid)["archived_at"] is None


def test_reopen_helper_only_accepts_terminal_succeeded(db) -> None:
    """受控重开只接受 succeeded / completed；其它状态一律拒绝。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="reopen-guard")
    with db.transaction() as conn:
        run = runs.create(conn, project_id=project["project_id"],
                          tenant_id=TENANT, pipeline_version="creative-v1",
                          status=RunStatus.RUNNING)

    with pytest.raises(ConflictError):
        with db.transaction() as conn:
            runs.reopen_for_revision(conn, run["run_id"], TENANT)
    with pytest.raises(ConflictError):
        with db.transaction() as conn:
            projects.reopen_for_revision(conn, project["project_id"], TENANT)


def test_repaired_doc_claim_only_two_controlled_edges() -> None:
    """状态表本身不得为终态开放出边（受控路径在 storage 层显式实现）。"""
    from cybercut.contracts import state_machine as sm

    assert sm.allowed_from(sm.RUN, RunStatus.SUCCEEDED) == frozenset()
    assert sm.allowed_from(sm.PROJECT, ProjectStatus.COMPLETED) == frozenset()
