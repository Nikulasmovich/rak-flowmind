"""取消测试：queued / running attempt 取消，Run 取消收敛 —— 真实 PostgreSQL。"""
from __future__ import annotations

from cybercut.storage import projects, runs
from cybercut.storage.db import Database
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_registry,
    make_run,
    make_worker,
    statuses,
)


def test_cancel_queued_attempt(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]
    assert attempt["status"] == "queued"

    cancelled = orch.cancel_attempt(attempt["attempt_id"], TENANT)
    assert cancelled["status"] == "cancelled"
    assert cancelled["finished_at"] is not None


def test_cancel_running_attempt(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]

    from cybercut.executor.claim import claim_next_attempt
    from cybercut.storage import node_attempts

    database = Database(db.dsn)
    claimed = claim_next_attempt(database, "w1")
    with database.transaction() as conn:
        node_attempts.mark_running(conn, claimed["attempt_id"], TENANT, "w1")

    cancelled = orch.cancel_attempt(attempt["attempt_id"], TENANT)
    assert cancelled["status"] == "cancelled"


def test_request_run_cancel_converges_run_and_project(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    worker = make_worker(db, orch, make_registry(), data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    worker.step()  # node_a 成功，node_b 入队
    before = statuses(orch, run["run_id"])
    assert before["node_a"] == "succeeded"

    cancelled_run = orch.request_run_cancel(run["run_id"], TENANT)
    assert cancelled_run["status"] == "cancelled"
    assert cancelled_run["cancel_requested_at"] is not None

    with db.transaction() as conn:
        project_row = projects.get(conn, project["project_id"], TENANT)
        run_row = runs.get(conn, run["run_id"], TENANT)
    assert run_row["status"] == "cancelled"
    assert run_row["finished_at"] is not None
    assert project_row["status"] == "cancelled"

    # 活动 attempt 全部收敛
    node_b = orch.run_state(run["run_id"], TENANT)["nodes"]["node_b"]
    for attempt in orch.attempts_of(node_b["node_run_id"], TENANT):
        assert attempt["status"] == "cancelled"


def test_run_cancel_is_idempotent(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    first = orch.request_run_cancel(run["run_id"], TENANT)
    second = orch.request_run_cancel(run["run_id"], TENANT)
    assert first["status"] == "cancelled"
    assert second["status"] == "cancelled"


def test_cancelled_run_is_not_advanced(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    orch.request_run_cancel(run["run_id"], TENANT)

    result = orch.advance_run(run["run_id"], TENANT)
    assert result.get("terminal") is True
    assert result["enqueued"] == []
