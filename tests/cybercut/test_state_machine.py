"""状态机单元测试（不依赖数据库）。"""
from __future__ import annotations

import pytest

from cybercut.contracts import state_machine as sm
from cybercut.contracts.enums import (
    ApprovalStatus,
    AttemptStatus,
    NodeRunStatus,
    ProjectStatus,
    RunStatus,
)
from cybercut.contracts.errors import InvalidTransitionError

# ── 合法转换 ──

@pytest.mark.parametrize(
    ("entity", "frm", "to"),
    [
        (sm.PROJECT, ProjectStatus.DRAFT, ProjectStatus.ACTIVE),
        (sm.PROJECT, ProjectStatus.ACTIVE, ProjectStatus.COMPLETED),
        (sm.PROJECT, ProjectStatus.ACTIVE, ProjectStatus.FAILED),
        (sm.PROJECT, ProjectStatus.ACTIVE, ProjectStatus.CANCELLED),
        (sm.RUN, RunStatus.QUEUED, RunStatus.RUNNING),
        (sm.RUN, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL),
        (sm.RUN, RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (sm.RUN, RunStatus.RUNNING, RunStatus.FAILED),
        (sm.RUN, RunStatus.RUNNING, RunStatus.CANCELLING),
        (sm.RUN, RunStatus.WAITING_APPROVAL, RunStatus.RUNNING),
        (sm.RUN, RunStatus.WAITING_APPROVAL, RunStatus.FAILED),
        (sm.RUN, RunStatus.WAITING_APPROVAL, RunStatus.CANCELLING),
        (sm.RUN, RunStatus.CANCELLING, RunStatus.CANCELLED),
        (sm.NODE_RUN, NodeRunStatus.BLOCKED, NodeRunStatus.READY),
        (sm.NODE_RUN, NodeRunStatus.READY, NodeRunStatus.RUNNING),
        (sm.NODE_RUN, NodeRunStatus.RUNNING, NodeRunStatus.WAITING_APPROVAL),
        (sm.NODE_RUN, NodeRunStatus.RUNNING, NodeRunStatus.SUCCEEDED),
        (sm.NODE_RUN, NodeRunStatus.RUNNING, NodeRunStatus.FAILED),
        (sm.NODE_RUN, NodeRunStatus.RUNNING, NodeRunStatus.CANCELLED),
        (sm.NODE_RUN, NodeRunStatus.WAITING_APPROVAL, NodeRunStatus.READY),
        (sm.NODE_RUN, NodeRunStatus.WAITING_APPROVAL, NodeRunStatus.SUCCEEDED),
        (sm.NODE_RUN, NodeRunStatus.WAITING_APPROVAL, NodeRunStatus.CANCELLED),
        (sm.NODE_RUN, NodeRunStatus.SUCCEEDED, NodeRunStatus.INVALIDATED),
        (sm.NODE_RUN, NodeRunStatus.INVALIDATED, NodeRunStatus.READY),
        (sm.NODE_RUN, NodeRunStatus.FAILED, NodeRunStatus.READY),
        (sm.NODE_ATTEMPT, AttemptStatus.QUEUED, AttemptStatus.CLAIMED),
        (sm.NODE_ATTEMPT, AttemptStatus.QUEUED, AttemptStatus.CANCELLED),
        (sm.NODE_ATTEMPT, AttemptStatus.CLAIMED, AttemptStatus.QUEUED),
        (sm.NODE_ATTEMPT, AttemptStatus.CLAIMED, AttemptStatus.RUNNING),
        (sm.NODE_ATTEMPT, AttemptStatus.CLAIMED, AttemptStatus.CANCELLED),
        (sm.NODE_ATTEMPT, AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED),
        (sm.NODE_ATTEMPT, AttemptStatus.RUNNING, AttemptStatus.FAILED),
        (sm.NODE_ATTEMPT, AttemptStatus.RUNNING, AttemptStatus.CANCELLED),
        (sm.APPROVAL, ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
        (sm.APPROVAL, ApprovalStatus.PENDING, ApprovalStatus.REJECTED),
        (sm.APPROVAL, ApprovalStatus.PENDING, ApprovalStatus.CANCELLED),
    ],
)
def test_legal_transitions(entity: str, frm: object, to: object) -> None:
    sm.assert_transition(entity, frm, to)
    assert sm.can_transition(entity, frm, to)


# ── 非法转换（必须抛异常，不得静默忽略）──

@pytest.mark.parametrize(
    ("entity", "frm", "to"),
    [
        (sm.PROJECT, ProjectStatus.DRAFT, ProjectStatus.COMPLETED),
        (sm.PROJECT, ProjectStatus.COMPLETED, ProjectStatus.ACTIVE),
        (sm.PROJECT, ProjectStatus.FAILED, ProjectStatus.ACTIVE),
        (sm.RUN, RunStatus.QUEUED, RunStatus.SUCCEEDED),
        (sm.RUN, RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED),
        (sm.RUN, RunStatus.SUCCEEDED, RunStatus.RUNNING),
        (sm.RUN, RunStatus.CANCELLED, RunStatus.RUNNING),
        (sm.NODE_RUN, NodeRunStatus.BLOCKED, NodeRunStatus.RUNNING),
        (sm.NODE_RUN, NodeRunStatus.READY, NodeRunStatus.SUCCEEDED),
        (sm.NODE_RUN, NodeRunStatus.SUCCEEDED, NodeRunStatus.READY),
        (sm.NODE_RUN, NodeRunStatus.CANCELLED, NodeRunStatus.READY),
        (sm.NODE_ATTEMPT, AttemptStatus.QUEUED, AttemptStatus.RUNNING),
        (sm.NODE_ATTEMPT, AttemptStatus.SUCCEEDED, AttemptStatus.RUNNING),
        (sm.NODE_ATTEMPT, AttemptStatus.FAILED, AttemptStatus.CLAIMED),
        (sm.NODE_ATTEMPT, AttemptStatus.CANCELLED, AttemptStatus.QUEUED),
        (sm.APPROVAL, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED),
    ],
)
def test_illegal_transitions_raise(entity: str, frm: object, to: object) -> None:
    assert not sm.can_transition(entity, frm, to)
    with pytest.raises(InvalidTransitionError) as excinfo:
        sm.assert_transition(entity, frm, to)
    assert excinfo.value.details["from"] == str(frm)
    assert excinfo.value.details["to"] == str(to)


def test_self_transition_is_not_legal() -> None:
    """幂等自反不算合法转换（避免掩盖逻辑错误）。"""
    assert not sm.can_transition(sm.RUN, RunStatus.RUNNING, RunStatus.RUNNING)
    with pytest.raises(InvalidTransitionError):
        sm.assert_transition(sm.RUN, RunStatus.RUNNING, RunStatus.RUNNING)


def test_terminal_states_have_no_outgoing() -> None:
    for entity, terminal in (
        (sm.PROJECT, ProjectStatus.COMPLETED),
        (sm.RUN, RunStatus.SUCCEEDED),
        (sm.NODE_ATTEMPT, AttemptStatus.FAILED),
    ):
        assert sm.is_terminal(entity, terminal)
        assert sm.allowed_from(entity, terminal) == frozenset()
