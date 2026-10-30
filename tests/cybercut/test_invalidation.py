"""失效测试：驳回/产物修订使下游失效，且已完成前置节点不重复执行。

使用扩展管线（含与闸门并行的分支）：

    node_a -> node_b -> approval_gate -> node_c
    node_b -> node_d
"""
from __future__ import annotations

from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_registry_for,
    make_run,
    make_worker,
    pending_approval,
    statuses,
)

FANOUT_PIPELINE = PipelineSpec(
    name="fanout",
    version="fanout-v1",
    nodes=(
        NodeSpec("node_a"),
        NodeSpec("node_b", depends_on=("node_a",)),
        NodeSpec("node_d", depends_on=("node_b",)),
        NodeSpec("approval_gate", depends_on=("node_b",), approval_required=True),
        NodeSpec("node_c", depends_on=("approval_gate",)),
    ),
)


def test_reject_invalidates_succeeded_descendants(db, data_root) -> None:
    orch = make_orchestrator(db, data_root, pipeline=FANOUT_PIPELINE)
    registry = make_registry_for(
        ["node_a", "node_b", "node_c", "node_d"], gate_key="approval_gate"
    )
    worker = make_worker(db, orch, registry, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    worker.drain()
    before = statuses(orch, run["run_id"])
    assert before["node_a"] == "succeeded"
    assert before["node_b"] == "succeeded"
    assert before["node_d"] == "succeeded"          # 并行分支已成功
    assert before["approval_gate"] == "waiting_approval"
    assert before["node_c"] == "blocked"

    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    node_d = orch.run_state(run["run_id"], TENANT)["nodes"]["node_d"]
    a_attempts_before = len(orch.attempts_of(node_a["node_run_id"], TENANT))
    d_attempts_before = len(orch.attempts_of(node_d["node_run_id"], TENANT))

    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    orch.reject(gate["node_run_id"], TENANT,
                expected_revision=approval["expected_revision"], decided_by="bob",
                feedback="脚本需修订")

    after = statuses(orch, run["run_id"])
    assert after["node_b"] == "ready", "被驳上游应失效并重跑"
    assert after["node_d"] == "invalidated", "已成功的下游分支应失效"
    assert after["node_a"] == "succeeded", "已完成的前置节点不受影响"

    # 局部重跑不重复执行已完成的前置节点
    a_attempts_after = orch.attempts_of(node_a["node_run_id"], TENANT)
    assert len(a_attempts_after) == a_attempts_before, "node_a 不应被重跑"

    # 重新推进：node_b / node_d 各新建 attempt
    worker.drain()
    final = statuses(orch, run["run_id"])
    assert final["node_b"] == "succeeded"
    assert final["node_d"] == "succeeded"
    assert len(orch.attempts_of(node_d["node_run_id"], TENANT)) == d_attempts_before + 1
    assert len(orch.attempts_of(node_a["node_run_id"], TENANT)) == a_attempts_before


def test_invalidated_node_can_be_rerun(db, data_root) -> None:
    """invalidated -> ready -> running -> succeeded 全链路可走通。"""
    orch = make_orchestrator(db, data_root, pipeline=FANOUT_PIPELINE)
    registry = make_registry_for(
        ["node_a", "node_b", "node_c", "node_d"], gate_key="approval_gate"
    )
    worker = make_worker(db, orch, registry, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    worker.drain()

    gate = orch.run_state(run["run_id"], TENANT)["nodes"]["approval_gate"]
    approval = pending_approval(db, gate["node_run_id"])
    orch.reject(gate["node_run_id"], TENANT,
                expected_revision=approval["expected_revision"], decided_by="bob")

    node_d = orch.run_state(run["run_id"], TENANT)["nodes"]["node_d"]
    assert node_d["status"] == "invalidated"

    worker.drain()
    node_d = orch.run_state(run["run_id"], TENANT)["nodes"]["node_d"]
    assert node_d["status"] == "succeeded"
    assert node_d["invalidated_at"] is None, "回到 ready 时应清空失效标记"
