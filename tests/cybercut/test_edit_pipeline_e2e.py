"""WB-04 编辑规划与模板绑定端到端测试（真实 PostgreSQL + Worker）。

媒体层使用可注入 fake，创作/编辑层使用确定性 provider。
"""
from __future__ import annotations

import json

import pytest

from cybercut.contracts.editing import TemplateBinding, TimelineSpec
from cybercut.creative.policy import (
    AUTO_POLICY,
    HYBRID_POLICY,
    MANUAL_POLICY,
    ApprovalMode,
    ApprovalPolicy,
)
from cybercut.orchestrator.pipeline import (
    CREATIVE_PIPELINE,
    build_creative_pipeline,
)
from cybercut.storage import creative_revisions
from tests.cybercut.helpers import TENANT
from tests.cybercut.test_creative_pipeline_e2e import (
    CREATIVE_INPUT,
    _build,
)

EDIT_KINDS = ("edit_plan", "template_bind")
ALL_KINDS = ("brief", "script", "storyboard", "match",
             "edit_plan", "template_bind")
CREATIVE_INPUT = {**CREATIVE_INPUT, "edit_input": {"template_id": "product_highlight"}}


@pytest.fixture
def auto_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, AUTO_POLICY)


@pytest.fixture
def hybrid_e2e(db, data_root, source_root):
    return _build(db, data_root, source_root, CREATIVE_PIPELINE, HYBRID_POLICY)


def _start(orch, pid, assets, *, policy):
    return orch.start_run(
        project_id=pid, tenant_id=TENANT,
        asset_ids=[a["asset_id"] for a in assets],
        creative_input=CREATIVE_INPUT, approval_policy=policy.to_json())


def _rows(db, pid: str, kind: str) -> list[dict]:
    with db.transaction() as conn:
        return creative_revisions.list_for_project(conn, kind, pid, TENANT)


def _node_run_id(orch, run_id: str, key: str) -> str:
    return orch.run_state(run_id, TENANT)["nodes"][key]["node_run_id"]


# ══════════════════════════════════════════════════════════════════
# 全自动端到端
# ══════════════════════════════════════════════════════════════════


