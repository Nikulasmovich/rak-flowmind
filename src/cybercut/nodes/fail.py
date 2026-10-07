"""测试用可控失败 handler。

`fail_times=None`（默认）表示**总是失败**；传入整数 N 表示前 N 次调用失败、
之后成功（用于验证「重试创建新 attempt 后成功」的路径）。
"""
from __future__ import annotations

from typing import Any

from cybercut.contracts.errors import NodeExecutionError
from cybercut.contracts.node_handler import NodeContext, NodeResult


class FailingHandler:
    """按调用次数可控失败的 handler。"""

    def __init__(
        self,
        node_key: str,
        *,
        error_code: str = "CONTROLLED_FAILURE",
        message: str = "受控失败（测试用）",
        fail_times: int | None = None,
    ) -> None:
        self.node_key = node_key
        self.error_code = error_code
        self.message = message
        self.fail_times = fail_times
        self.calls = 0

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        self.calls += 1
        if self.fail_times is None or self.calls <= self.fail_times:
            raise NodeExecutionError(
                f"{self.message}（第 {self.calls} 次调用）",
                error_code=self.error_code,
                details={"call_no": self.calls, "fail_times": self.fail_times},
            )
        return NodeResult(output={"node_key": self.node_key, "call_no": self.calls})


__all__ = ["FailingHandler"]
