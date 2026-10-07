"""聚合状态测试：Run / Project 随节点进度收敛 —— 真实 PostgreSQL。"""
from __future__ import annotations

from cybercut.storage import projects, runs
from tests.cybercut.helpers import (
    TENANT,
    make_failing_registry,
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


def test_run_becomes_waiting_approval_then_completed(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    worker = make_worker(db, orch, make_registry(), data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    worker.drain()
    assert statuses(orch, run["run_id"]) == {
        "node_a": "succeeded",
        "node_b": "succeeded",
        "approval_gate": "waiting_approval",
        "node_c": "blocked",
    }
    assert _run_row(db, run["run_id"])["status"] == "waiting_approval"
    assert _project_row(db, project["project_id"])["status"] == "active"

    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    assert approval is not None and approval["status"] == "pending"
    orch.approve(gate["node_run_id"], TENANT,
                 expected_revision=approval["expected_revision"], decided_by="alice")
    worker.drain()

    assert all(s == "succeeded" for s in statuses(orch, run["run_id"]).values())
    assert _run_row(db, run["run_id"])["status"] == "succeeded"
    assert _project_row(db, project["project_id"])["status"] == "completed"


def test_run_fails_when_any_node_fails(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    registry, _handler = make_failing_registry("node_a", error_code="BOOM")
    worker = make_worker(db, orch, registry, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    worker.drain()

    assert statuses(orch, run["run_id"])["node_a"] == "failed"
    run_row = _run_row(db, run["run_id"])
    assert run_row["status"] == "failed"
    assert run_row["failure_code"] == "NODE_FAILED"
    assert run_row["finished_at"] is not None
    assert _project_row(db, project["project_id"])["status"] == "failed"


def test_project_starts_draft_and_becomes_active(db) -> None:
    orch = make_orchestrator(db)
    project = make_project(orch)
    assert _project_row(db, project["project_id"])["status"] == "draft"
    run = make_run(orch, project=project)
    assert _project_row(db, project["project_id"])["status"] == "active"
    assert _project_row(db, project["project_id"])["current_run_id"] == run["run_id"]


def test_run_no_increments_per_project(db) -> None:
    orch = make_orchestrator(db)
    project = make_project(orch)
    first = make_run(orch, project=project)
    second = make_run(orch, project=project)
    assert first["run_no"] == 1
    assert second["run_no"] == 2
