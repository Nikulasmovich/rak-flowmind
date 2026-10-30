"""重试测试：新建 attempt、保留历史、失败传导 —— 真实 PostgreSQL。"""
from __future__ import annotations

from cybercut.storage import node_attempts, node_runs, projects, runs
from tests.cybercut.helpers import (
    TENANT,
    make_failing_registry,
    make_orchestrator,
    make_project,
    make_run,
    make_worker,
    statuses,
)


def test_failure_keeps_attempt_history(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    registry, _handler = make_failing_registry("node_b", error_code="BAD_SCRIPT")
    worker = make_worker(db, orch, registry, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    worker.drain()

    node_b = orch.run_state(run["run_id"], TENANT)["nodes"]["node_b"]
    attempts = orch.attempts_of(node_b["node_run_id"], TENANT)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "BAD_SCRIPT"
    assert attempts[0]["finished_at"] is not None
    assert statuses(orch, run["run_id"])["node_b"] == "failed"


def _seed_failed_node(db) -> tuple[dict, dict, dict, dict]:
    """构造：run=running、node_run=failed、attempt#1=failed（经合法迁移路径）。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="p", status="active")
        run = runs.create(
            conn, project_id=project["project_id"], tenant_id=TENANT,
            pipeline_version="v1", status="running",
        )
        node_run = node_runs.create(
            conn, run_id=run["run_id"], tenant_id=TENANT, node_key="node_a",
            status="ready",
        )
        attempt = node_attempts.create(
            conn, node_run_id=node_run["node_run_id"], tenant_id=TENANT, attempt_no=1,
        )
        # 与 orchestrator 入队行为一致：递增 latest_attempt_no
        node_runs.bump_attempt_no(conn, node_run["node_run_id"], TENANT)
    # queued -> claimed -> running -> failed（严格走状态机）
    with db.transaction() as conn:
        claimed = node_attempts.claim_next(conn, "seed-worker")
        assert claimed is not None and claimed["attempt_id"] == attempt["attempt_id"]
        node_run = node_runs.get(conn, node_run["node_run_id"], TENANT)
        node_runs.set_status(conn, node_run["node_run_id"], TENANT, to_status="running")
    with db.transaction() as conn:
        node_attempts.mark_running(conn, attempt["attempt_id"], TENANT, "seed-worker")
        node_attempts.finish(
            conn, attempt["attempt_id"], TENANT, to_status="failed",
            error_code="CONTROLLED_FAILURE", error_message="首次失败",
        )
        node_runs.set_status(conn, node_run["node_run_id"], TENANT, to_status="failed")
        node_run = node_runs.get(conn, node_run["node_run_id"], TENANT)
    return project, run, node_run, attempt


def test_retry_creates_new_attempt_and_preserves_history(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    _project, run, node_run, first_attempt = _seed_failed_node(db)
    assert node_run["status"] == "failed"
    assert node_run["latest_attempt_no"] == 1

    refreshed = orch.retry_node(node_run["node_run_id"], TENANT)
    assert refreshed["status"] == "ready"
    assert refreshed["latest_attempt_no"] == 2

    attempts = orch.attempts_of(node_run["node_run_id"], TENANT)
    assert len(attempts) == 2, "重试必须新建 attempt，不得覆盖旧尝试"
    assert attempts[0]["attempt_id"] == first_attempt["attempt_id"]
    assert attempts[0]["status"] == "failed", "历史尝试应保留为 failed"
    assert attempts[0]["error_message"] == "首次失败"
    assert attempts[1]["attempt_no"] == 2
    assert attempts[1]["status"] == "queued"
    assert attempts[1]["attempt_id"] != first_attempt["attempt_id"]


def test_retry_of_non_failed_node_is_rejected(db, data_root) -> None:
    """succeeded 节点不可重试（状态机守卫）。"""
    from cybercut.contracts.errors import InvalidTransitionError

    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    # node_a 当前为 ready（尚未执行）-> ready 不可回到 ready
    with db.transaction() as conn:
        node_runs.set_status(conn, node_a["node_run_id"], TENANT, to_status="running")
        node_runs.set_status(conn, node_a["node_run_id"], TENANT, to_status="succeeded")
    import pytest

    with pytest.raises(InvalidTransitionError):
        orch.retry_node(node_a["node_run_id"], TENANT)
