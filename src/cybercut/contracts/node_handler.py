"""NodeHandler 协议（CyberCut 内部协议，**不使用 FlowMind `@skill`**）。

与基座的关系（ADR-0002）：
- 内部节点**不注册**进 FlowMind `_REGISTRY`，因此不会被基座 `register_all()`
  暴露为 MCP 工具（基座无「公开白名单」机制，只能靠「不注册」隔离）；
- `NodeResult.artifacts` 只**描述待登记文件**，**不直接写数据库**；
  由 executor 在同一事务内完成 ArtifactRevision 登记（哈希/大小在此阶段计算）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class NodeContext:
    """NodeHandler 执行上下文（由 executor 注入）。

    - `project_root`：`data/projects/{project_id}/`，已创建并完成路径校验；
    - `attempt_dir`：本 attempt 的**独占暂存目录** `{project_root}/.attempts/{attempt_id}/`。
      handler **只能**把待发布产物写入该目录，并在 `ArtifactSpec.staged_relative_path`
      中以**相对该目录**的路径声明；正式不可变路径由执行器分配（WB-01R3）。该目录在 attempt 结束时清理；
    - `progress_cb`：进度回调（落库 + 事件），非阻塞、失败不抛出；
    - `cancel_check`：协作式取消检查；返回 True 表示应尽快退出。
    """

    attempt_id: str
    node_run_id: str
    run_id: str
    project_id: str
    tenant_id: str
    node_key: str
    attempt_no: int
    project_root: Path
    data_root: Path
    worker_id: str
    #: attempt 独占暂存目录（handler 只能写入此处；attempt 结束即清理）
    attempt_dir: Path
    progress_cb: Callable[[float, str, str], None] = field(
        default=lambda progress, stage, message: None
    )
    cancel_check: Callable[[], bool] = field(default=lambda: False)


class ArtifactSpec(BaseModel):
    """**暂存**产物描述（WB-01R3）。

    - `staged_relative_path` 必须指向 `NodeContext.attempt_dir` **内**的文件；
    - handler **不得**决定正式路径：正式不可变路径由执行器在提交阶段分配
      （`data/projects/{project_id}/artifacts/{artifact_revision_id}/{safe_filename}`）；
    - 哈希与大小由执行器从 staged 文件计算。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    #: 相对 `NodeContext.attempt_dir` 的暂存路径（不允许绝对路径 / 越界）
    staged_relative_path: str = Field(min_length=1, max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeResult(BaseModel):
    """NodeHandler 执行结果。"""

    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: True 表示本节点产物需人工审批（executor 将 node_run 置 waiting_approval）
    awaiting_approval: bool = False


@runtime_checkable
class NodeHandler(Protocol):
    """节点执行器协议。

    实现方需提供 `node_key`（或 `node_type`）与 `execute(context, input_json)`。
    """

    node_key: str

    def execute(self, context: NodeContext, input_json: dict[str, Any]) -> NodeResult:
        """执行节点；失败时抛 `NodeExecutionError`。"""
        ...


def handler_key(handler: NodeHandler) -> str:
    """取 handler 的 node_key（兼容 node_type 别名）。"""
    key = getattr(handler, "node_key", None) or getattr(handler, "node_type", None)
    if not isinstance(key, str) or not key:
        raise TypeError("NodeHandler 必须定义非空 node_key（或 node_type）")
    return key


__all__ = [
    "ArtifactSpec",
    "NodeContext",
    "NodeHandler",
    "NodeResult",
    "handler_key",
]