def test_auto_pipeline_completes_all_eleven_nodes(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    assert len(state["statuses"]) == 11
    assert set(state["statuses"]) == set(CREATIVE_PIPELINE.keys())
    assert all(s == "succeeded" for s in state["statuses"].values()), \
        state["statuses"]


def test_auto_pipeline_publishes_six_artifact_kinds(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    for key in ("creative.brief", "creative.script", "creative.storyboard",
                "creative.match", "edit.plan", "template.bind"):
        node_run_id = state["nodes"][key]["node_run_id"]
        with db.transaction() as conn:
            from cybercut.storage import artifacts

            kinds = {r["kind"] for r in
                     artifacts.list_for_node_run(conn, node_run_id, TENANT)}
        assert kinds == {key}, f"{key} 产物 kind 不匹配：{kinds}"


def test_edit_planning_tables_are_populated(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    for kind in ALL_KINDS:
        rows = _rows(db, pid, kind)
        assert len(rows) == 1, f"{kind} 应恰好一条版本记录"
        assert rows[0]["revision_no"] == 1
        assert rows[0]["status"] == "active"
        assert rows[0]["prompt_version"]


def test_timeline_spec_is_deterministic_and_framed(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    payload = _rows(db, pid, "edit_plan")[0]["payload_json"]
    spec = TimelineSpec.model_validate(payload)

    assert spec.total_frames == sum(c.duration_frames for c in spec.clips)
    assert spec.playback_duration_us == sum(
        c.playback_duration_us for c in spec.clips)
    assert spec.clips, "阈值 0.05 下应有已匹配片段"
    assert spec.total_frames > 0
    # 片段溯源完整
    for clip in spec.clips:
        assert clip.shot_id and clip.asset_id and clip.segment_id
        assert clip.storyboard_artifact_revision_id
        assert clip.match_artifact_revision_id
        assert clip.analysis_artifact_revision_id


def test_edit_plan_is_byte_stable_across_reruns(auto_e2e) -> None:
    """重复执行（相同冻结输入）输出字节级稳定。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    first = _rows(db, pid, "edit_plan")[0]["payload_json"]

    edit_node = _node_run_id(orch, run["run_id"], "edit.plan")
    orch.revise_node(edit_node, TENANT, expected_revision=1, feedback="重跑")
    worker.drain()
    rows = _rows(db, pid, "edit_plan")
    assert len(rows) == 2
    second = rows[1]["payload_json"]

    # 除身份与修订说明外逐字节一致
    def strip(payload: dict) -> dict:
        return {k: v for k, v in payload.items()
                if k not in ("edit_plan_revision_id", "revision_no",
                             "revision_note")}

    assert json.dumps(strip(first), sort_keys=True) == json.dumps(
        strip(second), sort_keys=True)


def test_template_binding_records_template_and_unmet(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    _start(orch, pid, assets, policy=policy)
    worker.drain()
    payload = _rows(db, pid, "template_bind")[0]["payload_json"]
    binding = TemplateBinding.model_validate(payload)
    assert binding.template_id == "product_highlight"
    assert binding.template_version == "1.0.0"
    assert binding.selection_reason == "explicit:frozen_run_input"
    assert binding.accepted is True
    # 绑定产物不携带文案
    assert "text_intents" not in payload


# ══════════════════════════════════════════════════════════════════
# hybrid / manual 下的 timeline gate
# ══════════════════════════════════════════════════════════════════


def test_hybrid_pipeline_pauses_at_script(auto_e2e) -> None:
    """hybrid 默认门 = creative.script（WB-03R2 语义保持不变）。"""
    from cybercut.contracts.enums import RunStatus

    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    hybrid = ApprovalPolicy(mode=ApprovalMode.HYBRID,
                            gates=HYBRID_POLICY.gates)
    run = _start(orch, pid, assets, policy=hybrid)
    worker.drain()
    state = orch.run_state(run["run_id"], TENANT)
    assert state["statuses"]["creative.script"] == "waiting_approval"
    assert state["run"]["status"] == RunStatus.WAITING_APPROVAL.value


def test_timeline_gate_waits_approves_and_completes(db, data_root, source_root) -> None:
    """timeline gate（template.bind）：等待 → 批准 → 全部完成。"""
    from cybercut.contracts.enums import RunStatus

    timeline_gates = ApprovalPolicy(mode=ApprovalMode.HYBRID,
                                    gates=("template.bind",))
    pipeline = build_creative_pipeline(timeline_gates)
    orch, worker, db, data_root, pid, assets, _ = _build(
        db, data_root, source_root, pipeline, timeline_gates)
    run = _start(orch, pid, assets, policy=timeline_gates)
    worker.drain()

    state = orch.run_state(run["run_id"], TENANT)
    # 非 gate 的编辑节点不应被隐式跳过：edit.plan 正常完成
    assert state["statuses"]["edit.plan"] == "succeeded"
    assert state["statuses"]["template.bind"] == "waiting_approval"
    assert state["run"]["status"] == RunStatus.WAITING_APPROVAL.value

    orch.approve(_node_run_id(orch, run["run_id"], "template.bind"),
                 TENANT, expected_revision=1, decided_by="reviewer")
    worker.drain()
    final = orch.run_state(run["run_id"], TENANT)
    assert all(s == "succeeded" for s in final["statuses"].values()), \
        final["statuses"]
    assert len(_rows(db, pid, "template_bind")) == 1


def test_timeline_gate_rejection_blocks_downstream(db, data_root, source_root) -> None:
    """timeline gate 驳回：版本置 rejected，不产生后续产物。"""
    from cybercut.contracts.enums import ApprovalStatus
    from cybercut.storage import approvals

    timeline_gates = ApprovalPolicy(mode=ApprovalMode.HYBRID,
                                    gates=("template.bind",))
    pipeline = build_creative_pipeline(timeline_gates)
    orch, worker, db, data_root, pid, assets, _ = _build(
        db, data_root, source_root, pipeline, timeline_gates)
    run = _start(orch, pid, assets, policy=timeline_gates)
    worker.drain()
    node_id = _node_run_id(orch, run["run_id"], "template.bind")

    orch.reject(node_id, TENANT, expected_revision=1, decided_by="reviewer",
                feedback="绑定需调整")
    worker.drain()

    with db.transaction() as conn:
        approval = approvals.find_by_revision(conn, node_id, 1, TENANT)
    assert approval["status"] == ApprovalStatus.REJECTED.value
    rows = _rows(db, pid, "template_bind")
    assert rows and rows[0]["status"] == "rejected"


def test_manual_mode_gates_all_six_nodes(db, data_root, source_root) -> None:
    """manual 模式：六个规划节点依次等待审批。"""
    orch, worker, db, data_root, pid, assets, _ = _build(
        db, data_root, source_root, CREATIVE_PIPELINE, MANUAL_POLICY)
    run = _start(orch, pid, assets, policy=MANUAL_POLICY)

    gate_order = ("creative.brief", "creative.script", "creative.storyboard",
                  "creative.match", "edit.plan", "template.bind")
    for key in gate_order:
        worker.drain()
        state = orch.run_state(run["run_id"], TENANT)
        assert state["statuses"][key] == "waiting_approval", key
        orch.approve(state["nodes"][key]["node_run_id"], TENANT,
                     expected_revision=1, decided_by="reviewer")

    worker.drain()
    final = orch.run_state(run["run_id"], TENANT)
    assert all(s == "succeeded" for s in final["statuses"].values())


# ══════════════════════════════════════════════════════════════════
# 局部修订：storyboard/match 变更使编辑规划与模板绑定失效
# ══════════════════════════════════════════════════════════════════


def test_storyboard_revision_invalidates_edit_chain(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    counts_before = {k: len(_rows(db, pid, k)) for k in ALL_KINDS}

    storyboard_node = _node_run_id(orch, run["run_id"], "creative.storyboard")
    orch.revise_node(storyboard_node, TENANT, expected_revision=1,
                     feedback="分镜调整")
    worker.drain()

    # 编辑规划与模板绑定被级联失效并重跑
    for kind in ("edit_plan", "template_bind"):
        assert len(_rows(db, pid, kind)) == 2, kind
    # 上游不受影响
    assert len(_rows(db, pid, "brief")) == counts_before["brief"]
    assert len(_rows(db, pid, "script")) == counts_before["script"]


def test_match_revision_invalidates_edit_chain(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    match_node = _node_run_id(orch, run["run_id"], "creative.match")
    orch.revise_node(match_node, TENANT, expected_revision=1,
                     new_prompt="重新匹配")
    worker.drain()
    for kind in ("edit_plan", "template_bind"):
        assert len(_rows(db, pid, kind)) == 2, kind


def test_template_bind_revision_does_not_touch_upstream(auto_e2e) -> None:
    """模板绑定修订不得导致素材分析、脚本或分镜无关重跑。"""
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()

    def _attempt_counts() -> dict[str, int]:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT n.node_key AS k, count(a.attempt_id) AS n"
                    "  FROM cybercut.node_runs n"
                    "  LEFT JOIN cybercut.node_attempts a"
                    "    ON a.node_run_id = n.node_run_id"
                    " WHERE n.run_id = %s GROUP BY n.node_key", (run["run_id"],))
                return {r["k"]: int(r["n"]) for r in cur.fetchall()}

    before = _attempt_counts()
    bind_node = _node_run_id(orch, run["run_id"], "template.bind")
    orch.revise_node(bind_node, TENANT, expected_revision=1,
                     new_prompt="换个模板")
    worker.drain()
    after = _attempt_counts()

    for key in ("asset_probe", "asset_normalize", "scene_detect",
                "speech_transcribe", "asset_analysis", "creative.brief",
                "creative.script", "creative.storyboard", "creative.match",
                "edit.plan"):
        assert after[key] == before[key], f"{key} 不得重跑"
    assert after["template.bind"] == before["template.bind"] + 1


def test_frozen_retry_no_drift_for_edit_plan(auto_e2e) -> None:
    orch, worker, db, data_root, pid, assets, policy = auto_e2e
    run = _start(orch, pid, assets, policy=policy)
    worker.drain()
    edit_node = _node_run_id(orch, run["run_id"], "edit.plan")

    def _refs() -> list[dict]:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT input_revision_refs FROM cybercut.node_runs"
                    " WHERE node_run_id = %s", (edit_node,))
                return cur.fetchone()["input_revision_refs"]

    refs = _refs()
    kinds = {r["kind"] for r in refs}
    assert {"creative.storyboard", "creative.match", "creative.script",
            "creative.brief"} <= kinds
    for _ in range(2):
        orch.advance_run(run["run_id"], TENANT)
        worker.drain()
        assert _refs() == refs, "冻结引用不得漂移"
