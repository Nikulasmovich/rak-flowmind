"""WB-04 编辑规划契约（Pydantic v2，extra=forbid，整数帧时间轴）。

设计原则（ADR-0005）：
- **整数帧是时间轴主单位**：所有时间线位置用整数帧表达，禁止浮点累计；
- **半开区间**：源素材区间 `[source_in_us, source_out_us)`、
  时间线区间 `[timeline_start_frame, timeline_end_frame)`；
- **源时间与目标时间严格区分**：图片没有自然时长，其源区间为空 `[0, 0)`，
  播放时长只能来自脚本/分镜目标时长，绝不伪装成素材自然时长；
- **确定性**：同一冻结输入 + 同一配置必须产生字节级一致结果；
- 所有源引用必须指向该 Run 冻结的 artifact revisions。
"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TIMELINE_SCHEMA_VERSION = "timeline-spec.v1"
TEMPLATE_MANIFEST_SCHEMA_VERSION = "template-manifest.v1"
TEMPLATE_BINDING_SCHEMA_VERSION = "template-binding.v1"

#: 时间线轨道（最小可用三轨）
TRACK_VIDEO = "v1"
TRACK_AUDIO = "a1"
TRACK_TEXT = "t1"

#: 本阶段允许的速度（不重定时）
ALLOWED_SPEED = "1x"


def _no_nan_inf(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("禁止 NaN / Infinity")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="before")
    @classmethod
    def _reject_nan_inf(cls, value: Any) -> Any:
        return _no_nan_inf(value)


# ══════════════════════════════════════════════════════════════════
# 时间基准
# ══════════════════════════════════════════════════════════════════


class FrameRate(_Strict):
    """整数有理数帧率：`fps = num / den`（帧/秒），`den > 0` 且已约分。

    所有帧/微秒换算必须经由 `editing.frames` 的**整数**运算完成，
    禁止引入浮点中间值。
    """

    num: int = Field(gt=0)
    den: int = Field(gt=0)

    @model_validator(mode="after")
    def _reduced(self) -> FrameRate:
        import math

        if math.gcd(self.num, self.den) != 1:
            raise ValueError(f"帧率必须约分：{self.num}/{self.den}")
        return self

    def as_tuple(self) -> tuple[int, int]:
        return (self.num, self.den)


# ══════════════════════════════════════════════════════════════════
# 时间线片段
# ══════════════════════════════════════════════════════════════════


class TimelineClip(_Strict):
    """时间线片段。

    - `source_*` 为**源素材**微秒区间（半开 `[source_in_us, source_out_us)`）；
      图片没有自然时长，恒为 `[0, 0)`；
    - `timeline_*` 为**时间线**整数帧区间（半开 `[start, end)`）；
    - `target_duration_us` 是脚本/分镜的**请求**时长；
      `playback_duration_us` 是该片段在时间线上**实际**占据的时长（由帧数
      换算回微秒，floor）；二者之差 `duration_drift_us` 显式记录，不静默漂移。
    """

    clip_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    track_id: str = Field(min_length=1)

    # ── 溯源链：shot → match 选中 → segment → asset ──
    shot_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    segment_index: int = Field(ge=0)
    media_kind: Literal["video", "image"]

    # ── 源素材区间（半开，微秒；图片为 [0, 0)）──
    source_in_us: int = Field(ge=0)
    source_out_us: int = Field(ge=0)

    # ── 时间线区间（半开，整数帧）──
    timeline_start_frame: int = Field(ge=0)
    timeline_end_frame: int = Field(gt=0)

    # ── 时长与偏差 ──
    target_duration_us: int = Field(gt=0)
    duration_frames: int = Field(gt=0)
    playback_duration_us: int = Field(gt=0)
    duration_drift_us: int

    # ── 渲染意图（本阶段仅表达，不执行）──
    speed: Literal["1x"] = ALLOWED_SPEED
    scale: Literal["fit", "fill"] = "fit"
    crop: Literal["none", "center"] = "none"

    # ── 溯源：指向本 Run 冻结的 artifact revisions ──
    storyboard_artifact_revision_id: str = Field(min_length=1)
    match_artifact_revision_id: str = Field(min_length=1)
    analysis_artifact_revision_id: str = Field(min_length=1)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _intervals(self) -> TimelineClip:
        if self.timeline_end_frame <= self.timeline_start_frame:
            raise ValueError(
                f"{self.clip_id}: timeline_end_frame 必须大于 timeline_start_frame"
                f"（半开区间）")
        if self.media_kind == "image":
            if (self.source_in_us, self.source_out_us) != (0, 0):
                raise ValueError(
                    f"{self.clip_id}: 图片没有自然时长，源区间必须为 [0, 0)")
        else:
            if self.source_out_us <= self.source_in_us:
                raise ValueError(
                    f"{self.clip_id}: 视频源区间必须为正（半开区间，微秒）")
        if self.duration_frames != self.timeline_end_frame - self.timeline_start_frame:
            raise ValueError(
                f"{self.clip_id}: duration_frames 必须等于时间线区间长度")
        return self


class TimelineTransition(_Strict):
    """相邻片段间的转场意图（数据，不执行）。"""

    transition_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    from_clip_id: str = Field(min_length=1)
    to_clip_id: str = Field(min_length=1)
    kind: Literal["cut", "fade", "dissolve", "match_cut", "whip_pan"]
    position_frame: int = Field(ge=0)


class TimelineTrack(_Strict):
    """轨道：同一轨道上的元素按时间线顺序排列。"""

    track_id: str = Field(min_length=1)
    kind: Literal["video", "audio", "text"]
    clip_ids: list[str] = Field(default_factory=list)

    @field_validator("clip_ids")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("clip_ids 不得重复")
        return value


class TextIntent(_Strict):
    """字幕/屏幕文字**意图**（不生成、不烧录）。"""

    intent_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    kind: Literal["subtitle", "on_screen_text"]
    text: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _window(self) -> TextIntent:
        if self.end_frame <= self.start_frame:
            raise ValueError(f"{self.intent_id}: 文本窗口必须为正（半开区间）")
        return self


class AudioIntent(_Strict):
    """旁白/BGM **意图**（不做 TTS、不下载、不混音）。"""

    intent_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    kind: Literal["narration", "bgm"]
    text: str | None = None
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)

    @model_validator(mode="after")
    def _window(self) -> AudioIntent:
        if self.end_frame <= self.start_frame:
            raise ValueError(f"{self.intent_id}: 音频窗口必须为正（半开区间）")
        return self


class UnresolvedShot(_Strict):
    """未匹配镜头：**显式保留**，绝不偷偷补素材。"""

    shot_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    target_duration_us: int = Field(gt=0)
    reason: Literal[
        "no_segment_selected", "match_entry_missing", "segment_not_resolvable"
    ]
    detail: str = Field(min_length=1)


class TimelineSpec(_Strict):
    """编辑规划产物：确定性、整数帧时间线。"""

    schema_version: str = TIMELINE_SCHEMA_VERSION
    edit_plan_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)

    # ── 时间基准（整数有理数帧率）──
    frame_rate: FrameRate
    aspect_ratio: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    # ── 时长（帧为主单位；微秒为换算结果）──
    total_frames: int = Field(ge=0)
    target_duration_us: int = Field(ge=0)
    playback_duration_us: int = Field(ge=0)
    duration_drift_us: int

    # ── 内容 ──
    clips: list[TimelineClip] = Field(default_factory=list)
    transitions: list[TimelineTransition] = Field(default_factory=list)
    tracks: list[TimelineTrack] = Field(default_factory=list)
    text_intents: list[TextIntent] = Field(default_factory=list)
    audio_intents: list[AudioIntent] = Field(default_factory=list)
    unresolved_shots: list[UnresolvedShot] = Field(default_factory=list)

    # ── 溯源：本 Run 冻结的产物 revision ──
    storyboard_artifact_revision_id: str = Field(min_length=1)
    match_artifact_revision_id: str = Field(min_length=1)
    script_artifact_revision_id: str = Field(min_length=1)
    brief_artifact_revision_id: str = Field(min_length=1)
    source_artifact_revision_ids: list[str] = Field(default_factory=list)

    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("aspect_ratio")
    @classmethod
    def _ratio_shape(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[1-9]\d{0,3}:[1-9]\d{0,3}", value):
            raise ValueError(f"aspect_ratio 必须形如 '16:9'：{value!r}")
        return value

    @field_validator("clips")
    @classmethod
    def _deterministic_order(cls, value: list[TimelineClip]) -> list[TimelineClip]:
        seqs = [c.sequence for c in value]
        if seqs != list(range(len(value))):
            raise ValueError("clips.sequence 必须是确定性的 0..n-1 递增序列")
        ids = [c.clip_id for c in value]
        if len(set(ids)) != len(ids):
            raise ValueError("clip_id 不得重复")
        expected = [f"clip-{i:04d}" for i in range(len(value))]
        if ids != expected:
            raise ValueError(f"clip_id 必须为稳定顺序 id：{expected}")
        return value

    @field_validator("unresolved_shots")
    @classmethod
    def _unique_unresolved(cls, value: list[UnresolvedShot]) -> list[UnresolvedShot]:
        ids = [u.shot_id for u in value]
        if len(set(ids)) != len(ids):
            raise ValueError("unresolved_shots 的 shot_id 不得重复")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> TimelineSpec:
        clip_ids = {c.clip_id for c in self.clips}
        # 非 video 轨道承载的是意图 id（text/audio intent），而非片段
        intent_ids = {i.intent_id for i in self.text_intents} | {
            a.intent_id for a in self.audio_intents}
        for track in self.tracks:
            valid = clip_ids if track.kind == "video" else intent_ids
            for cid in track.clip_ids:
                if cid not in valid:
                    raise ValueError(
                        f"轨道 {track.track_id} 引用了不存在的元素：{cid!r}")
        for tr in self.transitions:
            if tr.from_clip_id not in clip_ids or tr.to_clip_id not in clip_ids:
                raise ValueError(
                    f"转场 {tr.transition_id} 引用了不存在的 clip")
        # 时间线区间必须连续且从 0 开始（无空隙、无重叠）
        cursor = 0
        for clip in sorted(self.clips, key=lambda c: c.sequence):
            if clip.timeline_start_frame != cursor:
                raise ValueError(
                    f"{clip.clip_id}: 时间线区间必须连续（期望起点 {cursor}，"
                    f"实际 {clip.timeline_start_frame}）")
            cursor = clip.timeline_end_frame
        if cursor != self.total_frames:
            raise ValueError(
                f"total_frames 必须等于各片段帧数之和：{cursor} != {self.total_frames}")
        # 片段与未解析镜头不得重叠（一个 shot 要么成片段、要么显式未解析）
        clip_shots = {c.shot_id for c in self.clips}
        unresolved = {u.shot_id for u in self.unresolved_shots}
        if clip_shots & unresolved:
            raise ValueError(
                f"shot 不能同时成片段又处于未解析：{sorted(clip_shots & unresolved)}")
        return self


# ══════════════════════════════════════════════════════════════════
# 模板清单（纯数据，不执行代码/表达式）
# ══════════════════════════════════════════════════════════════════


class TemplateSlot(_Strict):
    """模板槽位：按 `order` 从时间线中**依序**取一个匹配素材类型的片段。"""

    slot_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    media_kind: Literal["video", "image", "any"]
    min_duration_frames: int = Field(ge=0)
    max_duration_frames: int | None = None
    required: bool = True


class TemplateTextLimit(_Strict):
    """文字限制：只校验，**绝不改写**文案。"""

    kind: Literal["subtitle", "on_screen_text", "narration"]
    max_items: int = Field(gt=0)
    max_chars_per_item: int = Field(gt=0)


class TemplateManifest(_Strict):
    """模板清单（数据）。`version` 固定，变更模板必须升版本。"""

    schema_version: str = TEMPLATE_MANIFEST_SCHEMA_VERSION
    template_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    applicable_aspect_ratios: list[str] = Field(min_length=1)
    applicable_frame_rates: list[FrameRate] = Field(min_length=1)
    slots: list[TemplateSlot] = Field(min_length=1)
    text_limits: list[TemplateTextLimit] = Field(min_length=1)
    #: 缺槽策略：`warn` 仅告警；`fail` 额外把绑定标记为 `accepted=False`
    missing_slot_strategy: Literal["warn", "fail"]
    allowed_transitions: list[
        Literal["cut", "fade", "dissolve", "match_cut", "whip_pan"]
    ] = Field(min_length=1)
    layout: str = Field(min_length=1)

    @field_validator("applicable_aspect_ratios")
    @classmethod
    def _ratios(cls, value: list[str]) -> list[str]:
        import re

        for item in value:
            if not re.fullmatch(r"[1-9]\d{0,3}:[1-9]\d{0,3}", item):
                raise ValueError(f"画幅必须形如 '16:9'：{item!r}")
        if len(set(value)) != len(value):
            raise ValueError("applicable_aspect_ratios 不得重复")
        return value

    @field_validator("slots")
    @classmethod
    def _slot_orders(cls, value: list[TemplateSlot]) -> list[TemplateSlot]:
        orders = [s.order for s in value]
        if len(set(orders)) != len(orders):
            raise ValueError("slot order 不得重复")
        return sorted(value, key=lambda s: s.order)


# ══════════════════════════════════════════════════════════════════
# 模板绑定结果
# ══════════════════════════════════════════════════════════════════


class SlotBinding(_Strict):
    """槽位 → 时间线片段绑定。未满足时 `clip_id` 为 `None` 并给出原因。"""

    slot_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    clip_id: str | None = None
    satisfied: bool
    reason: str | None = None


class UnmetConstraint(_Strict):
    """未满足约束（结构化、可解释；绝不静默丢弃）。"""

    code: Literal[
        "ASPECT_UNSUPPORTED",
        "FRAME_RATE_UNSUPPORTED",
        "SLOT_UNFILLED",
        "TEXT_LIMIT_EXCEEDED",
        "TRANSITION_NOT_ALLOWED",
    ]
    subject: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class TemplateBinding(_Strict):
    """模板绑定产物：时间线 → 模板槽位的映射结果。"""

    schema_version: str = TEMPLATE_BINDING_SCHEMA_VERSION
    template_bind_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)

    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    timeline_artifact_revision_id: str = Field(min_length=1)
    script_artifact_revision_id: str = Field(min_length=1)

    #: 来自 manifest 的缺槽策略（记录下来以便审计 accepted 的语义）
    missing_slot_strategy: Literal["warn", "fail"]
    bindings: list[SlotBinding] = Field(default_factory=list)
    #: **阻断性**未满足约束（画幅/帧率不支持、文案超限、转场不支持、
    #: 必需槽位为空且策略为 fail）。WB-05 渲染前必须依据 `unmet`
    #: 阻止不兼容渲染；非阻断问题（可选槽位为空、warn 策略下的必需槽位为空）
    #: 仅以 `warnings` 表达，不进入本列表。
    unmet: list[UnmetConstraint] = Field(default_factory=list)
    #: **整体可接受**：当且仅当 `unmet` 为空（无阻断性未满足约束）。
    accepted: bool
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("bindings")
    @classmethod
    def _unique_slots(cls, value: list[SlotBinding]) -> list[SlotBinding]:
        ids = [b.slot_id for b in value]
        if len(set(ids)) != len(ids):
            raise ValueError("slot_id 不得重复绑定")
        return value

    @model_validator(mode="after")
    def _accepted_consistency(self) -> TemplateBinding:
        """`accepted` 必须与阻断性未满足约束严格一致：`accepted ⇔ unmet 为空`。"""
        if self.accepted == bool(self.unmet):
            raise ValueError(
                f"accepted 与 unmet 不一致：accepted={self.accepted}，"
                f"阻断性未满足约束 {len(self.unmet)} 项")
        return self


__all__ = [
    "ALLOWED_SPEED",
    "AudioIntent",
    "FrameRate",
    "TEMPLATE_BINDING_SCHEMA_VERSION",
    "TEMPLATE_MANIFEST_SCHEMA_VERSION",
    "TIMELINE_SCHEMA_VERSION",
    "TRACK_AUDIO",
    "TRACK_TEXT",
    "TRACK_VIDEO",
    "TemplateBinding",
    "TemplateManifest",
    "TemplateSlot",
    "TemplateTextLimit",
    "TextIntent",
    "TimelineClip",
    "TimelineSpec",
    "TimelineTrack",
    "TimelineTransition",
    "UnmetConstraint",
    "UnresolvedShot",
    "SlotBinding",
]
