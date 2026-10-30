"""确定性时间线规划：StoryboardRevision + AssetMatchResult → TimelineSpec。

规则（ADR-0005）：
- 处理顺序 = 分镜 `sequence_no` 升序（确定性）；
- 每个**已选中素材**的镜头必须可完整溯源：
  storyboard shot → match 选中 → AssetSegment → 原始 asset；
- 未匹配镜头进入 `unresolved_shots`（显式保留），**绝不**用未选中素材凑数；
- 视频：源区间在 segment 内**裁剪**以贴合目标时长；segment 不足时如实缩短并告警；
- 图片：源区间恒为 `[0, 0)`，播放时长来自目标时长（契约区分源/目标时间）；
- 帧换算为纯整数运算（`editing.frames`），偏差显式记录。
"""
from __future__ import annotations

from cybercut.contracts.creative import (
    AssetMatchResult,
    CreativeBrief,
    ScriptRevision,
    StoryboardRevision,
)
from cybercut.contracts.editing import (
    TRACK_TEXT,
    TRACK_VIDEO,
    AudioIntent,
    FrameRate,
    TextIntent,
    TimelineClip,
    TimelineSpec,
    TimelineTrack,
    TimelineTransition,
    UnresolvedShot,
)
from cybercut.contracts.errors import ValidationError
from cybercut.editing.frames import (
    duration_us_for_frames,
    frames_for_duration_us,
    frames_for_interval,
)

PLAN_PROMPT_VERSION = "edit-plan.v1"
PLAN_MODEL_ID = "deterministic:edit-plan-v1"

_FALLBACK_FPS = FrameRate(num=30, den=1)


def _clip_id(sequence: int) -> str:
    return f"clip-{sequence:04d}"


def _transition_id(sequence: int) -> str:
    return f"trans-{sequence:04d}"


def select_frame_rate(analyses: list[dict]) -> tuple[FrameRate, str]:
    """确定性帧率选择：按 `asset_id` 升序取第一个有效视频 `avg_frame_rate`。

    无可用值时回退 `30/1`（记录于返回的来源说明）。
    """
    for analysis in sorted(analyses or [], key=lambda a: str(a.get("asset_id"))):
        probe = analysis.get("probe") or {}
        video = probe.get("video") or {}
        rate = video.get("avg_frame_rate")
        if isinstance(rate, dict):
            num, den = rate.get("num"), rate.get("den")
            if isinstance(num, int) and isinstance(den, int) and num > 0 and den > 0:
                try:
                    return FrameRate(num=num, den=den), f"probe:{analysis['asset_id']}"
                except ValidationError:  # pragma: no cover - 非法值跳过
                    continue
    return _FALLBACK_FPS, "fallback:30/1"


def _segment_map(analysis: dict) -> dict[int, dict]:
    return {
        int(seg.get("segment_index", 0)): seg
        for seg in (analysis.get("segments") or [])
    }


