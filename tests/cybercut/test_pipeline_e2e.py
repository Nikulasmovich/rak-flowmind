"""端到端闭环测试：node_a -> node_b -> approval_gate -> node_c（真实 PostgreSQL）。"""
from __future__ import annotations

from cybercut.storage import artifacts, projects, runs
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_registry,
    make_run,
    make_worker,
    pending_approval,
    statuses,
)


def _project_row(db, project_id: str) -> dict:
    with db.transaction() as conn:
        return projects.get(conn, project_id, TENANT)


def _run_row(db, run_id: str) -> dict:
    with db.transaction() as conn:
        return runs.get(conn, run_id, TENANT)


def test_echo_pipeline_full_loop(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    worker = make_worker(db, orch, make_registry(), data_root, worker_id="w-e2e")
    project = make_project(orch, name="e2e")
    run = make_run(orch, project=project)

    # 初始：root ready，其余 blocked
    assert statuses(orch, run["run_id"]) == {
        "node_a": "ready",
        "node_b": "blocked",
        "approval_gate": "blocked",
        "node_c": "blocked",
    }

    # ① node_a 执行 -> node_b 变 ready
    first = worker.step()
    assert first is not None and first.node_key == "node_a"
    st = statuses(orch, run["run_id"])
    assert st["node_a"] == "succeeded"
    assert st["node_b"] == "ready"
    assert st["approval_gate"] == "blocked"
    assert _run_row(db, run["run_id"])["status"] == "running"

    # ② node_b 执行 -> 闸门依赖满足，变 ready（尚未执行）
    second = worker.step()
    assert second is not None and second.node_key == "node_b"
    st = statuses(orch, run["run_id"])
    assert st["node_b"] == "succeeded"
    assert st["approval_gate"] == "ready"
    assert st["node_c"] == "blocked"

    # ③ 闸门执行 -> 产出待审记录，进入 waiting_approval；Run 同步 waiting_approval
    third = worker.step()
    assert third is not None and third.node_key == "approval_gate"
    assert statuses(orch, run["run_id"])["approval_gate"] == "waiting_approval"
    assert _run_row(db, run["run_id"])["status"] == "waiting_approval"

    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    assert approval is not None and approval["status"] == "pending"

    # ④ 无更多可执行任务
    assert worker.step() is None

    # ⑤ 审批通过 -> node_c 变 ready 并可执行
    orch.approve(gate["node_run_id"], TENANT,
                 expected_revision=approval["expected_revision"], decided_by="alice")
    assert statuses(orch, run["run_id"])["node_c"] == "ready"

    fourth = worker.step()
    assert fourth is not None and fourth.node_key == "node_c"

    # ⑥ Run 与 Project 收敛为成功
    assert all(s == "succeeded" for s in statuses(orch, run["run_id"]).values())
    assert _run_row(db, run["run_id"])["status"] == "succeeded"
    assert _run_row(db, run["run_id"])["finished_at"] is not None
    assert _project_row(db, project["project_id"])["status"] == "completed"


def test_pipeline_registers_one_artifact_per_node(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    worker = make_worker(db, orch, make_registry(), data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    worker.drain()

    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    orch.approve(gate["node_run_id"], TENANT,
                 expected_revision=approval["expected_revision"], decided_by="alice")
    worker.drain()

    for node_key in ("node_a", "node_b", "node_c"):
        node_run = orch.run_state(run["run_id"], TENANT)["nodes"][node_key]
        with db.transaction() as conn:
            rows = artifacts.list_for_node_run(conn, node_run["node_run_id"], TENANT)
        assert len(rows) == 1, f"{node_key} 应登记 1 个产物版本"
        assert rows[0]["sha256"] and rows[0]["size_bytes"] > 0
        assert rows[0]["relative_path"].startswith("artifacts/")

    # 产物不得落在 flowmind 的 tasks/outputs 目录
    for node_key in ("node_a", "node_b", "node_c"):
        node_run = orch.run_state(run["run_id"], TENANT)["nodes"][node_key]
        with db.transaction() as conn:
            rows = artifacts.list_for_node_run(conn, node_run["node_run_id"], TENANT)
        assert "flowmind" not in rows[0]["relative_path"]
        assert (data_root / "projects" / project["project_id"] / rows[0]["relative_path"]).is_file()


def test_unknown_handler_fails_attempt_as_handler_not_found(db, data_root) -> None:
    """注册表缺 handler -> attempt 失败为 HANDLER_NOT_FOUND（不是静默跳过）。"""
    orch = make_orchestrator(db, data_root)
    empty_registry = make_registry()
    # 用只注册 node_b/node_c/gate 的注册表，制造 node_a 缺失
    from cybercut.nodes.registry import NodeRegistry

    partial = NodeRegistry()
    worker = make_worker(db, orch, partial, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    outcome = worker.step()
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error_code == "HANDLER_NOT_FOUND"
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]
    assert attempt["error_code"] == "HANDLER_NOT_FOUND"
    assert statuses(orch, run["run_id"])["node_a"] == "failed"
    assert empty_registry is not None  # 保留引用，避免未使用告警
