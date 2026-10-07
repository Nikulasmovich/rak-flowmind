"""集成测试辅助：快速搭建 project / run / worker。"""
from __future__ import annotations

from pathlib import Path

from cybercut.executor.worker import Worker
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.engine import Orchestrator
from cybercut.orchestrator.pipeline import TEST_PIPELINE, NodeSpec, PipelineSpec

TENANT = "tenant-a"


def make_orchestrator(
    db,
    data_root: Path | str | None = None,
    *,
    pipeline: PipelineSpec | None = None,
    auto_retry_worker_lost: bool = True,
    max_auto_retries: int = 1,
) -> Orchestrator:
    return Orchestrator(
        db,
        pipeline=pipeline or TEST_PIPELINE,
        data_root=data_root,
        auto_retry_worker_lost=auto_retry_worker_lost,
        max_auto_retries=max_auto_retries,
    )


def make_project(orch: Orchestrator, tenant: str = TENANT, name: str = "proj") -> dict:
    project, _ = orch.create_project(tenant_id=tenant, name=name)
    return project


def make_run(orch: Orchestrator, tenant: str = TENANT, project: dict | None = None) -> dict:
    project = project or make_project(orch, tenant)
    return orch.start_run(project_id=project["project_id"], tenant_id=tenant)


def make_registry() -> NodeRegistry:
    """标准测试注册表（与 TEST_PIPELINE 的四个节点对齐）。"""
    from cybercut.nodes.echo import EchoHandler
    from cybercut.nodes.gate import ApprovalGateHandler

    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    registry.register(EchoHandler("node_b"))
    registry.register(ApprovalGateHandler("approval_gate"))
    registry.register(EchoHandler("node_c"))
    return registry


def make_failing_registry(
    node_key: str, *, error_code: str = "CONTROLLED_FAILURE",
    fail_times: int | None = None,
) -> tuple[NodeRegistry, "object"]:
    """把 `node_key` 换成可控失败 handler，返回 (registry, handler)。"""
    from cybercut.nodes.echo import EchoHandler
    from cybercut.nodes.fail import FailingHandler
    from cybercut.nodes.gate import ApprovalGateHandler

    handler = FailingHandler(node_key, error_code=error_code, fail_times=fail_times)
    registry = NodeRegistry()
    for key in ("node_a", "node_b", "node_c"):
        if key == node_key:
            registry.register(handler)
        else:
            registry.register(EchoHandler(key))
    registry.register(ApprovalGateHandler("approval_gate"))
    return registry, handler


def make_worker(
    db,
    orch: Orchestrator,
    registry: NodeRegistry,
    data_root: Path | str,
    *,
    worker_id: str = "worker-1",
    lease_seconds: int = 60,
) -> Worker:
    return Worker(
        db, registry, worker_id=worker_id, data_root=data_root,
        lease_seconds=lease_seconds, orchestrator=orch,
    )


def make_registry_for(
    node_keys: list[str], *, gate_key: str | None = None
) -> NodeRegistry:
    """按给定节点键构造注册表（gate_key 用审批闸门，其余用 echo）。

    `gate_key` 会**自动补齐**（即使未出现在 `node_keys` 中）。
    """
    from cybercut.nodes.echo import EchoHandler
    from cybercut.nodes.gate import ApprovalGateHandler

    keys = list(dict.fromkeys(list(node_keys) + ([gate_key] if gate_key else [])))
    registry = NodeRegistry()
    for key in keys:
        if gate_key is not None and key == gate_key:
            registry.register(ApprovalGateHandler(key))
        else:
            registry.register(EchoHandler(key))
    return registry


def pending_approval(db, node_run_id: str, tenant: str = TENANT) -> dict | None:
    from cybercut.storage import approvals

    with db.transaction() as conn:
        return approvals.find_pending(conn, node_run_id, tenant)


def statuses(orch: Orchestrator, run_id: str, tenant: str = TENANT) -> dict[str, str]:
    return orch.run_state(run_id, tenant)["statuses"]


def node_run(orch: Orchestrator, run_id: str, node_key: str, tenant: str = TENANT) -> dict:
    return orch.run_state(run_id, tenant)["nodes"][node_key]


def attempts(orch: Orchestrator, node_run_id: str, tenant: str = TENANT) -> list[dict]:
    return orch.attempts_of(node_run_id, tenant)


DRAIN_PIPELINE = PipelineSpec(
    name="drain",
    version="drain-v1",
    nodes=(NodeSpec("node_a"),),
)


def official_artifact_dirs(data_root, project_id: str) -> list[Path]:
    """正式产物区下的 artifact_revision 目录列表（`artifacts/{revision_id}/`）。"""
    root = Path(data_root) / "projects" / project_id / "artifacts"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def staged_files(attempt_dir: Path) -> list[Path]:
    """attempt 暂存目录内的全部文件（含子目录）。"""
    if not Path(attempt_dir).is_dir():
        return []
    return sorted(p for p in Path(attempt_dir).rglob("*") if p.is_file())
