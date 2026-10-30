"""集中状态机：显式转换表 + 校验函数。

设计要点（WB-01 §四）：
- **所有**状态转换必须经本模块校验；非法转换抛 `InvalidTransitionError`，绝不静默忽略。
- 转换表是本模块的唯一真源（storage / executor / orchestrator 均引用 here，不各自硬编码）。
- 表即文档：可直接对照 WB-01 规格逐条校对。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from cybercut.contracts.enums import (
    ApprovalStatus,
    AttemptStatus,
    NodeRunStatus,
    ProjectStatus,
    RunStatus,
)
from cybercut.contracts.errors import InvalidTransitionError

# ── 实体名（用于异常信息与分派）──
PROJECT = "project"
RUN = "run"
NODE_RUN = "node_run"
NODE_ATTEMPT = "node_attempt"
APPROVAL = "approval"


def _t(**kwargs: list[str]) -> Mapping[str, frozenset[str]]:
    return MappingProxyType({k: frozenset(v) for k, v in kwargs.items()})


# ══════════════════════════════════════════════════════════════════
# 转换表（from -> 允许到达的 to 集合）
# ══════════════════════════════════════════════════════════════════

PROJECT_TRANSITIONS = _t(
    draft=[ProjectStatus.ACTIVE],
    active=[
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    ],
    # completed / failed / cancelled 为终态：无出边。
    # WB-03R2：定向修订**不通过**通用状态迁移开放终态出边，
    # 而是走受控专用路径 `projects.reopen_for_revision()`（见 ADR-0004 决策 31）。
    completed=[],
    failed=[],
    cancelled=[],
)

RUN_TRANSITIONS = _t(
    queued=[RunStatus.RUNNING],
    running=[
        RunStatus.WAITING_APPROVAL,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    ],
    waiting_approval=[
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    ],
    cancelling=[RunStatus.CANCELLED],
    # 终态无出边。WB-03R2：定向修订**不通过**通用状态迁移开放终态出边，
    # 而是走受控专用路径 `runs.reopen_for_revision()`（见 ADR-0004 决策 31）。
    succeeded=[],
    failed=[],
    cancelled=[],
)

NODE_RUN_TRANSITIONS = _t(
    blocked=[NodeRunStatus.READY],
    ready=[NodeRunStatus.RUNNING],
    running=[
        NodeRunStatus.WAITING_APPROVAL,
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.FAILED,
        NodeRunStatus.CANCELLED,
    ],
    waiting_approval=[
        NodeRunStatus.READY,
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.CANCELLED,
    ],
    succeeded=[NodeRunStatus.INVALIDATED],
    invalidated=[NodeRunStatus.READY],
    # failed -> ready 仅在「显式重试」时使用（见 orchestrator.retry）
    failed=[NodeRunStatus.READY],
    cancelled=[],
)

NODE_ATTEMPT_TRANSITIONS = _t(
    queued=[AttemptStatus.CLAIMED, AttemptStatus.CANCELLED],
    claimed=[
        AttemptStatus.QUEUED,  # 租约超时恢复
        AttemptStatus.RUNNING,
        AttemptStatus.CANCELLED,
    ],
    running=[
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
    ],
    # 终态不得再次改变
    succeeded=[],
    failed=[],
    cancelled=[],
)

APPROVAL_TRANSITIONS = _t(
    pending=[
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
    ],
    approved=[],
    rejected=[],
    cancelled=[],
)

_TABLES: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        PROJECT: PROJECT_TRANSITIONS,
        RUN: RUN_TRANSITIONS,
        NODE_RUN: NODE_RUN_TRANSITIONS,
        NODE_ATTEMPT: NODE_ATTEMPT_TRANSITIONS,
        APPROVAL: APPROVAL_TRANSITIONS,
    }
)

#: 各实体终态（无出边）
TERMINAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        entity: frozenset(s for s, outs in table.items() if not outs)
        for entity, table in _TABLES.items()
    }
)


# ══════════════════════════════════════════════════════════════════
# 校验 API
# ══════════════════════════════════════════════════════════════════


def _normalize(value: object) -> str:
    """枚举或字符串统一为字符串值。"""
    return value.value if hasattr(value, "value") else str(value)


def allowed_from(entity: str, current: object) -> frozenset[str]:
    """返回 `current` 允许迁移到的状态集合（未知实体/状态返回空集）。"""
    table = _TABLES.get(entity)
    if table is None:
        return frozenset()
    return table.get(_normalize(current), frozenset())


def can_transition(entity: str, current: object, target: object) -> bool:
    """判断转换是否合法（不抛异常）。"""
    return _normalize(target) in allowed_from(entity, current)


def assert_transition(entity: str, current: object, target: object) -> None:
    """断言转换合法；非法则抛 `InvalidTransitionError`。

    幂等自反（current == target）**不**视为合法——状态机不做无操作迁移，
    避免掩盖逻辑错误（如需幂等返回，由调用方先行判断）。
    """
    if entity not in _TABLES:
        raise InvalidTransitionError(f"未知实体：{entity!r}")
    cur, tgt = _normalize(current), _normalize(target)
    if tgt not in allowed_from(entity, cur):
        raise InvalidTransitionError(
            f"非法状态转换：{entity} {cur!r} -> {tgt!r}"
            f"（允许：{sorted(allowed_from(entity, cur))}）",
            details={
                "entity": entity,
                "from": cur,
                "to": tgt,
                "allowed": sorted(allowed_from(entity, cur)),
            },
        )


def is_terminal(entity: str, status: object) -> bool:
    """该实体的该状态是否为终态。"""
    return _normalize(status) in TERMINAL.get(entity, frozenset())


__all__ = [
    "APPROVAL",
    "APPROVAL_TRANSITIONS",
    "NODE_ATTEMPT",
    "NODE_ATTEMPT_TRANSITIONS",
    "NODE_RUN",
    "NODE_RUN_TRANSITIONS",
    "PROJECT",
    "PROJECT_TRANSITIONS",
    "RUN",
    "RUN_TRANSITIONS",
    "TERMINAL",
    "allowed_from",
    "assert_transition",
    "can_transition",
    "is_terminal",
]