def plan_timeline(
    *,
    project_id: str,
    run_id: str,
    revision_no: int,
    revision_note: str | None,
    storyboard: StoryboardRevision,
    match: AssetMatchResult,
    script: ScriptRevision,
    brief: CreativeBrief,
    analyses: list[dict],
    frozen_refs: dict[str, str],
) -> TimelineSpec:
    """构造并校验 `TimelineSpec`（纯确定性）。

    `frozen_refs` 为 `kind → artifact_revision_id`（来自 attempt 冻结输入），
    用于把每个片段的溯源指向**本 Run 冻结**的产物。
    """
    fps, fps_source = select_frame_rate(analyses)
    analyses_by_asset = {
        str(a.get("asset_id")): a for a in (analyses or [])
    }
    match_by_shot = {s.shot_id: s for s in match.shots}

    warnings: list[str] = []
    clips: list[TimelineClip] = []
    unresolved: list[UnresolvedShot] = []
    cursor_frame = 0

    shots = sorted(storyboard.shots, key=lambda s: s.sequence_no)
    for shot in shots:
        shot_match = match_by_shot.get(shot.shot_id)
        if shot_match is None:
            unresolved.append(UnresolvedShot(
                shot_id=shot.shot_id, section_id=shot.section_id,
                sequence=shot.sequence_no,
                target_duration_us=shot.target_duration_us,
                reason="match_entry_missing",
                detail="AssetMatchResult 中不存在该 shot 的匹配记录"))
            warnings.append(f"UNMATCHED_SHOT:{shot.shot_id}:match_entry_missing")
            continue
        selected = shot_match.selected_segment_id
        if selected is None:
            unresolved.append(UnresolvedShot(
                shot_id=shot.shot_id, section_id=shot.section_id,
                sequence=shot.sequence_no,
                target_duration_us=shot.target_duration_us,
                reason="no_segment_selected",
                detail="匹配结果未选中任何素材（低于阈值或无候选）"))
            warnings.append(f"UNMATCHED_SHOT:{shot.shot_id}:no_segment_selected")
            continue

        asset_id, segment_index = _parse_segment_id(selected)
        analysis = analyses_by_asset.get(asset_id)
        if analysis is None:
            unresolved.append(UnresolvedShot(
                shot_id=shot.shot_id, section_id=shot.section_id,
                sequence=shot.sequence_no,
                target_duration_us=shot.target_duration_us,
                reason="segment_not_resolvable",
                detail=f"选中素材 {asset_id!r} 不在当前 Run 的分析产物中"))
            warnings.append(
                f"UNMATCHED_SHOT:{shot.shot_id}:segment_not_resolvable")
            continue

        segments = _segment_map(analysis)
        segment = segments.get(segment_index)
        if segment is None:
            unresolved.append(UnresolvedShot(
                shot_id=shot.shot_id, section_id=shot.section_id,
                sequence=shot.sequence_no,
                target_duration_us=shot.target_duration_us,
                reason="segment_not_resolvable",
                detail=f"素材 {asset_id!r} 中不存在 segment_index={segment_index}"))
            warnings.append(
                f"UNMATCHED_SHOT:{shot.shot_id}:segment_not_resolvable")
            continue

        media_kind = str(analysis.get("media_kind") or "video")
        target_us = int(shot.target_duration_us)

        if media_kind == "image":
            source_in, source_out = 0, 0
            duration_frames = frames_for_duration_us(target_us, fps)
            playback_us = duration_us_for_frames(duration_frames, fps)
            warnings.append(f"IMAGE_NO_SOURCE_DURATION:{shot.shot_id}")
        else:
            seg_start = int(segment.get("start_us", 0))
            seg_end = int(segment.get("end_us", 0))
            available = seg_end - seg_start
            if available <= 0:
                unresolved.append(UnresolvedShot(
                    shot_id=shot.shot_id, section_id=shot.section_id,
                    sequence=shot.sequence_no,
                    target_duration_us=target_us,
                    reason="segment_not_resolvable",
                    detail=f"segment 区间为空：[{seg_start}, {seg_end})"))
                warnings.append(
                    f"UNMATCHED_SHOT:{shot.shot_id}:empty_segment")
                continue
            if available >= target_us:
                source_in, source_out = seg_start, seg_start + target_us
            else:
                source_in, source_out = seg_start, seg_end
                warnings.append(
                    f"SOURCE_TOO_SHORT:{shot.shot_id}:{target_us - available}")
            duration_frames = frames_for_interval(source_in, source_out, fps)
            if duration_frames <= 0:
                duration_frames = 1
                warnings.append(
                    f"SOURCE_FRAME_FLOOR:{shot.shot_id}")
            playback_us = duration_us_for_frames(duration_frames, fps)

        clip_warnings: list[str] = []
        if media_kind == "video":
            media_duration = analysis.get("duration_us")
            if isinstance(media_duration, int) and source_out > media_duration:
                raise ValidationError(
                    f"{shot.shot_id}: 源区间越界（source_out={source_out}"
                    f" > 媒体时长 {media_duration}）",
                    details={"asset_id": asset_id, "source_in_us": source_in,
                             "source_out_us": source_out},
                )
        else:
            clip_warnings.append("IMAGE_NO_SOURCE_DURATION")

        clips.append(TimelineClip(
            clip_id=_clip_id(len(clips)),
            sequence=len(clips),
            track_id=TRACK_VIDEO,
            shot_id=shot.shot_id,
            section_id=shot.section_id,
            asset_id=asset_id,
            segment_id=selected,
            segment_index=segment_index,
            media_kind="image" if media_kind == "image" else "video",
            source_in_us=source_in,
            source_out_us=source_out,
            timeline_start_frame=cursor_frame,
            timeline_end_frame=cursor_frame + duration_frames,
            target_duration_us=target_us,
            duration_frames=duration_frames,
            playback_duration_us=playback_us,
            duration_drift_us=playback_us - target_us,
            storyboard_artifact_revision_id=frozen_refs["creative.storyboard"],
            match_artifact_revision_id=frozen_refs["creative.match"],
            analysis_artifact_revision_id=frozen_refs[f"asset.analysis.{asset_id}"],
            evidence_refs=[r.model_dump(mode="json") for r in shot.evidence_refs],
            warnings=clip_warnings,
        ))
        cursor_frame += duration_frames

    transitions = [
        TimelineTransition(
            transition_id=_transition_id(index),
            sequence=index,
            from_clip_id=clips[index].clip_id,
            to_clip_id=clips[index + 1].clip_id,
            kind=clips[index + 1] and _transition_kind(
                storyboard, clips[index + 1].shot_id),
            position_frame=clips[index + 1].timeline_start_frame,
        )
        for index in range(len(clips) - 1)
    ]

    text_intents, audio_intents, text_warnings = _intents_from_script(
        script=script, clips=clips, fps=fps, total_frames=cursor_frame)
    warnings.extend(text_warnings)

    total_target = sum(c.target_duration_us for c in clips)
    total_playback = sum(c.playback_duration_us for c in clips)
    if total_playback != total_target:
        warnings.append(f"FRAME_ROUNDING_DRIFT:{total_playback - total_target}")

    tracks = [
        TimelineTrack(track_id="v1", kind="video",
                      clip_ids=[c.clip_id for c in clips]),
        TimelineTrack(track_id="a1", kind="audio",
                      clip_ids=[a.intent_id for a in audio_intents]),
        TimelineTrack(track_id=TRACK_TEXT, kind="text",
                      clip_ids=[x.intent_id for x in text_intents]),
    ]

    return TimelineSpec(
        edit_plan_revision_id=f"pending:edit.plan-{run_id}",
        project_id=project_id,
        run_id=run_id,
        revision_no=revision_no,
        frame_rate=fps,
        aspect_ratio=brief.aspect_ratio,
        width=_width_for(brief.aspect_ratio),
        height=_height_for(brief.aspect_ratio),
        total_frames=cursor_frame,
        target_duration_us=total_target,
        playback_duration_us=total_playback,
        duration_drift_us=total_playback - total_target,
        clips=clips,
        transitions=transitions,
        tracks=tracks,
        text_intents=text_intents,
        audio_intents=audio_intents,
        unresolved_shots=unresolved,
        storyboard_artifact_revision_id=frozen_refs["creative.storyboard"],
        match_artifact_revision_id=frozen_refs["creative.match"],
        script_artifact_revision_id=frozen_refs["creative.script"],
        brief_artifact_revision_id=frozen_refs["creative.brief"],
        source_artifact_revision_ids=sorted({
            c.analysis_artifact_revision_id for c in clips
        }),
        prompt_version=PLAN_PROMPT_VERSION,
        model_id=PLAN_MODEL_ID,
        revision_note=revision_note,
        warnings=sorted(set(warnings)),
        decision_trace=[{
            "step": "edit.plan",
            "decision": "build_timeline",
            "rationale_code": "deterministic_template",
            "evidence_count": len(clips),
            "notes": [f"fps_source={fps_source}",
                      f"unresolved={len(unresolved)}"],
        }],
    )


