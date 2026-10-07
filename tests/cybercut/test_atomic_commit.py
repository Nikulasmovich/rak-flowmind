"""WB-01R §二.2：原子成功提交 / 失败回滚测试（真实 PostgreSQL）。

五个场景：
1. 产物登记后、attempt finish 前发生**取消**；
2. 产物登记过程中**第二个产物失败**；
3. attempt 已被 **WORKER_LOST** 判死；
4. **approval 创建失败**；
5. **成功事件写入失败**。

统一断言：
- attempt 不会被错误覆盖为 `succeeded`；
- 本次事务没有留下 `ArtifactRevision`；
- 没有 `attempt.succeeded` 事件、没有半成品 Approval；
- 未登记文件做最佳努力清理（不视为正式产物）。
"""
from __future__ import annotations

import json

import cybercut.executor.runner as runner_mod
from cybercut.contracts.node_handler import ArtifactSpec, NodeResult
from cybercut.executor.worker import Worker
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.gate import ApprovalGateHandler
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from cybercut.storage import approvals, artifacts, events, node_attempts, node_runs
from cybercut.storage.paths import attempt_dir, resolve_staged_path
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_run,
    official_artifact_dirs,
)

ONE_NODE = PipelineSpec(
    name="one-node", version="one-node-v1", nodes=(NodeSpec("node_a"),)
)
GATE_ONLY = PipelineSpec(
    name="gate-only", version="gate-only-v1",
    nodes=(NodeSpec("approval_gate", approval_required=True),),
)


class _ThenMutate:
    """包装 handler：执行完 inner 后触发外部状态变更（模拟并发取消 / 巡检判死）。"""

    def __init__(self, inner, mutate) -> None:
        self._inner = inner
        self._mutate = mutate

    @property
    def node_key(self) -> str:
        return self._inner.node_key

    def execute(self, context, input_json):
        result = self._inner.execute(context, input_json)
        self._mutate()
        return result


class _MultiArtifactHandler:
    """在 attempt_dir 内产出两个 staged 产物（用于「第二个产物发布失败」场景）。"""

    def __init__(self, node_key: str) -> None:
        self.node_key = node_key

    def execute(self, context, input_json):
        specs = []
        for idx in (1, 2):
            staged_rel = f"{self.node_key}-{idx}.json"
            target = resolve_staged_path(context.attempt_dir, staged_rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"idx": idx}), encoding="utf-8")
            specs.append(
                ArtifactSpec(
                    kind=f"{self.node_key}_out{idx}", staged_relative_path=staged_rel
                )
            )
        return NodeResult(output={"multi": True}, artifacts=specs)


def _setup(db, data_root, registry, pipeline, *, worker_id="w-atomic"):
    orch = make_orchestrator(db, data_root, pipeline=pipeline)
    worker = Worker(
        db, registry, worker_id=worker_id, data_root=data_root,
        lease_seconds=30, orchestrator=orch,
    )
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_key = pipeline.nodes[0].node_key
    node_run = orch.run_state(run["run_id"], TENANT)["nodes"][node_key]
    attempt = orch.attempts_of(node_run["node_run_id"], TENANT)[0]
    return orch, worker, run, node_run, attempt


def _assert_no_partial_success(db, node_run_id: str, run_id: str) -> None:
    """统一断言：无产物、无审批、attempt 非 succeeded、无成功事件。"""
    with db.transaction() as conn:
        assert artifacts.list_for_node_run(conn, node_run_id, TENANT) == [], (
            "回滚后不得留下 ArtifactRevision"
        )
        assert approvals.list_for_node_run(conn, node_run_id, TENANT) == [], (
            "回滚后不得留下 Approval"
        )
        attempts = node_attempts.list_for_node_run(conn, node_run_id, TENANT)
        assert attempts and all(a["status"] != "succeeded" for a in attempts), (
            "attempt 不得被错误覆盖为 succeeded"
        )
        evs = events.list_for_scope(conn, TENANT, run_id=run_id)
    assert "attempt.succeeded" not in [e["event_type"] for e in evs], "不得写入成功事件"


def _project_id(orch, run_id: str) -> str:
    return orch.run_state(run_id, TENANT)["run"]["project_id"]


def test_cancel_between_register_and_finish_rolls_back(db, data_root) -> None:
    holder: dict = {}

    def mutate() -> None:
        with db.transaction() as conn:
            node_attempts.cancel(conn, holder["attempt_id"], TENANT)

    registry = NodeRegistry()
    registry.register(_ThenMutate(EchoHandler("node_a"), mutate))
    orch, worker, run, node_run, attempt = _setup(db, data_root, registry, ONE_NODE)
    holder["attempt_id"] = attempt["attempt_id"]

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    _assert_no_partial_success(db, node_run["node_run_id"], run["run_id"])
    with db.transaction() as conn:
        row = node_attempts.get(conn, attempt["attempt_id"], TENANT)
    assert row["status"] == "cancelled", "被取消的 attempt 必须保持 cancelled"

    project_root = data_root / "projects" / _project_id(orch, run["run_id"])
    assert not attempt_dir(project_root, attempt["attempt_id"]).exists(), (
        "attempt 暂存目录应被清理"
    )
    assert official_artifact_dirs(data_root, _project_id(orch, run["run_id"])) == [], (
        "失败路径不得在正式区留下任何 artifact 目录"
    )


