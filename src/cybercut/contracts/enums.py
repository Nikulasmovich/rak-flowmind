"""CyberCut 领域枚举（状态机取值真源）。

所有状态取值集中于此，供 models / state_machine / storage / executor 复用，
避免字符串字面量散落各处导致口径漂移。
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类（值即字面量，便于直接落库与断言）。"""

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return self.value


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class NodeRunStatus(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutorKind(StrEnum):
    """执行器种类：CyberCut 自建 worker 为默认；flowmind_task 仅作可选适配。"""

    CYBERCUT_WORKER = "cybercut_worker"
    FLOWMIND_TASK = "flowmind_task"


class ArtifactStatus(StrEnum):
    ACTIVE = "active"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# ── 终态集合（供聚合与守卫使用）──
PROJECT_TERMINAL = frozenset(
    {ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED}
)
RUN_TERMINAL = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
)
NODE_RUN_TERMINAL = frozenset(
    {NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED, NodeRunStatus.CANCELLED}
)
ATTEMPT_TERMINAL = frozenset(
    {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED, AttemptStatus.CANCELLED}
)
ATTEMPT_ACTIVE = frozenset(
    {AttemptStatus.QUEUED, AttemptStatus.CLAIMED, AttemptStatus.RUNNING}
)

__all__ = [
    "ApprovalStatus",
    "ArtifactStatus",
    "AttemptStatus",
    "ATTEMPT_ACTIVE",
    "ATTEMPT_TERMINAL",
    "ExecutorKind",
    "NODE_RUN_TERMINAL",
    "NodeRunStatus",
    "PROJECT_TERMINAL",
    "ProjectStatus",
    "RUN_TERMINAL",
    "RunStatus",
    "StrEnum",
]
