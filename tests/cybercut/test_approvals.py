"""审批测试：approve/reject 语义与 expected_revision 冲突 —— 真实 PostgreSQL。"""
from __future__ import annotations

import pytest

from cybercut.contracts.errors import ConflictError, NotFoundError
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


def _reach_gate(db, data_root):
    orch = make_orchestrator(db, data_root)
    worker = make_worker(db, orch, make_registry(), data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    worker.drain()
    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    assert approval is not None
    return orch, worker, project, run, gate, approval


def test_approve_advances_downstream(db, data_root) -> None:
    orch, worker, _project, run, gate, approval = _reach_gate(db, data_root)
    assert statuses(orch, run["run_id"])["approval_gate"] == "waiting_approval"

    orch.approve(gate["node_run_id"], TENANT,
                 expected_revision=approval["expected_revision"], decided_by="alice")

    after = statuses(orch, run["run_id"])
    assert after["approval_gate"] == "succeeded"
    assert after["node_c"] == "ready"

    worker.drain()
    assert statuses(orch, run["run_id"])["node_c"] == "succeeded"


def test_approve_with_stale_expected_revision_conflicts(db, data_root) -> None:
    orch, _worker, _project, _run, gate, approval = _reach_gate(db, data_root)
    with pytest.raises(ConflictError) as excinfo:
        orch.approve(gate["node_run_id"], TENANT,
                     expected_revision=approval["expected_revision"] + 99,
                     decided_by="alice")
    assert excinfo.value.details["expected_revision"] == (
        approval["expected_revision"] + 99
    )
    # 状态未被改动
    assert statuses(orch, _run["run_id"])["approval_gate"] == "waiting_approval"


def test_approve_twice_conflicts(db, data_root) -> None:
    orch, _worker, _project, _run, gate, approval = _reach_gate(db, data_root)
    orch.approve(gate["node_run_id"], TENANT,
                 expected_revision=approval["expected_revision"], decided_by="alice")
    with pytest.raises(ConflictError):
        orch.approve(gate["node_run_id"], TENANT,
                     expected_revision=approval["expected_revision"],
                     decided_by="bob")


def test_decision_on_non_waiting_node_conflicts(db, data_root) -> None:
    orch, _worker, _project, run, _gate, _approval = _reach_gate(db, data_root)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    with pytest.raises(ConflictError):
        orch.approve(node_a["node_run_id"], TENANT, expected_revision=1,
                     decided_by="alice")


def test_decision_on_unknown_node_is_not_found(db, data_root) -> None:
    orch = make_orchestrator(db, data_root)
    with pytest.raises(NotFoundError):
        orch.reject("no-such-node-run", TENANT, expected_revision=1, decided_by="alice")


def test_reject_returns_gate_to_ready_and_reopens_upstream(db, data_root) -> None:
    orch, worker, _project, run, gate, approval = _reach_gate(db, data_root)
    node_b = orch.run_state(run["run_id"], TENANT)["nodes"]["node_b"]
    node_b_attempts_before = len(orch.attempts_of(node_b["node_run_id"], TENANT))

    orch.reject(gate["node_run_id"], TENANT,
                expected_revision=approval["expected_revision"], decided_by="bob",
                feedback="脚本不合格")

    after = statuses(orch, run["run_id"])
    assert after["approval_gate"] == "ready"
    assert after["node_b"] == "ready"
    assert after["node_c"] == "blocked"

    node_b_attempts_after = orch.attempts_of(node_b["node_run_id"], TENANT)
    assert len(node_b_attempts_after) == node_b_attempts_before + 1, "驳回应为上游新建 attempt"
    assert node_b_attempts_after[-1]["status"] == "queued"

    # 重跑后闸门再次进入待审，且 expected_revision 提高（新产物版本）
    worker.drain()
    assert statuses(orch, run["run_id"])["approval_gate"] == "waiting_approval"
    new_approval = pending_approval(db, gate["node_run_id"])
    assert new_approval is not None
    assert new_approval["expected_revision"] > approval["expected_revision"]