def test_second_artifact_failure_rolls_back(db, data_root, monkeypatch) -> None:
    registry = NodeRegistry()
    registry.register(_MultiArtifactHandler("node_a"))
    orch, worker, run, node_run, attempt = _setup(db, data_root, registry, ONE_NODE)

    calls = {"n": 0}
    real_register = runner_mod.artifacts_repo.register

    def flaky(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟第二个产物登记失败")
        return real_register(conn, **kwargs)

    monkeypatch.setattr(runner_mod.artifacts_repo, "register", flaky)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "COMMIT_FAILED"
    assert calls["n"] == 2, "应尝试登记两个产物"
    _assert_no_partial_success(db, node_run["node_run_id"], run["run_id"])

    with db.transaction() as conn:
        attempt_now = node_attempts.list_for_node_run(
            conn, node_run["node_run_id"], TENANT
        )[0]
    assert attempt_now["status"] == "failed"

    for idx in (1, 2):
        staged = resolve_staged_path(
            attempt_dir(
                data_root / "projects" / _project_id(orch, run["run_id"]),
                attempt["attempt_id"],
            ),
            f"node_a-{idx}.json",
        )
        assert not staged.exists(), "暂存目录应被整体清理"
    assert official_artifact_dirs(data_root, _project_id(orch, run["run_id"])) == [], (
        "发布失败必须清理本次新分配的正式目录"
    )


def test_attempt_killed_by_worker_lost_rolls_back(db, data_root) -> None:
    holder: dict = {}

    def mutate() -> None:
        with db.transaction() as conn:
            node_attempts.finish(
                conn, holder["attempt_id"], TENANT, to_status="failed",
                error_code="WORKER_LOST", error_message="模拟巡检判死",
            )

    registry = NodeRegistry()
    registry.register(_ThenMutate(EchoHandler("node_a"), mutate))
    _orch, worker, run, node_run, attempt = _setup(db, data_root, registry, ONE_NODE)
    holder["attempt_id"] = attempt["attempt_id"]

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    _assert_no_partial_success(db, node_run["node_run_id"], run["run_id"])
    with db.transaction() as conn:
        row = node_attempts.get(conn, attempt["attempt_id"], TENANT)
    assert row["status"] == "failed"
    assert row["error_code"] == "WORKER_LOST", "已有错误码不得被覆盖"


def test_approval_creation_failure_rolls_back(db, data_root, monkeypatch) -> None:
    registry = NodeRegistry()
    registry.register(ApprovalGateHandler("approval_gate"))
    _orch, worker, run, node_run, _attempt = _setup(db, data_root, registry, GATE_ONLY)

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟 approval 创建失败")

    monkeypatch.setattr(runner_mod.approvals_repo, "create_pending", boom)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    _assert_no_partial_success(db, node_run["node_run_id"], run["run_id"])
    with db.transaction() as conn:
        node_row = node_runs.get(conn, node_run["node_run_id"], TENANT)
    assert node_row["status"] != "waiting_approval", "不得留下半成品 waiting_approval"


def test_success_event_failure_rolls_back(db, data_root, monkeypatch) -> None:
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    _orch, worker, run, node_run, _attempt = _setup(db, data_root, registry, ONE_NODE)

    real_append = runner_mod.events_repo.append

    def flaky_append(conn, **kwargs):
        if kwargs.get("event_type") == "attempt.succeeded":
            raise RuntimeError("模拟成功事件写入失败")
        return real_append(conn, **kwargs)

    monkeypatch.setattr(runner_mod.events_repo, "append", flaky_append)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    _assert_no_partial_success(db, node_run["node_run_id"], run["run_id"])


def test_happy_path_is_atomic_and_succeeds(db, data_root) -> None:
    """对照：正常路径必须在同一事务内完成全部登记并成功。"""
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a"))
    _orch, worker, run, node_run, _attempt = _setup(db, data_root, registry, ONE_NODE)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "succeeded"
    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_run["node_run_id"], TENANT)
        evs = events.list_for_scope(conn, TENANT, run_id=run["run_id"])
    assert len(rows) == 1
    assert rows[0]["sha256"] and rows[0]["size_bytes"] > 0
    assert "attempt.succeeded" in [e["event_type"] for e in evs]
