"""幂等键测试：同请求重放返回同资源；同键不同指纹冲突 —— 真实 PostgreSQL。"""
from __future__ import annotations

import pytest

from cybercut.contracts.errors import IdempotencyConflictError
from cybercut.storage import projects
from tests.cybercut.helpers import TENANT, make_orchestrator


def test_same_key_same_request_returns_same_project(db) -> None:
    orch = make_orchestrator(db)
    first, replayed_first = orch.create_project(
        tenant_id=TENANT, name="同一请求", goal={"k": 1}, idempotency_key="key-1"
    )
    second, replayed_second = orch.create_project(
        tenant_id=TENANT, name="同一请求", goal={"k": 1}, idempotency_key="key-1"
    )

    assert replayed_first is False
    assert replayed_second is True
    assert first["project_id"] == second["project_id"]

    with db.transaction() as conn:
        rows = projects.list_for_tenant(conn, TENANT)
    assert len(rows) == 1, "幂等重放不得创建第二个项目"


def test_same_key_different_request_conflicts(db) -> None:
    orch = make_orchestrator(db)
    orch.create_project(tenant_id=TENANT, name="原始", idempotency_key="key-2")

    with pytest.raises(IdempotencyConflictError) as excinfo:
        orch.create_project(tenant_id=TENANT, name="不同名", idempotency_key="key-2")
    assert excinfo.value.details["operation"] == "create_project"
    assert excinfo.value.details["existing_request_hash"] != (
        excinfo.value.details["incoming_request_hash"]
    )

    with db.transaction() as conn:
        rows = projects.list_for_tenant(conn, TENANT)
    assert len(rows) == 1, "冲突请求不得落库"


def test_different_tenant_may_reuse_same_key(db) -> None:
    orch = make_orchestrator(db)
    a, _ = orch.create_project(tenant_id="tenant-a", name="x", idempotency_key="shared")
    b, replayed = orch.create_project(tenant_id="tenant-b", name="x",
                                      idempotency_key="shared")
    assert a["project_id"] != b["project_id"]
    assert replayed is False


def test_without_key_always_creates_new_project(db) -> None:
    orch = make_orchestrator(db)
    a, _ = orch.create_project(tenant_id=TENANT, name="n")
    b, _ = orch.create_project(tenant_id=TENANT, name="n")
    assert a["project_id"] != b["project_id"]
