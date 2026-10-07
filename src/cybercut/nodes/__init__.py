"""CyberCut 内部节点实现（NodeHandler，**不注册进 FlowMind registry**）。

本阶段仅提供测试用 handler：
- `echo`            : 产出一个小文本产物，用于验证执行器闭环；
- `silent`          : 长时间执行且**不报进度**（验证独立周期心跳）；
- `fail`            : 可控失败（指定 error_code），验证失败路径与重试；
- `approval_gate`   : 产出待审产物并声明 `awaiting_approval=True`。
"""
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.fail import FailingHandler
from cybercut.nodes.gate import ApprovalGateHandler
from cybercut.nodes.registry import NodeRegistry, build_test_registry
from cybercut.nodes.silent import SilentHandler

__all__ = [
    "ApprovalGateHandler",
    "EchoHandler",
    "FailingHandler",
    "NodeRegistry",
    "SilentHandler",
    "build_test_registry",
]
