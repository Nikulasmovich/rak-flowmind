"""WB-02 媒体层契约（Pydantic v2，extra=forbid，时间统一整数微秒）。

规则：
- 时间区间统一为**半开区间** `[start_us, end_us)`；
- 图片没有自然时长：时间字段使用 `None`，不伪造视频时长；
- 分数/帧率/time_base 使用**有理数**（num/den）表示，拒绝 NaN / Infinity；
- 所有 JSON 输出稳定、可复现，并携带 `schema_version`。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MEDIA_SCHEMA_VERSION = "media.v1"
ANALYSIS_SCHEMA_VERSION = "asset-analysis.v1"

MediaKind = Literal["video", "image"]
Rational = dict[str, int]


def _no_nan_inf(value: Any) -> Any:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("禁止 NaN / Infinity")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class VideoStreamInfo(_Strict):
    index: int
    codec_name: str
    codec_type: Literal["video"]
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    pix_fmt: str | None = None
    avg_frame_rate: Rational | None = None
    r_frame_rate: Rational | None = None
    time_base: Rational | None = None
    duration_us: int | None = Field(default=None, ge=0)
    nb_frames: int | None = Field(default=None, ge=0)
    rotation: int | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _v(cls, value: Any) -> Any:
        return _no_nan_inf(value)


class AudioStreamInfo(_Strict):
    index: int
    codec_name: str
    codec_type: Literal["audio"]
    sample_rate: int | None = Field(default=None, ge=0)
    channels: int | None = Field(default=None, ge=0)
    time_base: Rational | None = None
    duration_us: int | None = Field(default=None, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def _v(cls, value: Any) -> Any:
        return _no_nan_inf(value)


class MediaProbeReport(_Strict):
    """ffprobe 结构化结果（确定性媒体事实）。"""

    schema_version: str = MEDIA_SCHEMA_VERSION
    media_kind: MediaKind
    format_name: str | None = None
    duration_us: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    bit_rate: int | None = Field(default=None, ge=0)
    video: VideoStreamInfo | None = None
    audio: AudioStreamInfo | None = None
    has_audio: bool
    has_video: bool
    rotation: int | None = None
    raw_format_tags: dict[str, str] = Field(default_factory=dict)


class TranscriptSegment(_Strict):
    """ASR 文本片段（半开区间，微秒）。"""

    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    text: str = Field(min_length=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("end_us")
    @classmethod
    def _end_after_start(cls, value: int, info) -> int:
        start = info.data.get("start_us")
        if start is not None and value < start:
            raise ValueError("end_us 必须大于等于 start_us")
        return value


class AssetSegment(_Strict):
    """镜头/分段（半开区间，微秒；图片为单段非时间型 segment）。"""

    segment_index: int = Field(ge=0)
    start_us: int = Field(ge=0)
    end_us: int = Field(ge=0)
    is_time_based: bool
    keyframe_artifact_revision_id: str | None = None
    source: Literal["scene_detect", "single_frame"]

    @field_validator("end_us")
    @classmethod
    def _end_after_start(cls, value: int, info) -> int:
        start = info.data.get("start_us")
        if start is not None and value < start:
            raise ValueError("end_us 必须大于等于 start_us")
        return value


class AssetAnalysisReport(_Strict):
    """素材分析报告：**只**合并确定性媒体事实，不含任何 LLM 生成内容。"""

    schema_version: str = ANALYSIS_SCHEMA_VERSION
    asset_id: str
    media_kind: MediaKind
    duration_us: int | None = Field(default=None, ge=0)
    probe: MediaProbeReport
    segments: list[AssetSegment] = Field(default_factory=list)
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    keyframe_artifact_revision_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ToolCapabilityReport(_Strict):
    """ffmpeg/ffprobe 能力报告。"""

    schema_version: str = MEDIA_SCHEMA_VERSION
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    ffmpeg_version: str | None = None
    ffprobe_version: str | None = None
    encoders: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    available: bool
    missing_reason: str | None = None


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "MEDIA_SCHEMA_VERSION",
    "AssetAnalysisReport",
    "AssetSegment",
    "AudioStreamInfo",
    "MediaKind",
    "MediaProbeReport",
    "ToolCapabilityReport",
    "TranscriptSegment",
    "VideoStreamInfo",
]
