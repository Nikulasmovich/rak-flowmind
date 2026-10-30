"""WB-05 渲染层契约（Pydantic v2，extra=forbid）。

设计原则（ADR-0006）：
- 渲染是**确定性变换**：TimelineSpec + TemplateBinding + 源素材 → MP4；
- 渲染器**不得**改写镜头顺序、素材选择、入出点或文案；
- QC 结果逐项可审计；QC 失败 ⇒ 不交付；
- 前置门槛（accepted / unresolved_shots）在节点入口校验。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from cybercut.contracts.editing import _Strict

RENDER_SCHEMA_VERSION = "render-output.v1"
QC_SCHEMA_VERSION = "render-qc.v1"


class NarrationOutput(_Strict):
    """旁白音轨输出（本版无 TTS ⇒ not_available）。"""

    status: Literal["not_available", "generated"]
    reason: str = Field(min_length=1)
    audio_paths: list[str] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RenderOutput(_Strict):
    """渲染合成产物。"""

    schema_version: str = RENDER_SCHEMA_VERSION
    render_composite_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)

    timeline_artifact_revision_id: str = Field(min_length=1)
    template_bind_artifact_revision_id: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    template_version: str = Field(min_length=1)
    renderer_id: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)

    output_path: str = Field(min_length=1)
    output_format: Literal["mp4"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate_num: int = Field(gt=0)
    frame_rate_den: int = Field(gt=0)
    total_frames: int = Field(ge=0)
    duration_us: int = Field(ge=0)

    narration: NarrationOutput
    bgm_status: Literal["not_provided", "mixed"]
    clip_count: int = Field(ge=0)
    unresolved_shot_count: int = Field(ge=0)

    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    revision_note: str | None = None
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)


class QcCheck(_Strict):
    """单项 QC 检查结果。"""

    check_id: str = Field(min_length=1)
    passed: bool
    detail: str = Field(min_length=1)


class QcReport(_Strict):
    """技术 QC 报告。"""

    schema_version: str = QC_SCHEMA_VERSION
    qc_revision_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    revision_no: int = Field(ge=1)

    composite_artifact_revision_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate_num: int = Field(gt=0)
    frame_rate_den: int = Field(gt=0)
    duration_us: int = Field(ge=0)
    has_video: bool
    has_audio: bool
    video_codec: str = Field(min_length=1)
    audio_codec: str = Field(min_length=1)

    checks: list[QcCheck] = Field(min_length=1)
    all_passed: bool
    cover_path: str = Field(min_length=1)

    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "NarrationOutput",
    "QcCheck",
    "QC_SCHEMA_VERSION",
    "QcReport",
    "RENDER_SCHEMA_VERSION",
    "RenderOutput",
]