def _transition_kind(storyboard: StoryboardRevision, shot_id: str) -> str:
    for shot in storyboard.shots:
        if shot.shot_id == shot_id:
            return str(shot.transition_hint)
    return "cut"


def _intents_from_script(
    *, script: ScriptRevision, clips: list[TimelineClip], fps: FrameRate,
    total_frames: int,
) -> tuple[list[TextIntent], list[AudioIntent], list[str]]:
    """把脚本段落映射为文本/音频意图（窗口 = 该段落片段的帧范围）。"""
    warnings: list[str] = []
    section_bounds: dict[str, list[int]] = {}
    for clip in clips:
        bounds = section_bounds.setdefault(clip.section_id, [None, 0])
        if bounds[0] is None:
            bounds[0] = clip.timeline_start_frame
        bounds[1] = clip.timeline_end_frame

    texts: list[TextIntent] = []
    audios: list[AudioIntent] = []
    for section in script.sections:
        bounds = section_bounds.get(section.section_id)
        if bounds is None or bounds[0] is None:
            warnings.append(
                f"SECTION_WITHOUT_TIMELINE:{section.section_id}")
            continue
        start, end = int(bounds[0]), int(bounds[1])
        if end <= start:
            warnings.append(f"SECTION_EMPTY_TIMELINE:{section.section_id}")
            continue
        texts.append(TextIntent(
            intent_id=f"text-{section.section_id}",
            section_id=section.section_id,
            kind="on_screen_text",
            text=section.on_screen_text,
            start_frame=start, end_frame=end,
            evidence_refs=[r.model_dump(mode="json") for r in section.evidence_refs],
        ))
        texts.append(TextIntent(
            intent_id=f"subtitle-{section.section_id}",
            section_id=section.section_id,
            kind="subtitle",
            text=section.narration,
            start_frame=start, end_frame=end,
            evidence_refs=[r.model_dump(mode="json")
                           for r in section.evidence_refs],
        ))
        audios.append(AudioIntent(
            intent_id=f"audio-{section.section_id}",
            section_id=section.section_id,
            kind="narration",
            text=section.narration,
            start_frame=start, end_frame=end,
        ))
    return texts, audios, warnings


def _parse_segment_id(segment_id: str) -> tuple[str, int]:
    from cybercut.creative.matching import parse_segment_id

    return parse_segment_id(segment_id)


def _width_for(aspect_ratio: str) -> int:
    widths = {"9:16": 1080, "16:9": 1920, "1:1": 1080}
    return int(widths.get(aspect_ratio, 1920))


def _height_for(aspect_ratio: str) -> int:
    heights = {"9:16": 1920, "16:9": 1080, "1:1": 1080}
    return int(heights.get(aspect_ratio, 1080))


__all__ = [
    "PLAN_MODEL_ID",
    "PLAN_PROMPT_VERSION",
    "plan_timeline",
    "select_frame_rate",
]
