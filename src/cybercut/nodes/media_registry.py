"""WB-02 媒体节点执行器注册表（不注册 FlowMind @skill，WB-02R §三）。"""
from __future__ import annotations

from typing import Any

from cybercut.media.asr import AsrAdapter
from cybercut.media.scenes import SceneDetector
from cybercut.media.toolchain import FFmpegToolchain
from cybercut.nodes.media_nodes import (
    AssetAnalysisHandler,
    AssetNormalizeHandler,
    AssetProbeHandler,
    SceneDetectHandler,
    SpeechTranscribeHandler,
)
from cybercut.nodes.registry import NodeRegistry

MEDIA_NODE_KEYS = (
    "asset_probe",
    "asset_normalize",
    "scene_detect",
    "speech_transcribe",
    "asset_analysis",
)


def build_media_registry(
    *,
    toolchain: FFmpegToolchain | None = None,
    detector: SceneDetector | None = None,
    asr_adapter: Any | None = None,
    require_asr: bool = True,
    funasr_model_path: str | None = None,
) -> NodeRegistry:
    """注册五个媒体 Handler；`toolchain`/`detector`/`asr_adapter` 均可注入 fake。"""
    registry = NodeRegistry()
    chain = toolchain if toolchain is not None else FFmpegToolchain()
    detector_obj = detector if detector is not None else _default_detector()
    asr = asr_adapter if asr_adapter is not None else _default_asr(
        require_asr, funasr_model_path
    )

    registry.register(AssetProbeHandler(toolchain=chain))
    registry.register(AssetNormalizeHandler(toolchain=chain))
    registry.register(SceneDetectHandler(detector=detector_obj, toolchain=chain))
    registry.register(
        SpeechTranscribeHandler(asr_adapter=asr, toolchain=chain,
                                require_asr=require_asr)
    )
    registry.register(AssetAnalysisHandler())
    return registry


def _default_detector() -> SceneDetector:
    from cybercut.media.scenes import PySceneDetectAdapter

    return PySceneDetectAdapter()


def _default_asr(require_asr: bool, model_path: str | None) -> AsrAdapter | None:
    """WB-02R §四.7：仅在提供**本地模型路径**时才构造 FunASRAdapter。"""
    if model_path is None:
        return None
    from cybercut.media.asr import FunASRAdapter

    return FunASRAdapter(model_path=model_path)


__all__ = ["MEDIA_NODE_KEYS", "build_media_registry"]
