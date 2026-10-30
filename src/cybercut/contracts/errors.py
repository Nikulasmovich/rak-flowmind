"""CyberCut 领域异常。

约定：所有领域层错误均继承 `CyberCutError`；对外语义映射（示例）：
- `NotFoundError`          -> 资源不存在或不属于当前租户（**统一表现为 not found**，不区分）
- `ConflictError`          -> 乐观锁 / 幂等 / 唯一约束 / expected_revision 冲突
- `InvalidTransitionError` -> 非法状态转换（**绝不静默忽略**）
- `PathEscapeError`        -> relative_path 逃出项目目录
- `NodeExecutionError`     -> NodeHandler 主动失败（携带 error_code）
- `HandlerNotFoundError`   -> 未知 node_key / handler
- `ValidationError`        -> 入参非法
"""
from __future__ import annotations


class CyberCutError(Exception):
    """CyberCut 领域错误基类。"""

    #: 对外错误码（HTTP/事件载荷可直接使用）
    code: str = "CYBERCUT_ERROR"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(CyberCutError):
    code = "NOT_FOUND"


class ConflictError(CyberCutError):
    code = "CONFLICT"


class InvalidTransitionError(CyberCutError):
    code = "INVALID_TRANSITION"


class PathEscapeError(CyberCutError):
    code = "PATH_ESCAPE"


class ValidationError(CyberCutError):
    code = "VALIDATION"


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"


class ArtifactImmutableError(ConflictError):
    code = "ARTIFACT_IMMUTABLE"


class ArtifactOwnershipError(ConflictError):
    """产物路径不归当前 attempt 所有（历史文件 / 他人 attempt 文件 / 落在独占工作目录内）。

    WB-01R2 §一：拒绝提交，且清理时不得删除。
    """

    code = "ARTIFACT_NOT_OWNED"


class ArtifactHistoricalError(ConflictError):
    """产物路径已被某条 ArtifactRevision 引用（历史正式产物）：禁止覆盖/删除/重新声明。"""

    code = "ARTIFACT_HISTORICAL"


class ArtifactPublishError(ConflictError):
    """发布正式路径时发生冲突（目标已存在）：绝不覆盖既有文件或目录。"""

    code = "ARTIFACT_PUBLISH_CONFLICT"


class HeartbeatStuckError(CyberCutError):
    """心跳线程无法可靠停止 —— **worker 进程级致命错误**。

    此时不能再继续领取新任务（心跳线程可能仍在访问数据库资源）。
    `AttemptRunner.execute` 会直接抛出该异常，而不是把它降级为普通 attempt 失败。
    """

    code = "HEARTBEAT_STUCK"


class LeaseConflictError(ConflictError):
    code = "LEASE_CONFLICT"


class HandlerNotFoundError(CyberCutError):
    code = "HANDLER_NOT_FOUND"


class NodeExecutionError(CyberCutError):
    """NodeHandler 主动抛出的执行失败（携带稳定 error_code 落库）。"""

    code = "NODE_EXECUTION_FAILED"

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "NODE_EXECUTION_FAILED",
        details: dict | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.error_code = error_code


class WorkerLostError(CyberCutError):
    """worker 心跳超时（running 无法确知副作用，不得直接重跑）。"""

    code = "WORKER_LOST"


__all__ = [
    "ArtifactHistoricalError",
    "ArtifactImmutableError",
    "ArtifactOwnershipError",
    "ArtifactPublishError",
    "ConflictError",
    "CyberCutError",
    "HandlerNotFoundError",
    "HeartbeatStuckError",
    "CyberCutError",
    "HandlerNotFoundError",
    "IdempotencyConflictError",
    "InvalidTransitionError",
    "LeaseConflictError",
    "NodeExecutionError",
    "NotFoundError",
    "PathEscapeError",
    "ValidationError",
    "WorkerLostError",
]
