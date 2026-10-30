"""WB-04 编辑节点注册表（不注册 FlowMind `@skill`）。

- `build_edit_registry`：只注册两个编辑节点；
- `build_creative_full_registry` 另见 `creative_registry`（媒体 + 创作 + 编辑）。
"""
from __future__ import annotations

from cybercut.creative.policy import AUTO_POLICY, ApprovalPolicy
from cybercut.nodes.edit_nodes import EditPlanHandler, TemplateBindHandler
from cybercut.nodes.registry import NodeRegistry

EDIT_NODE_KEYS = ("edit.plan", "template.bind")


def build_edit_registry(*, policy: ApprovalPolicy | None = None) -> NodeRegistry:
    """注册两个编辑 Handler。"""
    registry = NodeRegistry()
    resolved = policy or AUTO_POLICY
    for handler_cls in (EditPlanHandler, TemplateBindHandler):
        registry.register(handler_cls(policy=resolved))
    return registry


__all__ = ["EDIT_NODE_KEYS", "build_edit_registry"]
