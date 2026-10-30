"""WB-03 创作层节点注册表（不注册 FlowMind `@skill`）。

- `build_creative_registry`：只注册四个创作节点；
- `build_creative_full_registry`：媒体五节点 + 创作四节点（供创作管线端到端运行）。
"""
from __future__ import annotations

from typing import Any

from cybercut.creative.policy import AUTO_POLICY, CREATIVE_NODE_KEYS, ApprovalPolicy
from cybercut.creative.provider import CreativeProvider
from cybercut.nodes.creative_nodes import (
    CreativeBriefHandler,
    CreativeMatchHandler,
    CreativeScriptHandler,
    CreativeStoryboardHandler,
)
from cybercut.nodes.edit_registry import build_edit_registry
from cybercut.nodes.media_registry import build_media_registry
from cybercut.nodes.registry import NodeRegistry
from cybercut.nodes.render_registry import build_render_registry


def build_creative_registry(
    *,
    provider: CreativeProvider | None = None,
    policy: ApprovalPolicy | None = None,
) -> NodeRegistry:
    """注册四个创作 Handler；`provider` 可注入 fake 或真实实现。"""
    registry = NodeRegistry()
    resolved = policy or AUTO_POLICY
    for handler_cls in (CreativeBriefHandler, CreativeScriptHandler,
                        CreativeStoryboardHandler, CreativeMatchHandler):
        registry.register(handler_cls(provider=provider, policy=resolved))
    return registry


def build_creative_full_registry(
    *,
    provider: CreativeProvider | None = None,
    policy: ApprovalPolicy | None = None,
    toolchain: Any | None = None,
    detector: Any | None = None,
    asr_adapter: Any | None = None,
    require_asr: bool = True,
    funasr_model_path: str | None = None,
) -> NodeRegistry:
    """媒体五节点 + 创作四节点（同一注册表，供 `CREATIVE_PIPELINE` 使用）。"""
    media = build_media_registry(
        toolchain=toolchain, detector=detector, asr_adapter=asr_adapter,
        require_asr=require_asr, funasr_model_path=funasr_model_path,
    )
    registry = NodeRegistry()
    for key in media.keys():
        registry.register(media.get(key))
    creative = build_creative_registry(provider=provider, policy=policy)
    for key in creative.keys():
        registry.register(creative.get(key))
    edit = build_edit_registry(policy=policy)
    for key in edit.keys():
        registry.register(edit.get(key))
    render = build_render_registry(policy=policy)
    for key in render.keys():
        registry.register(render.get(key))
    return registry


__all__ = [
    "CREATIVE_NODE_KEYS",
    "build_creative_full_registry",
    "build_creative_registry",
]
