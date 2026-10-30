"""WB-05 渲染节点注册表。"""
from __future__ import annotations

from cybercut.creative.policy import AUTO_POLICY, ApprovalPolicy
from cybercut.nodes.registry import NodeRegistry
from cybercut.nodes.render_nodes import (
    RenderCompositeHandler,
    RenderNarrationHandler,
    RenderQcHandler,
)

RENDER_NODE_KEYS = ("render.narration", "render.composite", "render.qc")


def build_render_registry(*, policy: ApprovalPolicy | None = None) -> NodeRegistry:
    registry = NodeRegistry()
    resolved = policy or AUTO_POLICY
    for handler_cls in (RenderNarrationHandler, RenderCompositeHandler,
                        RenderQcHandler):
        registry.register(handler_cls(policy=resolved))
    return registry


__all__ = ["RENDER_NODE_KEYS", "build_render_registry"]
