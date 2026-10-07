"""WB-01R3 §一：暂存区所有权与「执行器分配正式路径」边界（真实 PostgreSQL）。

覆盖：
1. handler 只能把待发布产物写入 `context.attempt_dir`；
2. `ArtifactSpec.staged_relative_path` 越界（`..` / 绝对路径）-> `ARTIFACT_NOT_OWNED`；
3. handler 无法把文件“注入”正式区：正式路径完全由执行器决定；
4. 成功路径：正式路径 = `artifacts/{artifact_revision_id}/{safe_filename}`，且 staged 文件为**移动**；
5. attempt 暂存目录在结束时被清理。
"""
from __future__ import annotations

import json

from cybercut.contracts.node_handler import ArtifactSpec, NodeResult
from cybercut.executor.worker import Worker
from cybercut.nodes.echo import EchoHandler
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from cybercut.storage import artifacts
from cybercut.storage.paths import (
    attempt_dir,
    resolve_artifact_path,
    resolve_staged_path,
    sanitize_filename,
)
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


class _DeclareStaged:
    """在 attempt_dir 内（或试图越界）写文件并声明 staged 路径。"""

    def __init__(self, node_key: str, staged_rel: str, *, write: bool = True,
                 kind: str = "declared") -> None:
        self.node_key = node_key
        self.staged_rel = staged_rel
        self.write = write
        self.kind = kind

    def execute(self, context, input_json):
        if self.write:
            try:
                target = resolve_staged_path(context.attempt_dir, self.staged_rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"declared": self.staged_rel}),
                                  encoding="utf-8")
            except Exception:  # noqa: BLE001 - 越界路径故意让写入失败，声明仍然返回
                pass
        return NodeResult(
            output={"declared": self.staged_rel},
            artifacts=[ArtifactSpec(kind=self.kind,
                                    staged_relative_path=self.staged_rel)],
        )


def _run_one(db, data_root, registry, *, worker_id="w-stage"):
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker = Worker(db, registry, worker_id=worker_id, data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]
    return orch, worker, project, run, node_a, attempt


# ── 1/2) staged 路径必须位于当前 attempt 独占目录内 ──


def test_staged_path_escape_is_rejected(db, data_root) -> None:
    registry = NodeRegistry()
    registry.register(_DeclareStaged("node_a", "../escape.json"))
    _orch, worker, project, _run, node_a, attempt = _run_one(db, data_root, registry)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "ARTIFACT_NOT_OWNED"
    with db.transaction() as conn:
        assert artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT) == []
    assert not (data_root / "projects" / project["project_id"] / "escape.json").exists()
    assert official_artifact_dirs(data_root, project["project_id"]) == []
    assert not attempt_dir(
        data_root / "projects" / project["project_id"], attempt["attempt_id"]
    ).exists()


def test_absolute_staged_path_is_rejected(db, data_root) -> None:
    registry = NodeRegistry()
    registry.register(_DeclareStaged("node_a", "C:/Windows/system32/evil.json"))
    _orch, worker, _project, _run, _node_a, _attempt = _run_one(
        db, data_root, registry
    )
    outcome = worker.step()
    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "ARTIFACT_NOT_OWNED"


def test_staged_path_nested_subdir_is_allowed(db, data_root) -> None:
    """暂存目录内的子目录是允许的（正式路径仍由执行器重排）。"""
    registry = NodeRegistry()
    registry.register(_DeclareStaged("node_a", "sub/dir/out.json"))
    _orch, worker, project, _run, node_a, _attempt = _run_one(db, data_root, registry)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "succeeded"
    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
    assert len(rows) == 1
    project_root = data_root / "projects" / project["project_id"]
    assert (project_root / rows[0]["relative_path"]).is_file()
    assert rows[0]["relative_path"].endswith("/out.json")


# ── 3) handler 无法注入正式区 ──


def test_handler_cannot_inject_into_official_zone(db, data_root) -> None:
    """handler 直接写正式区文件不会被登记，也不会被删除（不按 handler 路径清理）。"""

    class _Injector:
        node_key = "node_a"

        def execute(self, context, input_json):
            injected = resolve_artifact_path(
                context.project_root, "artifacts/injected.json"
            )
            injected.parent.mkdir(parents=True, exist_ok=True)
            injected.write_text('{"injected": true}', encoding="utf-8")
            # 声明一个 attempt_dir 内**并不存在**的 staged 文件
            return NodeResult(
                output={"injected": True},
                artifacts=[ArtifactSpec(kind="k", staged_relative_path="injected.json")],
            )

    registry = NodeRegistry()
    registry.register(_Injector())
    _orch, worker, project, _run, node_a, _attempt = _run_one(db, data_root, registry)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "ARTIFACT_MISSING"
    injected = (
        data_root / "projects" / project["project_id"] / "artifacts" / "injected.json"
    )
    assert injected.is_file(), "不得根据 handler 提供的正式路径删除文件"
    with db.transaction() as conn:
        assert artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT) == []
    assert official_artifact_dirs(data_root, project["project_id"]) == []


# ── 4/5) 成功路径：执行器分配正式路径 + 暂存文件被移动 ──


def test_success_publishes_to_executor_assigned_path(db, data_root) -> None:
    registry = NodeRegistry()
    registry.register(EchoHandler("node_a", staged_filename="same.json"))
    _orch, worker, project, _run, node_a, attempt = _run_one(db, data_root, registry)
    project_root = data_root / "projects" / project["project_id"]

    outcome = worker.step()

    assert outcome is not None and outcome.status == "succeeded"
    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
    assert len(rows) == 1
    row = rows[0]
    assert row["relative_path"] == (
        f"artifacts/{row['artifact_revision_id']}/{sanitize_filename('same.json')}"
    ), "正式 relative_path 必须由执行器分配"
    published = project_root / row["relative_path"]
    assert published.is_file()
    assert row["sha256"] and row["size_bytes"] > 0

    # staged 文件是**移动**而不是复制：暂存目录已不存在
    staged = resolve_staged_path(
        attempt_dir(project_root, attempt["attempt_id"]), "same.json"
    )
    assert not staged.exists(), "staged 文件应被移动到正式路径"
    assert not attempt_dir(project_root, attempt["attempt_id"]).exists()


def test_safe_filename_sanitizes_and_bounds() -> None:
    assert sanitize_filename("a/b/c.json") == "c.json"
    assert sanitize_filename("..\\..\\evil.json") == "evil.json"
    assert sanitize_filename("weird name!.json") == "weird_name_.json"
    assert sanitize_filename("") == "artifact.bin"
    assert sanitize_filename("..") == "artifact.bin"
    assert len(sanitize_filename("x" * 400 + ".json")) <= 128
    assert sanitize_filename("中文名.json") == "中文名.json"
