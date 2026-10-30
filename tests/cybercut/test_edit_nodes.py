"""WB-04 编辑节点测试（handler 级，无数据库）。"""
from __future__ import annotations

import json
from typing import Any

import pytest

from cybercut.contracts.editing import TemplateBinding, TimelineSpec
from cybercut.contracts.errors import NodeExecutionError
from cybercut.creative.policy import HYBRID_POLICY, ApprovalPolicy
from cybercut.nodes.edit_nodes import (
    CANCELLED,
    EDIT_PLAN_KIND,
    INPUT_MISSING,
    TEMPLATE_BIND_KIND,
    TEMPLATE_NOT_FOUND,
    EditPlanHandler,
    TemplateBindHandler,
)
from tests.cybercut.test_creative_nodes import (
    CREATIVE_INPUT,
    RUN,
    _Env,
    _run_chain,
)

_STAGED_PATHS = {"edit.plan": "editing/plan.json",
                 "template.bind": "editing/bound.json"}


def _staged(env: _Env, kind: str) -> dict:
    path = env.attempt_dir / _STAGED_PATHS[kind]
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def env(data_root):
    from tests.cybercut.test_creative_nodes import _Env

    return _Env(data_root)


@pytest.fixture
def chained(env):
    """跑完创作四节点，返回 (env, chain 结果)。"""
    return env, _run_chain(env)


def _edit_input(env: _Env, *, template_id: str | None = "product_highlight",
                **overrides) -> dict:
    payload = env.input_json()
    edit_input = {} if template_id is None else {"template_id": template_id}
    payload["creative_input"] = {
        **CREATIVE_INPUT,
        "edit_input": edit_input,
        **overrides,
    }
    return payload


# ══════════════════════════════════════════════════════════════════
# edit.plan
# ══════════════════════════════════════════════════════════════════


def test_edit_plan_publishes_deterministic_spec(env: _Env, chained) -> None:
    handler = EditPlanHandler()
    first = handler.execute(env.context("edit.plan"), _edit_input(env))
    payload_a = _staged(env, EDIT_PLAN_KIND)
    handler.execute(env.context("edit.plan"), _edit_input(env))
    payload_b = _staged(env, EDIT_PLAN_KIND)

    assert first.artifacts[0].kind == EDIT_PLAN_KIND
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b,
                                                              sort_keys=True)
    spec = TimelineSpec.model_validate(payload_a)
    assert spec.project_id == "proj-nodes" and spec.run_id == RUN
    assert spec.clips, "阈值 0.1 下应有已匹配片段"
    assert spec.total_frames == sum(c.duration_frames for c in spec.clips)
    assert spec.model_id == "deterministic:edit-plan-v1"
    assert spec.prompt_version == "edit-plan.v1"


def test_edit_plan_clip_lineage_points_to_frozen_refs(env: _Env, chained) -> None:
    EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    spec = TimelineSpec.model_validate(_staged(env, EDIT_PLAN_KIND))
    refs = {r["kind"]: r["artifact_revision_id"] for r in env.refs}
    for clip in spec.clips:
        assert clip.storyboard_artifact_revision_id == refs["creative.storyboard"]
        assert clip.match_artifact_revision_id == refs["creative.match"]
        assert clip.analysis_artifact_revision_id == \
            refs[f"asset.analysis.{clip.asset_id}"]
    assert spec.storyboard_artifact_revision_id == refs["creative.storyboard"]
    assert spec.script_artifact_revision_id == refs["creative.script"]


def test_edit_plan_all_unmatched_when_no_candidates(env: _Env, chained) -> None:
    """全部镜头都无候选 ⇒ 全部进入 unresolved，绝不凑数。"""
    # storyboard 修改候选为不存在的 segment：match 会全部落空
    # （match handler 对无效候选输出 invalid_reference 且不选中）
    story = env.staged("creative.storyboard")
    for shot in story["shots"]:
        shot["candidate_segment_ids"] = ["ghost#0000"]
    env.add(story, kind="creative.storyboard")
    _run_chain_match_only(env)
    EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    spec = TimelineSpec.model_validate(_staged(env, EDIT_PLAN_KIND))
    assert spec.clips == []
    assert len(spec.unresolved_shots) == len(story["shots"])
    assert any(w.startswith("UNMATCHED_SHOT:") for w in spec.warnings)


def _run_chain_match_only(env: _Env) -> None:
    """只重跑 match（storyboard 已被篡改）。"""
    from cybercut.nodes.creative_nodes import CreativeMatchHandler

    CreativeMatchHandler().execute(env.context("creative.match"),
                                   _edit_input(env))
    env.add(env.staged("creative.match"), kind="creative.match")


def test_image_clip_warns_about_no_source_duration(env: _Env, chained) -> None:
    EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    spec = TimelineSpec.model_validate(_staged(env, EDIT_PLAN_KIND))
    for clip in spec.clips:
        if clip.media_kind == "image":
            assert (clip.source_in_us, clip.source_out_us) == (0, 0)
            assert "IMAGE_NO_SOURCE_DURATION" in clip.warnings


def test_edit_plan_rejects_frozen_identity_mismatch(env: _Env, chained) -> None:
    """冻结产物的 run_id 与当前 Run 不一致必须拒绝。"""
    tampered = dict(env.staged("creative.storyboard"))
    tampered["run_id"] = "run-other"
    env.add(tampered, kind="creative.storyboard")
    with pytest.raises(NodeExecutionError) as excinfo:
        EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    assert excinfo.value.error_code == "EDIT_FACT_INVALID"


