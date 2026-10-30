"""WB-04 编辑规划与模板绑定层（确定性规划，不做媒体渲染）。

模块划分（ADR-0005）：
    frames.py     整数帧 ↔ 微秒换算（纯整数运算）
    templates.py  内置模板清单（纯数据）
    planner.py    TimelineSpec 规划（确定性）
    binder.py     模板绑定（确定性）
"""
from __future__ import annotations

from cybercut.editing.binder import (
    BIND_MODEL_ID,
    BIND_PROMPT_VERSION,
    bind_template,
)
from cybercut.editing.frames import (
    duration_us_for_frames,
    frames_for_duration_us,
    frames_for_interval,
)
from cybercut.editing.planner import (
    PLAN_MODEL_ID,
    PLAN_PROMPT_VERSION,
    plan_timeline,
    select_frame_rate,
)
from cybercut.editing.templates import (
    MANIFESTS,
    TEMPLATE_VERSION,
    get_manifest,
    list_manifests,
    select_default_manifest,
)

__all__ = [
    "BIND_MODEL_ID",
    "BIND_PROMPT_VERSION",
    "MANIFESTS",
    "PLAN_MODEL_ID",
    "PLAN_PROMPT_VERSION",
    "TEMPLATE_VERSION",
    "bind_template",
    "duration_us_for_frames",
    "frames_for_duration_us",
    "frames_for_interval",
    "get_manifest",
    "list_manifests",
    "plan_timeline",
    "select_default_manifest",
    "select_frame_rate",
]
