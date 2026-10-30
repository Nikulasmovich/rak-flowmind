"""NodeHandler 注册表（CyberCut 内部，与 FlowMind `_REGISTRY` 无关）。

隔离原理：内部 handler **不写入** `flowmind/skills/`、**不追加** `flowmind/skills/__init__.py`，
因此基座 `register_all()` 无法感知它们，也就不会被暴露为 MCP 工具。
"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.node_handler import NodeHandler, handler_key


class NodeRegistry:
    """node_key -> handler 映射（进程内）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, NodeHandler] = {}

    def register(self, handler: NodeHandler) -> None:
        key = handler_key(handler)
        if key in self._handlers:
            raise ValueError(f"node_key 已注册，禁止重复：{key!r}")
        self._handlers[key] = handler

    def get(self, node_key: str) -> NodeHandler | None:
        return self._handlers.get(node_key)

    def keys(self) -> list[str]:
        return sorted(self._handlers)

    def __contains__(self, node_key: object) -> bool:
        return node_key in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)


def build_test_registry(**kwargs: Any) -> NodeRegistry:
    """构造测试用注册表（echo_b / fail_b / approval_gate 三个 handler）。

    `echo` 与 `approval_gate` 的 node_key 与 `orchestrator.pipeline` 的测试管线对齐。
    """
    from cybercut.nodes.echo import EchoHandler
    from cybercut.nodes.fail import FailingHandler
    from cybercut.nodes.gate import ApprovalGateHandler

    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    registry.register(EchoHandler("node_b"))
    registry.register(ApprovalGateHandler("approval_gate"))
    registry.register(EchoHandler("node_c"))
    # 可控失败 handler 按需注入（默认不注册，供失败路径测试显式使用）
    fail_code = kwargs.get("fail_code")
    fail_key = kwargs.get("fail_node_key")
    if fail_key:
        registry.register(FailingHandler(fail_key, error_code=fail_code or "CONTROLLED_FAILURE"))
    return registry


__all__ = ["NodeRegistry", "build_test_registry"]