def test_edit_plan_requires_all_frozen_refs(env: _Env, chained) -> None:
    payload = _edit_input(env)
    payload["input_artifacts"] = [
        r for r in payload["input_artifacts"]
        if r["kind"] != "creative.brief"]
    with pytest.raises(NodeExecutionError) as excinfo:
        EditPlanHandler().execute(env.context("edit.plan"), payload)
    assert excinfo.value.error_code == INPUT_MISSING


def test_edit_plan_honours_cancel(env: _Env, chained) -> None:
    with pytest.raises(NodeExecutionError) as excinfo:
        EditPlanHandler().execute(env.context("edit.plan", cancel=True),
                                  _edit_input(env))
    assert excinfo.value.error_code == CANCELLED


def test_edit_plan_awaiting_approval_follows_policy(env: _Env, chained) -> None:
    auto = EditPlanHandler().execute(env.context("edit.plan"),
                                     _edit_input(env))
    assert auto.awaiting_approval is False
    hybrid = EditPlanHandler().execute(
        env.context("edit.plan"),
        _edit_input(env) | {
            "approval_policy": ApprovalPolicy(
                mode=HYBRID_POLICY.mode,
                gates=("edit.plan",)).to_json()})
    assert hybrid.awaiting_approval is True


# ══════════════════════════════════════════════════════════════════
# template.bind
# ══════════════════════════════════════════════════════════════════


def _bind_after_plan(env: _Env, **overrides) -> tuple[TemplateBinding, Any]:
    EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    env.add(_staged(env, EDIT_PLAN_KIND), kind=EDIT_PLAN_KIND)
    result = TemplateBindHandler().execute(
        env.context("template.bind"), _edit_input(env, **overrides))
    return TemplateBinding.model_validate(_staged(env, TEMPLATE_BIND_KIND)), result


def test_template_bind_default_selection_is_deterministic(env: _Env, chained) -> None:
    payload = _edit_input(env, template_id=None)
    EditPlanHandler().execute(env.context("edit.plan"), payload)
    env.add(_staged(env, EDIT_PLAN_KIND), kind=EDIT_PLAN_KIND)
    TemplateBindHandler().execute(
        env.context("template.bind"), _edit_input(env, template_id=None))
    binding = TemplateBinding.model_validate(_staged(env, TEMPLATE_BIND_KIND))
    payload2 = _edit_input(env)
    payload2["creative_input"] = {**CREATIVE_INPUT}
    TemplateBindHandler().execute(env.context("template.bind"), payload2)
    payload_a = _staged(env, TEMPLATE_BIND_KIND)
    TemplateBindHandler().execute(env.context("template.bind"), payload2)
    payload_b = _staged(env, TEMPLATE_BIND_KIND)
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b,
                                                              sort_keys=True)
    assert binding.selection_reason == "default:first_by_template_id"
    assert binding.template_id == "brand_story"  # 支持 9:16 的最小 template_id


def test_template_bind_explicit_template(env: _Env, chained) -> None:
    binding, _ = _bind_after_plan(env)
    # _edit_input 默认指定 product_highlight
    assert binding.template_id == "product_highlight"
    assert binding.selection_reason == "explicit:frozen_run_input"


def test_template_bind_unknown_template_fails_closed(env: _Env, chained) -> None:
    """指定的 template_id 不存在：fail-closed，不做静默回退。"""
    bind_payload = _edit_input(env, template_id="nope")
    # edit.plan 本身不受模板影响
    EditPlanHandler().execute(env.context("edit.plan"),
                              _edit_input(env, template_id=None))
    env.add(_staged(env, EDIT_PLAN_KIND), kind=EDIT_PLAN_KIND)
    bind_payload = _edit_input(env, template_id="nope")
    with pytest.raises(NodeExecutionError) as excinfo:
        TemplateBindHandler().execute(env.context("template.bind"), bind_payload)
    assert excinfo.value.error_code == TEMPLATE_NOT_FOUND


def test_template_bind_never_rewrites_copy(env: _Env, chained) -> None:
    """绑定不得改写事实性文案：时间线文本与脚本逐字一致。"""
    EditPlanHandler().execute(env.context("edit.plan"), _edit_input(env))
    before = _staged(env, EDIT_PLAN_KIND)
    env.add(before, kind=EDIT_PLAN_KIND)
    TemplateBindHandler().execute(env.context("template.bind"),
                                  _edit_input(env))
    after = _staged(env, EDIT_PLAN_KIND)
    binding = TemplateBinding.model_validate(_staged(env, TEMPLATE_BIND_KIND))
    # 绑定不得改写时间线（含全部事实性文案）
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    # 绑定产物本身不携带任何文案字段（只携带槽位映射与未满足项）
    assert "text_intents" not in binding.model_dump(mode="json")
    assert binding.accepted is True


def _read_script(env: _Env):
    from cybercut.contracts.creative import ScriptRevision

    return ScriptRevision.model_validate(env.staged("creative.script"))


def test_template_bind_awaiting_approval_follows_policy(env: _Env, chained) -> None:
    _, result = _bind_after_plan(env)
    assert result.awaiting_approval is False


def test_both_edit_nodes_in_manual_mode_await(env: _Env, chained) -> None:
    manual = ApprovalPolicy(
        mode=HYBRID_POLICY.mode,
        gates=("creative.brief", "creative.script", "creative.storyboard",
               "creative.match", "edit.plan", "template.bind"))
    policy_payload = _edit_input(env) | {"approval_policy": manual.to_json()}
    plan = EditPlanHandler().execute(env.context("edit.plan"), policy_payload)
    assert plan.awaiting_approval is True
    env.add(_staged(env, EDIT_PLAN_KIND), kind=EDIT_PLAN_KIND)
    bound = TemplateBindHandler().execute(
        env.context("template.bind"),
        _edit_input(env) | {"approval_policy": manual.to_json()})
    assert bound.awaiting_approval is True
