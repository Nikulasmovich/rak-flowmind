"""WB-03 创作层契约 + 确定性匹配 + 校验器（纯单元，无数据库）。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from cybercut.contracts.creative import (
    MATCH_ALGORITHM_VERSION,
    AssetMatchResult,
    CreativeBrief,
    EvidenceRef,
    MatchCandidate,
    ScoreBreakdown,
    ScriptRevision,
    SellingPoint,
    ShotMatch,
    StoryboardRevision,
)
from cybercut.contracts.errors import ValidationError
from cybercut.creative import validators
from cybercut.creative.fake_provider import FakeCreativeProvider
from cybercut.creative.matching import (
    DEFAULT_SELECTION_THRESHOLD,
    WEIGHTS,
    build_match_result,
    iter_run_segments,
    match_storyboard,
    parse_segment_id,
    segment_id,
    tokenize,
    total_score,
)
from cybercut.creative.policy import (
    AUTO_POLICY,
    CREATIVE_NODE_KEYS,
    CREATIVE_STAGES,
    EDIT_NODE_KEYS,
    HYBRID_POLICY,
    MANUAL_POLICY,
    PLAN_NODE_KEYS,
    RENDER_NODE_KEYS,
    ApprovalMode,
    ApprovalPolicy,
    PolicyPresets,
    creative_descendants,
    policy_for,
)
from cybercut.creative.prompts import PROMPT_VERSIONS, build_prompt, prompt_version
from cybercut.creative.provider import (
    GenerationRequest,
    GenerationResult,
    ProviderError,
    provider_model_id,
)
from cybercut.creative.traces import (
    RATIONALE_CODES,
    assert_no_chain_of_thought,
    digest,
    step,
    summarize,
)

A1 = "a1" * 8
A2 = "b2" * 8
PROJECT = "proj-creative"
RUN = "run-creative"


# ══════════════════════════════════════════════════════════════════
# 夹具构造
# ══════════════════════════════════════════════════════════════════


def analysis(asset_id: str, *, duration_us: int | None, segments: int,
             transcript: list[dict] | None = None,
             width: int = 1280, height: int = 720) -> dict:
    kind = "image" if duration_us is None else "video"
    segs = []
    for index in range(segments):
        if duration_us is None:
            start = end = 0
            time_based = False
        else:
            step_us = duration_us // max(1, segments)
            start = index * step_us
            end = duration_us if index == segments - 1 else (index + 1) * step_us
            time_based = True
        segs.append({
            "segment_index": index, "start_us": start, "end_us": end,
            "is_time_based": time_based, "source": "scene_detect" if time_based
            else "single_frame",
            "keyframe_artifact_revision_id": f"kf-{index}" if time_based else None,
        })
    return {
        "schema_version": "asset-analysis.v1", "asset_id": asset_id,
        "media_kind": kind, "duration_us": duration_us,
        "probe": {"has_audio": time_based_audio(segs), "has_video": True,
                  "video": {"width": width, "height": height,
                            "pix_fmt": "yuv420p"}},
        "segments": segs, "transcript": transcript or [],
        "keyframe_artifact_revision_ids": [],
    }


def time_based_audio(segs: list[dict]) -> bool:
    return any(s["is_time_based"] for s in segs)


def assets() -> list[dict]:
    return [
        {"asset_id": A1, "media_kind": "video", "original_filename": "clip.mp4",
         "source_relative_path": f"assets/{A1}/original/clip.mp4",
         "sha256": "a" * 64, "size_bytes": 10},
        {"asset_id": A2, "media_kind": "image", "original_filename": "poster.png",
         "source_relative_path": f"assets/{A2}/original/poster.png",
         "sha256": "b" * 64, "size_bytes": 20},
    ]


def analyses() -> list[dict]:
    return [
        analysis(A1, duration_us=10_000_000, segments=2,
                 transcript=[{"start_us": 0, "end_us": 5_000_000,
                              "text": "clip 产品演示"}]),
        analysis(A2, duration_us=None, segments=1, width=800, height=600),
    ]


def a_ref(asset_id: str) -> dict:
    return {"asset_id": asset_id, "artifact_kind": f"asset.analysis.{asset_id}",
            "segment_index": None}


def brief_payload(**overrides) -> dict:
    payload = {
        "brief_revision_id": "pending:brief-test", "revision_no": 1,
        "project_id": PROJECT, "run_id": RUN, "target_platform": "douyin",
        "aspect_ratio": "9:16", "target_duration_us": 10_000_000,
        "language": "zh", "audience": "受众", "objective": "推广",
        "core_message": "演示 2 个素材",
        "selling_points": [{"text": "clip 素材共 2 个可切分片段",
                            "evidence_refs": [a_ref(A1)]}],
        "call_to_action": "了解详情", "tone": "轻快", "visual_style": "实拍",
        "forbidden_claims": ["绝对第一"],
        "source_asset_ids": [A1, A2],
        "evidence_refs": [a_ref(A1), a_ref(A2)],
        "prompt_version": "creative-brief-prompt.v1", "model_id": "fake",
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def brief() -> CreativeBrief:
    return CreativeBrief.model_validate(brief_payload())


def script_payload() -> dict:
    return {
        "script_revision_id": "script-1", "project_id": PROJECT, "run_id": RUN,
        "revision_no": 1, "language": "zh", "hook": "演示 2 个素材",
        "sections": [
            {"section_id": "s0", "role": "hook", "narration": "演示 2 个素材",
             "on_screen_text": "开头", "target_duration_us": 4_000_000,
             "required_visual_intent": "开场画面", "evidence_refs": [a_ref(A1)]},
            {"section_id": "s1", "role": "cta", "narration": "了解详情",
             "on_screen_text": "行动", "target_duration_us": 6_000_000,
             "required_visual_intent": "收尾画面", "evidence_refs": [a_ref(A2)]},
        ],
        "total_target_duration_us": 10_000_000, "cta": "了解详情",
        "evidence_refs": [a_ref(A1)], "prompt_version": "p", "model_id": "fake",
        "warnings": [], "decision_trace": [],
    }


def script() -> ScriptRevision:
    return ScriptRevision.model_validate(script_payload())


def storyboard_payload(shots: list[dict] | None = None) -> dict:
    return {
        "storyboard_revision_id": "sb-1", "project_id": PROJECT, "run_id": RUN,
        "revision_no": 1,
        "shots": shots if shots is not None else [
            _shot(0, "s0", 4_000_000), _shot(1, "s1", 6_000_000),
        ],
        "total_target_duration_us": 10_000_000, "rhythm": "2 镜头",
        "evidence_refs": [a_ref(A1)], "prompt_version": "p", "model_id": "fake",
        "warnings": [], "decision_trace": [],
    }


def _shot(seq: int, section_id: str, duration_us: int,
          candidates: list[str] | None = None) -> dict:
    return {
        "shot_id": f"shot-{seq:04d}", "section_id": section_id,
        "sequence_no": seq, "intent": "clip 产品演示", "visual_requirement": "演示",
        "subject": "产品", "composition": "中心", "camera_motion": "固定",
        "emotion": "中性", "target_duration_us": duration_us,
        "source_constraints": [], "transition_hint": "cut",
        "candidate_segment_ids": candidates if candidates is not None else [
            segment_id(A1, 0), segment_id(A1, 1), segment_id(A2, 0)],
        "evidence_refs": [a_ref(A1)],
    }


def storyboard() -> StoryboardRevision:
    return StoryboardRevision.model_validate(storyboard_payload())


# ══════════════════════════════════════════════════════════════════
# 契约
# ══════════════════════════════════════════════════════════════════


def test_evidence_ref_kind_must_mention_asset() -> None:
    with pytest.raises(PydanticValidationError):
        EvidenceRef(asset_id=A1, artifact_kind="asset.analysis.other")


def test_evidence_ref_rejects_extra_fields() -> None:
    with pytest.raises(PydanticValidationError):
        EvidenceRef(asset_id=A1, artifact_kind=f"asset.analysis.{A1}",
                    unexpected=1)


def test_brief_target_duration_must_be_positive() -> None:
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(brief_payload(target_duration_us=0))


def test_brief_aspect_ratio_shape() -> None:
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(brief_payload(aspect_ratio="9x16"))
    assert CreativeBrief.model_validate(
        brief_payload(aspect_ratio="16:9")).aspect_ratio == "16:9"


def test_brief_rejects_duplicate_source_assets() -> None:
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(
            brief_payload(source_asset_ids=[A1, A1]))


def test_brief_rejects_evidence_outside_source_assets() -> None:
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(brief_payload(
            source_asset_ids=[A1], evidence_refs=[a_ref(A2)]))


def test_selling_point_requires_evidence() -> None:
    with pytest.raises(PydanticValidationError):
        SellingPoint(text="卖点", evidence_refs=[])


def test_brief_requires_prompt_and_model_provenance() -> None:
    payload = brief_payload()
    del payload["model_id"]
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(payload)


def test_script_total_must_equal_section_sum() -> None:
    payload = script_payload()
    payload["total_target_duration_us"] = 9_000_000
    with pytest.raises(PydanticValidationError):
        ScriptRevision.model_validate(payload)


def test_script_section_duration_must_be_positive() -> None:
    payload = script_payload()
    payload["sections"][0]["target_duration_us"] = 0
    with pytest.raises(PydanticValidationError):
        ScriptRevision.model_validate(payload)


def test_script_rejects_duplicate_section_ids() -> None:
    payload = script_payload()
    payload["sections"][1]["section_id"] = payload["sections"][0]["section_id"]
    with pytest.raises(PydanticValidationError):
        ScriptRevision.model_validate(payload)


def test_script_rejects_unknown_role() -> None:
    payload = script_payload()
    payload["sections"][0]["role"] = "intro"
    with pytest.raises(PydanticValidationError):
        ScriptRevision.model_validate(payload)


def test_storyboard_sequence_must_be_deterministic() -> None:
    payload = storyboard_payload()
    payload["shots"][0]["sequence_no"] = 1
    payload["shots"][1]["sequence_no"] = 0
    with pytest.raises(PydanticValidationError):
        StoryboardRevision.model_validate(payload)


def test_storyboard_total_must_equal_shot_sum() -> None:
    payload = storyboard_payload()
    payload["total_target_duration_us"] = 1
    with pytest.raises(PydanticValidationError):
        StoryboardRevision.model_validate(payload)


def test_storyboard_rejects_duplicate_candidates() -> None:
    payload = storyboard_payload()
    payload["shots"][0]["candidate_segment_ids"] = [segment_id(A1, 0)] * 2
    with pytest.raises(PydanticValidationError):
        StoryboardRevision.model_validate(payload)


def test_storyboard_requires_candidates() -> None:
    payload = storyboard_payload()
    payload["shots"][0]["candidate_segment_ids"] = []
    with pytest.raises(PydanticValidationError):
        StoryboardRevision.model_validate(payload)


def test_match_candidates_must_be_descending() -> None:
    low = MatchCandidate(segment_id="x#0000", asset_id="x", score=0.1,
                         breakdown=ScoreBreakdown(
                             semantic_score=0, quality_score=0, duration_score=0,
                             continuity_score=0, repetition_penalty=0,
                             evidence_score=0))
    high = MatchCandidate(segment_id="y#0000", asset_id="y", score=0.9,
                          breakdown=low.breakdown)
    with pytest.raises(PydanticValidationError):
        ShotMatch(shot_id="s", candidates=[low, high])


def test_match_selected_must_be_candidate() -> None:
    candidate = MatchCandidate(
        segment_id="x#0000", asset_id="x", score=0.9,
        breakdown=ScoreBreakdown(
            semantic_score=0.9, quality_score=0.9, duration_score=1,
            continuity_score=1, repetition_penalty=0, evidence_score=1))
    with pytest.raises(PydanticValidationError):
        ShotMatch(shot_id="s", candidates=[candidate],
                  selected_segment_id="other#0000", score=0.9,
                  score_breakdown=candidate.breakdown)


def test_match_selected_requires_score() -> None:
    candidate = MatchCandidate(
        segment_id="x#0000", asset_id="x", score=0.9,
        breakdown=ScoreBreakdown(
            semantic_score=0.9, quality_score=0.9, duration_score=1,
            continuity_score=1, repetition_penalty=0, evidence_score=1))
    with pytest.raises(PydanticValidationError):
        ShotMatch(shot_id="s", candidates=[candidate],
                  selected_segment_id="x#0000")


def test_match_rejects_duplicate_selection() -> None:
    breakdown = ScoreBreakdown(
        semantic_score=0.9, quality_score=0.9, duration_score=1,
        continuity_score=1, repetition_penalty=0, evidence_score=1)
    candidate = MatchCandidate(segment_id="x#0000", asset_id="x", score=0.9,
                               breakdown=breakdown)
    shot = ShotMatch(shot_id="s", candidates=[candidate],
                     selected_segment_id="x#0000", score=0.9,
                     score_breakdown=breakdown)
    with pytest.raises(PydanticValidationError):
        AssetMatchResult(match_revision_id="pending:match-test",
                         project_id=PROJECT, run_id=RUN, revision_no=1,
                         selection_threshold=0.5,
                         shots=[shot, shot.model_copy(update={"shot_id": "s2"})])


def test_score_breakdown_rejects_out_of_range() -> None:
    with pytest.raises(PydanticValidationError):
        ScoreBreakdown(semantic_score=1.5, quality_score=0, duration_score=0,
                       continuity_score=0, repetition_penalty=0,
                       evidence_score=0)


def test_score_breakdown_rejects_nan() -> None:
    with pytest.raises(PydanticValidationError):
        ScoreBreakdown(semantic_score=float("nan"), quality_score=0,
                       duration_score=0, continuity_score=0,
                       repetition_penalty=0, evidence_score=0)


def test_weights_sum_to_one() -> None:
    positive = sum(v for k, v in WEIGHTS.items() if k != "repetition")
    assert abs(positive - 1.0) < 1e-9


def test_total_score_uses_weights() -> None:
    breakdown = ScoreBreakdown(
        semantic_score=1.0, quality_score=1.0, duration_score=1.0,
        continuity_score=1.0, repetition_penalty=0.0, evidence_score=1.0)
    assert total_score(breakdown) == 1.0
    penalized = breakdown.model_copy(update={"repetition_penalty": 1.0})
    assert total_score(penalized) == pytest.approx(0.5)


# ══════════════════════════════════════════════════════════════════
# 审批策略
# ══════════════════════════════════════════════════════════════════


def test_auto_policy_has_no_gate() -> None:
    assert AUTO_POLICY.mode == ApprovalMode.AUTO
    for key in CREATIVE_NODE_KEYS:
        assert AUTO_POLICY.requires_approval(key) is False


def test_hybrid_policy_gates_script_only() -> None:
    assert HYBRID_POLICY.requires_approval("creative.script") is True
    assert HYBRID_POLICY.requires_approval("creative.brief") is False


def test_policy_rejects_unknown_gate() -> None:
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode=ApprovalMode.HYBRID, gates=("unknown.node",))


def test_policy_rejects_gates_in_auto_mode() -> None:
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode=ApprovalMode.AUTO, gates=("creative.script",))


def test_policy_roundtrip() -> None:
    assert ApprovalPolicy.from_json(HYBRID_POLICY.to_json()) == HYBRID_POLICY
    assert ApprovalPolicy.from_json(None) == AUTO_POLICY
    with pytest.raises(ValidationError):
        ApprovalPolicy.from_json({"mode": "bogus"})


def test_policy_for_and_presets() -> None:
    assert policy_for("auto") == AUTO_POLICY
    assert policy_for("hybrid").gates == ("creative.script",)
    assert PolicyPresets().get("hybrid") == HYBRID_POLICY
    assert PolicyPresets().get("manual") == MANUAL_POLICY
    with pytest.raises(ValidationError):
        PolicyPresets().get("nope")


def test_manual_policy_gates_all_creative_nodes() -> None:
    """WB-05：manual 默认对全部规划+渲染节点审批。"""
    assert MANUAL_POLICY.mode == ApprovalMode.MANUAL
    assert set(MANUAL_POLICY.gates) == set(PLAN_NODE_KEYS)
    for key in PLAN_NODE_KEYS:
        assert MANUAL_POLICY.requires_approval(key) is True
    assert policy_for("manual").gates == PLAN_NODE_KEYS
    assert policy_for("manual", gates=("creative.match",)).gates == (
        "creative.match",)
    assert (set(CREATIVE_NODE_KEYS) | set(EDIT_NODE_KEYS)
            | set(RENDER_NODE_KEYS) == set(PLAN_NODE_KEYS))


def test_non_auto_policy_requires_explicit_gates() -> None:
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode=ApprovalMode.HYBRID, gates=())
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode=ApprovalMode.MANUAL, gates=())


def test_manual_policy_is_frozen_and_roundtrips() -> None:
    assert ApprovalPolicy.from_json(MANUAL_POLICY.to_json()) == MANUAL_POLICY
    assert MANUAL_POLICY.to_json()["mode"] == "manual"


def test_creative_descendants_only_covers_creative_chain() -> None:
    """下游失效沿完整规划链传递，绝不波及媒体层。"""
    assert creative_descendants("creative.brief") == (
        "creative.script", "creative.storyboard", "creative.match",
        "edit.plan", "template.bind", "render.narration",
        "render.composite", "render.qc")
    assert creative_descendants("creative.script") == (
        "creative.storyboard", "creative.match", "edit.plan", "template.bind",
        "render.narration", "render.composite", "render.qc")
    assert creative_descendants("creative.storyboard") == (
        "creative.match", "edit.plan", "template.bind",
        "render.narration", "render.composite", "render.qc")
    assert creative_descendants("creative.match") == (
        "edit.plan", "template.bind", "render.narration",
        "render.composite", "render.qc")
    assert creative_descendants("edit.plan") == (
        "template.bind", "render.narration", "render.composite", "render.qc")
    assert creative_descendants("template.bind") == (
        "render.narration", "render.composite", "render.qc")
    assert creative_descendants("render.narration") == (
        "render.composite", "render.qc")
    assert creative_descendants("render.composite") == ("render.qc",)
    assert creative_descendants("render.qc") == ()
    assert creative_descendants("render.narration") == (
        "render.composite", "render.qc")
    assert creative_descendants("render.composite") == ("render.qc",)
    assert creative_descendants("render.qc") == ()
    for media in ("asset_probe", "asset_normalize", "scene_detect",
                  "speech_transcribe", "asset_analysis"):
        assert creative_descendants(media) == ()
    assert CREATIVE_STAGES == PLAN_NODE_KEYS


# ══════════════════════════════════════════════════════════════════
# 决策摘要 / 提示词
# ══════════════════════════════════════════════════════════════════


def test_trace_step_rejects_unregistered_rationale() -> None:
    with pytest.raises(ValueError):
        step("s", decision="d", rationale_code="made_up")


def test_trace_has_no_chain_of_thought() -> None:
    record = step("s", decision="d", rationale_code="deterministic_template",
                  inputs={"a": 1}, outputs={"b": 2})
    assert_no_chain_of_thought([record])
    with pytest.raises(ValueError):
        assert_no_chain_of_thought([{**record, "chain_of_thought": "..."}])


def test_trace_digest_is_stable_and_order_insensitive() -> None:
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_trace_summarize_is_deterministic() -> None:
    a = step("b", decision="d", rationale_code="default_applied")
    b = step("a", decision="d", rationale_code="default_applied")
    assert summarize([a, b]) == summarize([b, a])
    assert [s["step"] for s in summarize([a, b])] == ["a", "b"]


def test_every_rationale_code_is_used_or_registered() -> None:
    assert "deterministic_template" in RATIONALE_CODES
    assert isinstance(RATIONALE_CODES, frozenset)


def test_prompt_versions_and_determinism() -> None:
    assert set(PROMPT_VERSIONS) == {"brief", "script", "storyboard"}
    assert prompt_version("brief") == PROMPT_VERSIONS["brief"]
    with pytest.raises(ValueError):
        prompt_version("unknown")
    first = build_prompt("script", {"x": 1, "y": [2, 3]})
    second = build_prompt("script", {"y": [2, 3], "x": 1})
    assert first == second
    assert PROMPT_VERSIONS["script"] in first


# ══════════════════════════════════════════════════════════════════
# 确定性匹配
# ══════════════════════════════════════════════════════════════════


def test_segment_id_roundtrip() -> None:
    sid = segment_id(A1, 3)
    assert sid == f"{A1}#0003"
    assert parse_segment_id(sid) == (A1, 3)
    with pytest.raises(ValueError):
        parse_segment_id("bad-id")


def test_tokenize_handles_ascii_and_cjk() -> None:
    tokens = tokenize("clip 产品演示 ab")
    assert "clip" in tokens and "ab" in tokens
    assert "产品" in tokens and "演示" in tokens


def test_iter_run_segments_includes_filename_tokens() -> None:
    segments = iter_run_segments(analyses(), assets())
    assert segment_id(A1, 0) in segments
    assert "clip" in segments[segment_id(A1, 0)].keyword_pool


def test_match_is_deterministic() -> None:
    payload_a = match_storyboard(storyboard_payload(), analyses(), assets=assets())
    payload_b = match_storyboard(storyboard_payload(), analyses(), assets=assets())
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b,
                                                              sort_keys=True)


def test_match_selection_is_highly_scored_for_aligned_fixture() -> None:
    """素材与意图高度对齐时，两个镜头都应选出素材且不重复。"""
    analyses_fixture = [
        analysis(A1, duration_us=10_000_000, segments=2,
                 transcript=[{"start_us": 0, "end_us": 5_000_000,
                              "text": "clip 产品演示"},
                             {"start_us": 5_000_000, "end_us": 10_000_000,
                              "text": "clip 产品演示"}]),
        analysis(A2, duration_us=None, segments=1, width=1920, height=1080),
    ]
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=storyboard_payload(), analyses=analyses_fixture,
        assets=assets())
    selected = [s.selected_segment_id for s in result.shots]
    assert all(s is not None for s in selected), result.warnings
    assert len(set(selected)) == len(selected), "禁止重复使用同一 segment"


def test_match_never_reuses_selected_segment() -> None:
    shots = [_shot(0, "s0", 4_000_000), _shot(1, "s1", 6_000_000)]
    shots[1]["candidate_segment_ids"] = [segment_id(A1, 0)]  # 只允许同一段
    payload = storyboard_payload(shots)
    payload["total_target_duration_us"] = 10_000_000
    story = StoryboardRevision.model_validate(payload)
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=story.model_dump(mode="json"), analyses=analyses(),
        assets=assets(), threshold=0.1)
    selected = [s.selected_segment_id for s in result.shots]
    assert selected[1] is None, "已被占用的 segment 不得再被选中"
    assert any("MATCH_ALL_CANDIDATES_USED" in w for w in result.warnings)


def test_match_rejects_invalid_segment_reference() -> None:
    shots = [_shot(0, "s0", 4_000_000,
                   candidates=[segment_id(A1, 0), "ghost#0000"]),
             _shot(1, "s1", 6_000_000)]
    payload = storyboard_payload(shots)
    result = match_storyboard(payload, analyses(), assets=assets())
    assert any("INVALID_SEGMENT_REFERENCE" in w for w in result["warnings"])
    rejected = {r["segment_id"]: r["reason"]
                for r in result["shots"][0]["rejected_candidates"]}
    assert rejected["ghost#0000"] == "invalid_reference"


def test_match_below_threshold_yields_warning_not_fabrication() -> None:
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=storyboard_payload(), analyses=analyses(), assets=assets(),
        threshold=0.99)
    assert all(s.selected_segment_id is None for s in result.shots)
    assert all(s.score is None for s in result.shots)
    assert any("MATCH_BELOW_THRESHOLD" in w for w in result.warnings)
    assert result.unselected_shot_ids


def test_match_emits_full_score_breakdown() -> None:
    result = match_storyboard(storyboard_payload(), analyses(), assets=assets())
    for shot in result["shots"]:
        for candidate in shot["candidates"]:
            breakdown = candidate["breakdown"]
            assert set(breakdown) >= {
                "semantic_score", "quality_score", "duration_score",
                "continuity_score", "repetition_penalty", "evidence_score",
                "algorithm_version",
            }
            assert breakdown["algorithm_version"] == MATCH_ALGORITHM_VERSION


def test_match_candidates_are_sorted_desc() -> None:
    result = match_storyboard(storyboard_payload(), analyses(), assets=assets())
    for shot in result["shots"]:
        scores = [c["score"] for c in shot["candidates"]]
        assert scores == sorted(scores, reverse=True)


def test_match_rejects_unknown_algorithm_version() -> None:
    with pytest.raises(ValueError):
        match_storyboard(storyboard_payload(), analyses(), assets=assets(),
                         algorithm_version="v-next")


def test_match_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        match_storyboard(storyboard_payload(), analyses(), assets=assets(),
                         threshold=1.5)


def test_unused_segments_reported() -> None:
    result = match_storyboard(storyboard_payload(), analyses(), assets=assets())
    used = {s["selected_segment_id"] for s in result["shots"]}
    assert set(result["unused_segment_ids"]) == \
        {segment_id(A1, 0), segment_id(A1, 1), segment_id(A2, 0)} - used


def test_default_threshold_is_sane() -> None:
    assert 0.0 < DEFAULT_SELECTION_THRESHOLD < 1.0


# ══════════════════════════════════════════════════════════════════
# 校验器（反虚构）
# ══════════════════════════════════════════════════════════════════


def test_allowed_numbers_from_analyses() -> None:
    allowed = validators.allowed_numbers_from_analyses(analyses())
    assert "2" in allowed  # A1 的片段数
    assert "10.0" in allowed  # 10s 时长
    assert validators.unsupported_numbers("时长 10.0 秒", allowed) == []
    assert validators.unsupported_numbers("销量提升 99 倍", allowed) == ["99"]


def test_validate_brief_accepts_grounded_brief() -> None:
    validators.validate_brief(
        brief(), frozen_asset_ids={A1, A2}, analyses=analyses(),
        brief_input={"objective": "推广"}, expected_project_id=PROJECT,
        expected_run_id=RUN)


def test_validate_brief_rejects_fabricated_number() -> None:
    payload = brief_payload()
    payload["selling_points"][0]["text"] = "clip 素材共 2 个片段，销量 77 万"
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            CreativeBrief.model_validate(payload), frozen_asset_ids={A1, A2},
            analyses=analyses())
    assert excinfo.value.details["code"] == "UNSUPPORTED_FACT"


def test_validate_brief_rejects_forbidden_claim() -> None:
    payload = brief_payload(core_message="绝对第一的演示 2 个素材")
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            CreativeBrief.model_validate(payload), frozen_asset_ids={A1, A2},
            analyses=analyses())
    assert excinfo.value.details["code"] == "FORBIDDEN_CLAIM"


def test_validate_brief_rejects_asset_outside_run() -> None:
    """模型层已保证「证据 ⊆ source_asset_ids」；此处校验「source ⊆ Run 冻结」。"""
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            brief(), frozen_asset_ids={A1}, analyses=analyses())
    assert excinfo.value.details["code"] == "ASSET_OUT_OF_RUN"
    assert excinfo.value.details["extra"] == [A2]


def test_model_rejects_evidence_outside_source_assets() -> None:
    """防御性：证据指向非 source 素材时，模型层即拒绝（纵深防御）。"""
    payload = brief_payload()
    payload["evidence_refs"][1]["asset_id"] = "zz" * 8
    payload["evidence_refs"][1]["artifact_kind"] = f"asset.analysis.{'zz' * 8}"
    with pytest.raises(PydanticValidationError):
        CreativeBrief.model_validate(payload)


def test_validate_brief_rejects_unreachable_evidence() -> None:
    payload = brief_payload()
    payload["evidence_refs"][0]["artifact_kind"] = f"asset.missing.{A1}"
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            CreativeBrief.model_validate(payload), frozen_asset_ids={A1, A2},
            analyses=analyses())
    assert excinfo.value.details["code"] == "EVIDENCE_UNREACHABLE"


def test_validate_script_accepts_grounded_script() -> None:
    validators.validate_script(script(), brief=brief(), analyses=analyses())


def test_validate_script_rejects_unsupported_number() -> None:
    payload = script_payload()
    payload["sections"][0]["narration"] = "销量提升 88 倍"
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_script(
            ScriptRevision.model_validate(payload), brief=brief(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "UNSUPPORTED_FACT"


def test_validate_script_rejects_forbidden_claim() -> None:
    payload = script_payload()
    payload["sections"][1]["on_screen_text"] = "绝对第一"
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_script(
            ScriptRevision.model_validate(payload), brief=brief(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "FORBIDDEN_CLAIM"


def test_validate_storyboard_accepts_covering_storyboard() -> None:
    validators.validate_storyboard(storyboard(), script=script(),
                                  analyses=analyses())


def test_validate_storyboard_rejects_uncovered_section() -> None:
    payload = storyboard_payload([
        _shot(0, "s0", 4_000_000), _shot(1, "s0", 6_000_000)])
    payload["total_target_duration_us"] = 10_000_000
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            StoryboardRevision.model_validate(payload), script=script(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "SECTION_NOT_COVERED"


def test_validate_storyboard_rejects_unknown_section() -> None:
    payload = storyboard_payload([_shot(0, "s9", 4_000_000),
                                  _shot(1, "s1", 6_000_000)])
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            StoryboardRevision.model_validate(payload), script=script(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "UNKNOWN_SECTION"


def test_validate_storyboard_rejects_candidate_out_of_run() -> None:
    stories = [_shot(0, "s0", 4_000_000, candidates=["ghost#0000"]),
               _shot(1, "s1", 6_000_000)]
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            StoryboardRevision.model_validate(storyboard_payload(stories)),
            script=script(), analyses=analyses())
    assert excinfo.value.details["code"] == "CANDIDATE_OUT_OF_RUN"


def _frozen_kinds() -> set[str]:
    """当前 Run 冻结输入允许的产物 kind。"""
    return {f"asset.analysis.{A1}", f"asset.analysis.{A2}",
            f"asset.scenes.{A1}", f"asset.scenes.{A2}",
            f"asset.probe.{A1}", f"asset.probe.{A2}"}


def test_evidence_segment_index_must_exist() -> None:
    """WB-03R §5：segment_index 非空时必须真实存在于对应分析报告。"""
    payload = brief_payload(evidence_refs=[
        {"asset_id": A1, "artifact_kind": f"asset.analysis.{A1}",
         "segment_index": 9},
    ])
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            CreativeBrief.model_validate(payload), frozen_asset_ids={A1, A2},
            analyses=analyses())
    assert excinfo.value.details["code"] == "EVIDENCE_SEGMENT_MISSING"


def test_evidence_segment_index_exists_is_accepted() -> None:
    payload = brief_payload(evidence_refs=[
        {"asset_id": A1, "artifact_kind": f"asset.analysis.{A1}",
         "segment_index": 1},
    ], selling_points=[{"text": "clip 素材共 2 个可切分片段",
                        "evidence_refs": [
                            {"asset_id": A1,
                             "artifact_kind": f"asset.analysis.{A1}",
                             "segment_index": 1}]}])
    validators.validate_brief(
        CreativeBrief.model_validate(payload), frozen_asset_ids={A1, A2},
        analyses=analyses())


def test_evidence_must_come_from_frozen_inputs() -> None:
    """证据产物必须属于当前 Run 的冻结输入（input_artifacts）。"""
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_brief(
            brief(), frozen_asset_ids={A1, A2}, analyses=analyses(),
            frozen_kinds={f"asset.analysis.{A1}"})  # A2 的证据不在冻结输入中
    assert excinfo.value.details["code"] == "EVIDENCE_NOT_FROZEN"


def test_script_identity_must_match_context() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_script(
            script(), brief=brief(), analyses=analyses(),
            expected_project_id="other-project")
    assert excinfo.value.details["code"] == "PROJECT_MISMATCH"
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_script(
            script(), brief=brief(), analyses=analyses(),
            expected_run_id="other-run")
    assert excinfo.value.details["code"] == "RUN_MISMATCH"


def test_storyboard_top_level_evidence_is_checked() -> None:
    payload = storyboard_payload()
    payload["evidence_refs"] = [
        {"asset_id": A1, "artifact_kind": f"asset.analysis.{A1}",
         "segment_index": 7}]
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            StoryboardRevision.model_validate(payload), script=script(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "EVIDENCE_SEGMENT_MISSING"


def test_storyboard_shot_evidence_is_checked() -> None:
    shots = [_shot(0, "s0", 4_000_000), _shot(1, "s1", 6_000_000)]
    shots[0]["evidence_refs"] = [
        {"asset_id": A2, "artifact_kind": f"asset.analysis.{A2}",
         "segment_index": 5}]
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            StoryboardRevision.model_validate(storyboard_payload(shots)),
            script=script(), analyses=analyses())
    assert excinfo.value.details["code"] == "EVIDENCE_SEGMENT_MISSING"


def test_storyboard_identity_must_match_context() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_storyboard(
            storyboard(), script=script(), analyses=analyses(),
            expected_run_id="other-run")
    assert excinfo.value.details["code"] == "RUN_MISMATCH"


def test_script_evidence_out_of_run_is_rejected() -> None:
    payload = script_payload()
    payload["sections"][0]["evidence_refs"] = [
        {"asset_id": "zz" * 8,
         "artifact_kind": f"asset.analysis.{'zz' * 8}"}]
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_script(
            ScriptRevision.model_validate(payload), brief=brief(),
            analyses=analyses())
    assert excinfo.value.details["code"] == "EVIDENCE_OUT_OF_RUN"


def test_match_identity_must_match_context() -> None:
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=storyboard_payload(), analyses=analyses(), assets=assets(),
        threshold=0.1)
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_match(result, storyboard=storyboard(),
                                  analyses=analyses(),
                                  expected_project_id="other-project")
    assert excinfo.value.details["code"] == "PROJECT_MISMATCH"


def test_validate_match_accepts_result() -> None:
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=storyboard_payload(), analyses=analyses(), assets=assets(),
        threshold=0.1)
    validators.validate_match(result, storyboard=storyboard(),
                              analyses=analyses())


def test_validate_match_rejects_unknown_shot() -> None:
    result = build_match_result(
        revision_id="pending:match-test",
        project_id=PROJECT, run_id=RUN, revision_no=1,
        storyboard=storyboard_payload(), analyses=analyses(), assets=assets(),
        threshold=0.1)
    tampered = result.model_copy(update={
        "shots": [result.shots[0].model_copy(update={"shot_id": "ghost"})]
        + list(result.shots[1:])})
    with pytest.raises(ValidationError) as excinfo:
        validators.validate_match(tampered, storyboard=storyboard(),
                                  analyses=analyses())
    assert excinfo.value.details["code"] == "UNKNOWN_SHOT"


# ══════════════════════════════════════════════════════════════════
# Provider 契约
# ══════════════════════════════════════════════════════════════════


def test_provider_result_shape() -> None:
    result = GenerationResult(payload={"a": 1}, model_id="m",
                              prompt_version="p", warnings=[])
    assert set(result.__dict__) == {"payload", "model_id", "prompt_version",
                                    "warnings"}


def test_provider_model_id_required() -> None:
    class Broken:
        model_id = ""

        def generate(self, request):  # pragma: no cover - 不会被调用
            raise AssertionError

    with pytest.raises(ProviderError):
        provider_model_id(Broken())


def test_fake_provider_is_deterministic() -> None:
    provider = FakeCreativeProvider()
    request = GenerationRequest(
        kind="brief", prompt_version=prompt_version("brief"),
        context={"project_id": PROJECT, "run_id": RUN, "assets": assets(),
                 "analyses": analyses(),
                 "brief_input": {"objective": "推广",
                                 "target_duration_us": 10_000_000}})
    first = provider.generate(request)
    second = provider.generate(request)
    assert json.dumps(first.payload, sort_keys=True) == \
        json.dumps(second.payload, sort_keys=True)
    assert request.cache_key() == request.cache_key()
    assert first.model_id == provider.model_id


def test_fake_provider_rejects_unknown_kind() -> None:
    with pytest.raises(ProviderError) as excinfo:
        FakeCreativeProvider().generate(
            GenerationRequest(kind="unknown", prompt_version="p", context={}))
    assert excinfo.value.code == "UNSUPPORTED_KIND"


def test_fake_provider_rejects_empty_assets() -> None:
    with pytest.raises(ProviderError) as excinfo:
        FakeCreativeProvider().generate(
            GenerationRequest(kind="brief", prompt_version="p",
                              context={"assets": [], "analyses": []}))
    assert excinfo.value.code == "NO_ASSETS"
