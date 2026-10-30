"""WB-05 渲染层（FFmpeg 首版 Provider，确定性合成）。

模块划分（ADR-0006）：
    frames.py           （继承自 WB-04）
    ffmpeg_renderer.py  FFmpeg 三阶段合成
    qc.py               技术 QC 检查
    provider.py         Provider 协议（首版为 FFmpeg 本地实现）
"""
from __future__ import annotations

from cybercut.rendering.ffmpeg_renderer import (
    COMPOSITE_MODEL_ID,
    COMPOSITE_PROMPT_VERSION,
    DEFAULT_FONT_PATH,
    render_spec,
)
from cybercut.rendering.qc import extract_cover, probe_format, run_qc

__all__ = [
    "COMPOSITE_MODEL_ID",
    "COMPOSITE_PROMPT_VERSION",
    "DEFAULT_FONT_PATH",
    "extract_cover",
    "probe_format",
    "render_spec",
    "run_qc",
]
