"""CyberCut 领域模型（Pydantic v2）。

用途：
1. **输入契约**（`*Input`）：对外方法/测试的入参校验，`extra="forbid"` 拒绝未知字段；
2. **实体视图**（其余）：storage 层从 PG 行映射出的结构化对象（只读语义）。

字段命名与 `migrations/cybercut/0001_init.sql` 的列一一对应，便于对照审阅。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cybercut.contracts.enums import (
    ApprovalStatus,
    ArtifactStatus,
    AttemptStatus,
    ExecutorKind,
    NodeRunStatus,
    ProjectStatus,
    RunStatus,
)

_ENTITY = ConfigDict(extra="ignore")
_STRICT = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════
# 实体
# ══════════════════════════════════════════════════════════════════


class Project(BaseModel):
    model_config = _ENTITY

    project_id: str
    tenant_id: str
    name: str
    goal_json: dict[str, Any] = Field(default_factory=dict)
    automation_policy_json: dict[str, Any] = Field(default_factory=dict)
    status: ProjectStatus
    current_run_id: str | None = None
    version: int = 1
    idempotency_key: str | None = None
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Run(BaseModel):
    model_config = _ENTITY

    run_id: str
    project_id: str
    tenant_id: str
    run_no: int
    status: RunStatus
    pipeline_version: str
    requested_from_node: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NodeRun(BaseModel):
    model_config = _ENTITY

    node_run_id: str
    run_id: str
    tenant_id: str
    node_key: str
    status: NodeRunStatus
    depends_on: list[str] = Field(default_factory=list)
    input_revision_refs: dict[str, Any] = Field(default_factory=dict)
    latest_attempt_no: int = 0
    approval_required: bool = False
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NodeAttempt(BaseModel):
    model_config = _ENTITY

    attempt_id: str
    node_run_id: str
    tenant_id: str
    attempt_no: int
    status: AttemptStatus
    executor_kind: ExecutorKind
    executor_ref: str | None = None
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    available_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    progress: float = 0.0
    stage: str | None = None
    message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ArtifactRevision(BaseModel):
    model_config = _ENTITY

    artifact_revision_id: str
    project_id: str
    run_id: str
    node_run_id: str
    attempt_id: str
    tenant_id: str
    kind: str
    revision_no: int
    relative_path: str
    sha256: str
    size_bytes: int
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    supersedes_id: str | None = None
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Approval(BaseModel):
    model_config = _ENTITY

    approval_id: str
    project_id: str
    run_id: str
    node_run_id: str
    artifact_revision_id: str | None = None
    tenant_id: str
    status: ApprovalStatus
    feedback: str | None = None
    scope_json: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Event(BaseModel):
    model_config = _ENTITY

    event_id: int
    tenant_id: str
    project_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


# ══════════════════════════════════════════════════════════════════
# 输入契约
# ══════════════════════════════════════════════════════════════════


class CreateProjectInput(BaseModel):
    """创建项目入参。"""

    model_config = _STRICT

    name: str = Field(min_length=1, max_length=200)
    goal: dict[str, Any] = Field(default_factory=dict)
    automation_policy: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class StartRunInput(BaseModel):
    """启动 Run 入参。"""

    model_config = _STRICT

    pipeline_version: str = Field(default="test-pipeline-v1", min_length=1, max_length=64)
    requested_from_node: str | None = Field(default=None, max_length=64)


class ApprovalDecisionInput(BaseModel):
    """审批决定入参。

    `expected_revision` 为**乐观并发守卫**：必须与待审审批记录一致，
    否则视为过期决定（Conflict），防止基于旧产物的审批生效。
    """

    model_config = _STRICT

    approved: bool
    expected_revision: int = Field(ge=0)
    decided_by: str = Field(min_length=1, max_length=200)
    feedback: str | None = Field(default=None, max_length=4000)
    scope: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Approval",
    "ApprovalDecisionInput",
    "ArtifactRevision",
    "CreateProjectInput",
    "Event",
    "NodeAttempt",
    "NodeRun",
    "Project",
    "Run",
    "StartRunInput",
]
