"""WB-04 编辑规划契约 + 帧换算 + 模板清单/绑定（纯单元，无数据库）。"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from cybercut.contracts.creative import AssetMatchResult, ScoreBreakdown
from cybercut.contracts.editing import (
    FrameRate,
    TemplateBinding,
    TemplateManifest,
    TimelineSpec,
)
from cybercut.contracts.errors import ValidationError
from cybercut.editing.binder import bind_template
from cybercut.editing.frames import (
    duration_us_for_frames,
    frames_for_duration_us,
    frames_for_interval,
)
from cybercut.editing.planner import plan_timeline, select_frame_rate
from cybercut.editing.templates import (
    MANIFESTS,
    TEMPLATE_VERSION,
    get_manifest,
    list_manifests,
    select_default_manifest,
)

PROJECT = "proj-edit"
RUN = "run-edit"
A1 = "a1" * 8
A2 = "b2" * 8
FPS = FrameRate(num=30, den=1)

REFS = {
    "creative.storyboard": "rev-storyboard",
    "creative.match": "rev-match",
    "creative.script": "rev-script",
    "creative.brief": "rev-brief",
    f"asset.analysis.{A1}": "rev-analysis-a1",
    f"asset.analysis.{A2}": "rev-analysis-a2",
}


def analysis(asset_id: str, *, segments: int, duration_us: int | None,
             fps: tuple[int, int] = (30, 1), width: int = 1920,
             height: int = 1080) -> dict:
    segs = []
    for index in range(segments):
        step = (duration_us // segments) if duration_us else 0
        segs.append({
            "segment_index": index,
            "start_us": index * step if duration_us else 0,
            "end_us": ((index + 1) * step) if duration_us else 0,
            "is_time_based": bool(duration_us),
            "source": "scene_detect" if duration_us else "single_frame",
            "keyframe_artifact_revision_id": f"kf-{index}" if duration_us else None,
        })
    return {
        "schema_version": "asset-analysis.v1", "asset_id": asset_id,
        "media_kind": "video" if duration_us else "image",
        "duration_us": duration_us,
        "probe": {"has_audio": bool(duration_us), "has_video": True,
                  "video": {"width": width, "height": height,
                            "avg_frame_rate": {"num": fps[0], "den": fps[1]}}},
        "segments": segs,
        "transcript": ([{"start_us": 0, "end_us": step, "text": "clip 产品演示"}]
                       if duration_us else []),
        "keyframe_artifact_revision_ids": [], "warnings": [],
    }


ASSETS = [
    {"asset_id": A1, "media_kind": "video", "original_filename": "clip.mp4"},
    {"asset_id": A2, "media_kind": "image", "original_filename": "poster.png"},
]

ANALYSES = [analysis(A1, segments=4, duration_us=12_000_000),
            analysis(A2, segments=1, duration_us=None)]


def brief_payload() -> dict:
    return {
        "brief_revision_id": "pending:brief", "revision_no": 1,
        "project_id": PROJECT, "run_id": RUN, "target_platform": "douyin",
        "aspect_ratio": "9:16", "target_duration_us": 12_000_000,
        "language": "zh", "audience": "受众", "objective": "推广",
        "core_message": "演示 4 个片段",
        "selling_points": [{"text": "clip 素材共 4 个可切分片段",
                            "evidence_refs": [
                                {"asset_id": A1,
                                 "artifact_kind": f"asset.analysis.{A1}"}]}],
        "call_to_action": "了解详情", "tone": "轻快", "visual_style": "实拍",
        "forbidden_claims": [], "source_asset_ids": [A1, A2],
        "evidence_refs": [{"asset_id": A1,
                           "artifact_kind": f"asset.analysis.{A1}"},
                          {"asset_id": A2,
                           "artifact_kind": f"asset.analysis.{A2}"}],
        "prompt_version": "p", "model_id": "fake", "warnings": [],
    }


def script_payload() -> dict:
    return {
        "script_revision_id": "pending:script", "project_id": PROJECT,
        "run_id": RUN, "revision_no": 1, "language": "zh", "hook": "演示 4 个片段",
        "sections": [
            {"section_id": "s0", "role": "hook", "narration": "演示 4 个片段",
             "on_screen_text": "开头", "target_duration_us": 6_000_000,
             "required_visual_intent": "开场",
             "evidence_refs": [{"asset_id": A1,
                                "artifact_kind": f"asset.analysis.{A1}"}]},
            {"section_id": "s1", "role": "cta", "narration": "了解详情",
             "on_screen_text": "行动", "target_duration_us": 6_000_000,
             "required_visual_intent": "收尾",
             "evidence_refs": [{"asset_id": A2,
                                "artifact_kind": f"asset.analysis.{A2}"}]},
        ],
        "total_target_duration_us": 12_000_000, "cta": "了解详情",
        "evidence_refs": [{"asset_id": A1,
                           "artifact_kind": f"asset.analysis.{A1}"}],
        "prompt_version": "p", "model_id": "fake", "warnings": [],
    }


def storyboard_payload(shots: list[dict] | None = None) -> dict:
    return {
        "storyboard_revision_id": "pending:storyboard", "project_id": PROJECT,
        "run_id": RUN, "revision_no": 1,
        "shots": shots if shots is not None else [_shot(i) for i in range(4)],
        "total_target_duration_us": 12_000_000, "rhythm": "4 镜头",
        "evidence_refs": [{"asset_id": A1,
                           "artifact_kind": f"asset.analysis.{A1}"}],
        "prompt_version": "p", "model_id": "fake", "warnings": [],
    }


def _shot(seq: int, section_id: str = "s0", duration_us: int = 3_000_000,
          candidates: list[str] | None = None) -> dict:
    return {
        "shot_id": f"shot-{seq:04d}", "section_id": section_id,
        "sequence_no": seq, "intent": "clip 产品演示",
        "visual_requirement": "演示", "subject": "产品",
        "composition": "中心", "camera_motion": "固定", "emotion": "中性",
        "target_duration_us": duration_us, "source_constraints": [],
        "transition_hint": "cut",
        "candidate_segment_ids": candidates if candidates is not None else [
            "a1a1a1a1a1a1a1a1#0000", "a1a1a1a1a1a1a1a1#0001",
            "a1a1a1a1a1a1a1a1#0002", "a1a1a1a1a1a1a1a1#0003"],
        "evidence_refs": [{"asset_id": A1,
                           "artifact_kind": f"asset.analysis.{A1}"}],
    }


def match_payload(selected: list[str | None]) -> dict:
    shots = []
    for index, sel in enumerate(selected):
        if sel is None:
            shots.append({
                "shot_id": f"shot-{index:04d}", "candidates": [],
                "selected_segment_id": None, "score": None,
                "score_breakdown": None, "rejected_candidates": [],
                "warnings": ["NO_SELECTION"],
            })
        else:
            breakdown = ScoreBreakdown(
                semantic_score=0.9, quality_score=0.9, duration_score=1,
                continuity_score=1, repetition_penalty=0, evidence_score=1)
            shots.append({
                "shot_id": f"shot-{index:04d}",
                "candidates": [{"segment_id": sel, "asset_id": A1,
                                "score": 0.9, "breakdown":
                                    breakdown.model_dump(mode="json")}],
                "selected_segment_id": sel, "score": 0.9,
                "score_breakdown": breakdown.model_dump(mode="json"),
                "rejected_candidates": [], "warnings": [],
            })
    return {
        "match_revision_id": "pending:match", "project_id": PROJECT,
        "run_id": RUN, "revision_no": 1, "algorithm_version": "asset-match-v1",
        "selection_threshold": 0.1, "shots": shots, "unused_segment_ids": [],
        "warnings": [], "decision_trace": [],
    }


def build_spec(shots: list[dict] | None = None,
               selected: list[str | None] | None = None,
               analyses: list[dict] | None = None,
               script_payload_dict: dict | None = None) -> TimelineSpec:
    from cybercut.contracts.creative import CreativeBrief, ScriptRevision, StoryboardRevision

    storyboard = StoryboardRevision.model_validate(storyboard_payload(shots))
    match = AssetMatchResult.model_validate(
        match_payload(selected if selected is not None
                      else ["a1a1a1a1a1a1a1a1#0000", "a1a1a1a1a1a1a1a1#0001",
                            "a1a1a1a1a1a1a1a1#0002", "a1a1a1a1a1a1a1a1#0003"]))
    script = ScriptRevision.model_validate(script_payload_dict or script_payload())
    brief = CreativeBrief.model_validate(brief_payload())
    return plan_timeline(
        project_id=PROJECT, run_id=RUN, revision_no=1, revision_note=None,
        storyboard=storyboard, match=match, script=script, brief=brief,
        analyses=analyses if analyses is not None else ANALYSES,
        frozen_refs=REFS,
    )


# ══════════════════════════════════════════════════════════════════
# 帧率换算
# ══════════════════════════════════════════════════════════════════


def test_frame_rate_must_be_reduced() -> None:
    with pytest.raises(PydanticValidationError):
        FrameRate(num=60, den=30)
    assert FrameRate(num=30000, den=1001).as_tuple() == (30000, 1001)


def test_frame_conversion_is_exact_for_integral_fps() -> None:
    assert frames_for_duration_us(1_000_000, FPS) == 30
    assert duration_us_for_frames(30, FPS) == 1_000_000


def test_frame_conversion_is_deterministic_floor() -> None:
    fps = FrameRate(num=30000, den=1001)
    frames = frames_for_duration_us(3_000_000, fps)
    assert frames == 89  # floor(3_000_000 * 30000 / (1001 * 1e6))
    assert duration_us_for_frames(frames, fps) == 2_969_633


def test_frame_conversion_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        frames_for_duration_us(0, FPS)
    with pytest.raises(ValueError):
        frames_for_duration_us(-1, FPS)
    with pytest.raises(ValueError):
        duration_us_for_frames(0, FPS)


def test_frames_never_below_one() -> None:
    assert frames_for_duration_us(1, FPS) == 1


def test_frames_for_interval_half_open() -> None:
    assert frames_for_interval(0, 1_000_000, FPS) == 30
    assert frames_for_interval(1_000_000, 1_000_000, FPS) == 0
    with pytest.raises(ValueError):
        frames_for_interval(2_000_000, 1_000_000, FPS)


# ══════════════════════════════════════════════════════════════════
# TimelineSpec 契约
# ══════════════════════════════════════════════════════════════════


def test_plan_is_deterministic_byte_level() -> None:
    first = build_spec().model_dump(mode="json")
    second = build_spec().model_dump(mode="json")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_plan_clips_have_stable_ids_and_continuous_windows() -> None:
    spec = build_spec()
    assert [c.clip_id for c in spec.clips] == [f"clip-{i:04d}" for i in range(4)]
    assert [c.sequence for c in spec.clips] == [0, 1, 2, 3]
    for index, clip in enumerate(spec.clips):
        assert clip.timeline_start_frame == index * 90  # 3s @30fps = 90 帧
        assert clip.timeline_end_frame == (index + 1) * 90
        assert clip.duration_frames == 90
        assert clip.source_in_us == index * 3_000_000
        assert clip.source_out_us == (index + 1) * 3_000_000


def test_plan_total_frames_match_clips() -> None:
    spec = build_spec()
    assert spec.total_frames == sum(c.duration_frames for c in spec.clips)
    assert spec.playback_duration_us == sum(
        c.playback_duration_us for c in spec.clips)
    assert spec.target_duration_us == sum(
        c.target_duration_us for c in spec.clips)
    assert spec.duration_drift_us == (
        spec.playback_duration_us - spec.target_duration_us)


def test_plan_tracks_reference_existing_elements() -> None:
    """video 轨道承载片段；text/audio 轨道承载意图 id（语义区分）。"""
    spec = build_spec()
    clip_ids = {c.clip_id for c in spec.clips}
    intent_ids = {i.intent_id for i in spec.text_intents} | {
        a.intent_id for a in spec.audio_intents}
    for track in spec.tracks:
        valid = clip_ids if track.kind == "video" else intent_ids
        assert set(track.clip_ids) <= valid, track.track_id


def test_half_open_interval_violations_rejected() -> None:
    payload = build_spec().model_dump(mode="json")
    payload["clips"][0]["timeline_end_frame"] = \
        payload["clips"][0]["timeline_start_frame"]
    with pytest.raises(PydanticValidationError):
        TimelineSpec.model_validate(payload)


def test_timeline_gap_rejected() -> None:
    payload = build_spec().model_dump(mode="json")
    payload["clips"][1]["timeline_start_frame"] += 5
    with pytest.raises(PydanticValidationError):
        TimelineSpec.model_validate(payload)


def test_video_clip_requires_positive_source_interval() -> None:
    payload = build_spec().model_dump(mode="json")
    payload["clips"][0]["source_out_us"] = payload["clips"][0]["source_in_us"]
    with pytest.raises(PydanticValidationError):
        TimelineSpec.model_validate(payload)


def test_image_clip_forbids_source_duration() -> None:
    """图片没有自然时长：源区间必须为 [0, 0)，绝不伪装。"""
    spec = build_spec()
    image_clips = [c for c in spec.clips if c.media_kind == "image"]
    assert not image_clips, "本夹具无图片镜头"


def test_image_clip_source_interval_must_be_empty() -> None:
    payload = build_spec().model_dump(mode="json")
    payload["clips"][0]["media_kind"] = "image"
    with pytest.raises(PydanticValidationError):
        TimelineSpec.model_validate(payload)


def test_frame_drift_recorded_not_silent() -> None:
    spec = build_spec(analyses=[
        analysis(A1, segments=4, duration_us=12_000_000, fps=(30000, 1001)),
        analysis(A2, segments=1, duration_us=None),
    ])
    assert spec.frame_rate.as_tuple() == (30000, 1001)
    drifts = [c.duration_drift_us for c in spec.clips]
    assert any(d != 0 for d in drifts), "非整除帧率必然产生偏差"
    assert spec.duration_drift_us == sum(drifts)
    assert any(w.startswith("FRAME_ROUNDING_DRIFT") for w in spec.warnings)


# ══════════════════════════════════════════════════════════════════
# 未匹配镜头与溯源
# ══════════════════════════════════════════════════════════════════


def test_unmatched_shot_is_explicit_not_fabricated() -> None:
    """未匹配镜头必须显式保留，且不得用未选中素材凑数。"""
    spec = build_spec(selected=[None, None, None, None])
    assert spec.clips == []
    assert len(spec.unresolved_shots) == 4
    assert {u.reason for u in spec.unresolved_shots} == {"no_segment_selected"}
    assert spec.total_frames == 0
    assert any(w.startswith("UNMATCHED_SHOT:") for w in spec.warnings)


def test_partially_unmatched_keeps_matched_only() -> None:
    spec = build_spec(selected=["a1a1a1a1a1a1a1a1#0000", None, None, None])
    assert len(spec.clips) == 1
    assert spec.clips[0].shot_id == "shot-0000"
    assert len(spec.unresolved_shots) == 3
    unresolved_ids = {u.shot_id for u in spec.unresolved_shots}
    assert spec.clips[0].shot_id not in unresolved_ids


def test_clip_lineage_points_to_frozen_revisions() -> None:
    spec = build_spec()
    for clip in spec.clips:
        assert clip.storyboard_artifact_revision_id == REFS["creative.storyboard"]
        assert clip.match_artifact_revision_id == REFS["creative.match"]
        assert clip.analysis_artifact_revision_id == REFS[f"asset.analysis.{A1}"]


def test_short_segment_shortens_clip_honestly() -> None:
    """segment 不足以覆盖目标时长：如实缩短并告警，不伪造素材。"""
    analyses = [analysis(A1, segments=4, duration_us=2_000_000),
                analysis(A2, segments=1, duration_us=None)]
    spec = build_spec(
        selected=["a1a1a1a1a1a1a1a1#0000"] + [None] * 3, analyses=analyses)
    assert len(spec.clips) == 1
    clip = spec.clips[0]
    assert clip.source_out_us - clip.source_in_us == 500_000
    assert clip.target_duration_us == 3_000_000
    assert clip.playback_duration_us < clip.target_duration_us
    assert any(w.startswith("SOURCE_TOO_SHORT:shot-0000") for w in spec.warnings)


def test_source_interval_out_of_media_duration_rejected() -> None:
    """源区间越界（超出媒体时长）必须拒绝。"""
    analyses = [analysis(A1, segments=4, duration_us=12_000_000)]
    analyses[0]["duration_us"] = 1_000_000  # 与 segment 区间矛盾
    with pytest.raises(ValidationError):
        build_spec(analyses=analyses)


def test_wrong_asset_in_analysis_rejected() -> None:
    analyses = [analysis(A1, segments=4, duration_us=12_000_000)]
    analyses[0]["asset_id"] = "zz" * 8
    spec = build_spec(
        selected=["a1a1a1a1a1a1a1a1#0000"] + [None] * 3, analyses=analyses)
    assert spec.clips == []
    assert spec.unresolved_shots[0].reason == "segment_not_resolvable"


def test_frame_rate_selected_deterministically() -> None:
    fps, source = select_frame_rate(ANALYSES)
    assert fps.as_tuple() == (30, 1)
    assert source.startswith("probe:")
    fps2, source2 = select_frame_rate([])
    assert fps2.as_tuple() == (30, 1) and source2 == "fallback:30/1"


# ══════════════════════════════════════════════════════════════════
# 模板清单
# ══════════════════════════════════════════════════════════════════


def test_three_builtin_manifests_are_valid() -> None:
    manifests = list_manifests()
    assert [m.template_id for m in manifests] == [
        "brand_story", "event_promo", "product_highlight"]
    for manifest in manifests:
        assert manifest.version == TEMPLATE_VERSION == "1.0.0"
        assert manifest.slots
        assert manifest.text_limits
        assert manifest.applicable_aspect_ratios
        assert manifest.missing_slot_strategy in ("warn", "fail")


def test_manifest_lookup_is_deterministic() -> None:
    assert get_manifest("product_highlight").template_id == "product_highlight"
    with pytest.raises(ValidationError):
        get_manifest("nope")
    assert json.dumps([m.template_id for m in list_manifests()]) == json.dumps(
        [m.template_id for m in list_manifests()])


def test_default_manifest_selection_is_deterministic() -> None:
    manifest, reason = select_default_manifest("9:16", FPS)
    assert manifest.template_id == "brand_story"  # template_id 升序第一个
    assert reason == "default:first_by_template_id"
    with pytest.raises(ValidationError):
        select_default_manifest("5:4", FPS)


def test_manifest_rejects_duplicate_slot_orders() -> None:
    payload = MANIFESTS["brand_story"].model_dump(mode="json")
    payload["slots"][1]["order"] = payload["slots"][0]["order"]
    with pytest.raises(PydanticValidationError):
        TemplateManifest.model_validate(payload)


def test_manifest_rejects_duplicate_aspect_ratios() -> None:
    payload = MANIFESTS["brand_story"].model_dump(mode="json")
    payload["applicable_aspect_ratios"] = ["16:9", "16:9"]
    with pytest.raises(PydanticValidationError):
        TemplateManifest.model_validate(payload)


# ══════════════════════════════════════════════════════════════════
# 模板绑定
# ══════════════════════════════════════════════════════════════════


def _bind(spec: TimelineSpec, template_id: str,
          script_payload_dict: dict | None = None) -> TemplateBinding:
    from cybercut.contracts.creative import ScriptRevision

    script = ScriptRevision.model_validate(
        script_payload_dict or script_payload())
    manifest = get_manifest(template_id)
    return bind_template(
        project_id=PROJECT, run_id=RUN, revision_no=1,
        manifest=manifest, selection_reason="explicit:test",
        timeline=spec, timeline_artifact_revision_id="rev-plan",
        script=script, script_artifact_revision_id="rev-script",
        revision_note=None,
    )


def test_binding_is_deterministic() -> None:
    spec = build_spec()
    first = _bind(spec, "product_highlight").model_dump(mode="json")
    second = _bind(spec, "product_highlight").model_dump(mode="json")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_binding_maps_all_slots_when_material_suffices() -> None:
    spec = build_spec()
    binding = _bind(spec, "product_highlight")
    assert [b.satisfied for b in binding.bindings] == [True, True, True]
    assert binding.accepted is True
    assert binding.unmet == []
    assert [b.clip_id for b in binding.bindings] == [
        "clip-0000", "clip-0001", "clip-0002"]


def test_binding_never_reuses_a_clip() -> None:
    spec = build_spec()
    binding = _bind(spec, "event_promo")  # 4 个槽位
    clip_ids = [b.clip_id for b in binding.bindings if b.clip_id]
    assert len(clip_ids) == len(set(clip_ids))


def test_required_slot_empty_warn_strategy_not_blocking() -> None:
    """必需槽位为空 + warn 策略：结构化告警，但不阻断（accepted=True）。"""
    spec = build_spec(selected=["a1a1a1a1a1a1a1a1#0000", None, None, None])
    binding = _bind(spec, "product_highlight")  # 3 槽位（全 required），1 个片段
    unfilled = [b for b in binding.bindings if not b.satisfied]
    assert len(unfilled) == 2
    assert all(b.reason == "required_slot_empty_warn" for b in unfilled)
    assert all(w.startswith("SLOT_UNFILLED:") for w in binding.warnings)
    # warn 策略下必需槽位为空**不阻断**：不进入 unmet
    assert binding.unmet == []
    assert binding.accepted is True


def test_required_slot_empty_fail_strategy_blocks() -> None:
    """必需槽位为空 + fail 策略：阻断（accepted=False，unmet 含 SLOT_UNFILLED）。"""
    spec = build_spec(selected=[None, None, None, None])
    binding = _bind(spec, "event_promo")  # fail 策略；3 必需 + 1 可选
    unfilled = [b for b in binding.bindings if not b.satisfied]
    assert len(unfilled) == 4
    blocking = [u for u in binding.unmet if u.code == "SLOT_UNFILLED"]
    assert len(blocking) == 3, "仅必需槽位进入阻断性 unmet"
    assert binding.accepted is False
    assert any(w == "TEMPLATE_SLOT_MISSING_FAIL" for w in binding.warnings)


def test_optional_slot_empty_never_blocks() -> None:
    """可选槽位为空：任何缺槽策略下都不阻断（fail 策略亦然）。"""
    spec = build_spec(
        selected=["a1a1a1a1a1a1a1a1#0000", "a1a1a1a1a1a1a1a1#0001",
                  "a1a1a1a1a1a1a1a1#0002", None])  # 3 个片段
    # event_promo：fail 策略，cta 槽位 required=False
    binding = _bind(spec, "event_promo")
    cta = [b for b in binding.bindings if b.slot_id == "cta"]
    assert cta and cta[0].satisfied is False
    assert cta[0].reason == "optional_slot_empty"
    assert any(w.startswith("OPTIONAL_SLOT_EMPTY:cta") for w in binding.warnings)
    assert binding.unmet == []
    assert binding.accepted is True


def test_text_limit_exceeded_reported_not_rewritten() -> None:
    """文案超限：阻断（accepted=False），只报告，绝不改写事实性文案。"""
    payload = script_payload()
    payload["sections"][0]["on_screen_text"] = "超" * 64
    spec = build_spec(script_payload_dict=payload)
    before = [(i.intent_id, i.text) for i in spec.text_intents]
    binding = _bind(spec, "product_highlight")
    assert any(u.code == "TEXT_LIMIT_EXCEEDED"
               and u.subject == "on_screen_text" for u in binding.unmet)
    assert binding.accepted is False
    # 绝不改写文案
    after = [(i.intent_id, i.text) for i in spec.text_intents]
    assert before == after
    assert binding.warnings


def test_aspect_unsupported_blocks() -> None:
    """画幅不支持：阻断（accepted=False）。"""
    spec = build_spec()
    spec = spec.model_copy(update={"aspect_ratio": "5:4"})
    binding = _bind(spec, "product_highlight")
    assert any(u.code == "ASPECT_UNSUPPORTED" for u in binding.unmet)
    assert binding.accepted is False
    assert binding.warnings


def test_frame_rate_unsupported_blocks() -> None:
    """帧率不支持：阻断（accepted=False）。"""
    spec = build_spec()
    spec = spec.model_copy(update={"frame_rate": FrameRate(num=24000, den=1001)})
    binding = _bind(spec, "product_highlight")
    assert any(u.code == "FRAME_RATE_UNSUPPORTED" for u in binding.unmet)
    assert binding.accepted is False


def test_transition_not_allowed_blocks() -> None:
    """转场不支持：阻断（accepted=False）。"""
    shots = [_shot(i) for i in range(4)]
    for shot in shots:
        shot["transition_hint"] = "whip_pan"
    spec = build_spec(shots=shots)
    binding = _bind(spec, "product_highlight")  # 仅允许 cut/fade/dissolve
    assert any(u.code == "TRANSITION_NOT_ALLOWED" for u in binding.unmet)
    assert binding.accepted is False
    assert binding.warnings


def test_accepted_always_matches_unmet() -> None:
    """accepted ⇔ unmet 为空（契约级一致性，跨多模板/多场景）。"""
    scenarios = [
        build_spec(),
        build_spec(selected=[None, None, None, None]),
        build_spec(selected=["a1a1a1a1a1a1a1a1#0000", None, None, None]),
    ]
    for spec in scenarios:
        for template_id in MANIFESTS:
            binding = _bind(spec, template_id)
            assert binding.accepted == (not binding.unmet), (
                f"{template_id}: accepted 与 unmet 不一致")


def test_binding_keeps_upstream_selection_unchanged() -> None:
    """绑定不得改写 match 的素材选择。"""
    spec = build_spec()
    before = [(c.shot_id, c.asset_id, c.segment_id) for c in spec.clips]
    _bind(spec, "event_promo")
    after = [(c.shot_id, c.asset_id, c.segment_id) for c in spec.clips]
    assert before == after


def test_binding_records_selection_reason() -> None:
    spec = build_spec()
    binding = _bind(spec, "brand_story")
    assert binding.selection_reason == "explicit:test"
    assert binding.template_version == "1.0.0"
