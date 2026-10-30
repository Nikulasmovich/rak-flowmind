"""租户隔离测试：跨租户一律「不存在」——真实 PostgreSQL。"""
from __future__ import annotations

import pytest

from cybercut.contracts.errors import NotFoundError
from cybercut.storage import node_attempts, node_runs, projects, runs
from tests.cybercut.helpers import TENANT, make_orchestrator, make_project, make_run

OTHER = "tenant-b"


def test_cross_tenant_reads_are_not_found(db) -> None:
    orch = make_orchestrator(db)
    project = make_project(orch, TENANT)
    run = make_run(orch, TENANT, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]

    with db.transaction() as conn:
        assert projects.get(conn, project["project_id"], OTHER) is None
        assert runs.get(conn, run["run_id"], OTHER) is None
        assert node_runs.get(conn, node_a["node_run_id"], OTHER) is None
        assert node_attempts.get(conn, attempt["attempt_id"], OTHER) is None

    with pytest.raises(NotFoundError):
        orch.get_project(project["project_id"], OTHER)
    with pytest.raises(NotFoundError):
        orch.advance_run(run["run_id"], OTHER)
    with pytest.raises(NotFoundError):
        orch.request_run_cancel(run["run_id"], OTHER)
    with pytest.raises(NotFoundError):
        orch.cancel_attempt(attempt["attempt_id"], OTHER)
    with pytest.raises(NotFoundError):
        orch.approve(node_a["node_run_id"], OTHER, expected_revision=1,
                     decided_by="mallory")


def test_cross_tenant_cannot_mutate(db) -> None:
    orch = make_orchestrator(db)
    project = make_project(orch, TENANT)
    with pytest.raises(NotFoundError):
        orch.start_run(project_id=project["project_id"], tenant_id=OTHER)

    with db.transaction() as conn:
        row = projects.get(conn, project["project_id"], TENANT)
    assert row["status"] == "draft", "跨租户请求不得影响原租户资源"


def test_listing_is_tenant_scoped(db) -> None:
    orch = make_orchestrator(db)
    make_project(orch, TENANT, name="mine")
    make_project(orch, OTHER, name="theirs")

    with db.transaction() as conn:
        mine = projects.list_for_tenant(conn, TENANT)
        theirs = projects.list_for_tenant(conn, OTHER)
    assert [p["name"] for p in mine] == ["mine"]
    assert [p["name"] for p in theirs] == ["theirs"]
