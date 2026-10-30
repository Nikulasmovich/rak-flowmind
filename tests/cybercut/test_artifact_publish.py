"""WB-01R3 §四：并发验收测试 —— 暂存同名、并发发布、失败回滚、无孤儿。

覆盖：
1. 两个 attempt 同时产生**相同 staged 文件名**但内容不同；
2. 使用 Barrier 保证二者同时结束 handler；
3. 两者发布后的正式路径必须不同；
4. 每条 ArtifactRevision 的 SHA-256 必须与其正式文件一致；
5. 一个提交成功、另一个提交失败时，成功产物不得被覆盖或删除；
6. handler 写出 staged 文件后抛异常：attempt_dir 被清理、正式区无孤儿；
7. 多产物发布到一半失败：DB 无记录、本次正式目录全部清理；
8. 发布绝不覆盖既有文件/目录。
"""
from __future__ import annotations

import hashlib
import json
import threading

import cybercut.executor.runner as runner_mod
from cybercut.contracts.node_handler import ArtifactSpec, NodeResult
from cybercut.executor.worker import Worker
from cybercut.nodes.registry import NodeRegistry
from cybercut.orchestrator.pipeline import NodeSpec, PipelineSpec
from cybercut.storage import artifacts, node_attempts, node_runs
from cybercut.storage.paths import attempt_dir, resolve_staged_path
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_run,
    official_artifact_dirs,
)

TWO_ROOTS = PipelineSpec(
    name="two-roots", version="two-roots-v1",
    nodes=(NodeSpec("node_a"), NodeSpec("node_b")),
)
ONE_NODE = PipelineSpec(
    name="one-node", version="one-node-v1", nodes=(NodeSpec("node_a"),)
)


class _Staging:
    """写入 attempt_dir 内 staged 文件；可选 Barrier 同步结束 handler。"""

    def __init__(self, node_key: str, *, payload: dict, staged_name: str = "same.json",
                 barrier: threading.Barrier | None = None,
                 metadata: dict | None = None,
                 raise_after: bool = False,
                 extra_files: tuple[str, ...] = ()) -> None:
        self.node_key = node_key
        self.payload = payload
        self.staged_name = staged_name
        self.barrier = barrier
        self.metadata = dict(metadata or {})
        self.raise_after = raise_after
        self.extra_files = extra_files

    def _stage(self, context, name: str, payload: dict) -> None:
        target = resolve_staged_path(context.attempt_dir, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def execute(self, context, input_json):
        self._stage(context, self.staged_name, self.payload)
        specs = [
            ArtifactSpec(kind=f"{self.node_key}_out", staged_relative_path=self.staged_name,
                         metadata=self.metadata)
        ]
        for name in self.extra_files:
            self._stage(context, name, {"extra": name})
            specs.append(
                ArtifactSpec(kind=f"{self.node_key}_{name}", staged_relative_path=name)
            )
        if self.barrier is not None:
            self.barrier.wait(timeout=30)
        if self.raise_after:
            raise RuntimeError("handler 在写完 staged 文件后抛异常")
        return NodeResult(output=self.payload, artifacts=specs)


def _run_two(db, data_root, registry, *, workers: int = 2):
    orch = make_orchestrator(db, data_root, pipeline=TWO_ROOTS)
    project = make_project(orch)
    run = make_run(orch, project=project)
    outcomes: list = [None] * workers
    errors: list[BaseException] = []

    def _work(idx: int) -> None:
        worker = Worker(db, registry, worker_id=f"w{idx}", data_root=data_root,
                        lease_seconds=30, orchestrator=orch)
        try:
            outcomes[idx] = worker.step()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_work, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return orch, project, run, outcomes, errors


def _rows(db, project_id: str) -> list[dict]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT artifact_revision_id, kind, relative_path, sha256, size_bytes"
                "  FROM cybercut.artifact_revisions WHERE project_id = %s"
                " ORDER BY revision_no",
                (project_id,),
            )
            return [dict(r) for r in cur.fetchall()]


# ── 1/2/3/4) 并发：同名 staged、正式路径不同、摘要一致 ──


def test_concurrent_same_staged_name_publishes_distinct_paths(db, data_root) -> None:
    barrier = threading.Barrier(2)
    registry = NodeRegistry()
    registry.register(_Staging("node_a", payload={"who": "a"}, barrier=barrier))
    registry.register(_Staging("node_b", payload={"who": "b"}, barrier=barrier))

    _orch, project, _run, outcomes, errors = _run_two(db, data_root, registry)

    assert errors == []
    assert all(o is not None and o.status == "succeeded" for o in outcomes)

    rows = _rows(db, project["project_id"])
    assert len(rows) == 2
    paths = {r["relative_path"] for r in rows}
    assert len(paths) == 2, "同名 staged 必须发布到不同正式路径"

    project_root = data_root / "projects" / project["project_id"]
    payloads = set()
    for row in rows:
        published = project_root / row["relative_path"]
        assert published.is_file()
        raw = published.read_bytes()
        assert row["sha256"] == hashlib.sha256(raw).hexdigest(), "摘要必须与正式文件一致"
        assert row["size_bytes"] == len(raw)
        payloads.add(raw)
    assert len(payloads) == 2, "两个 attempt 的内容必须各自完整保留"


def test_published_digest_matches_official_file(db, data_root) -> None:
    registry = NodeRegistry()
    registry.register(_Staging("node_a", payload={"k": "v"}))
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker = Worker(db, registry, worker_id="w-digest", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]

    assert worker.step() is not None

    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT)
    raw = (data_root / "projects" / project["project_id"] / rows[0]["relative_path"]).read_bytes()
    assert rows[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert rows[0]["size_bytes"] == len(raw)


# ── 5) 一个提交成功、另一个失败：成功产物不受影响 ──


def test_one_commit_fails_other_success_artifact_intact(db, data_root, monkeypatch) -> None:
    barrier = threading.Barrier(2)
    registry = NodeRegistry()
    registry.register(
        _Staging("node_a", payload={"who": "a"}, barrier=barrier,
                 metadata={"fail_publish": False})
    )
    registry.register(
        _Staging("node_b", payload={"who": "b"}, barrier=barrier,
                 metadata={"fail_publish": True})
    )

    real_register = runner_mod.artifacts_repo.register

    def flaky_register(conn, **kwargs):
        if (kwargs.get("metadata") or {}).get("fail_publish"):
            raise RuntimeError("模拟第二个 attempt 的登记失败")
        return real_register(conn, **kwargs)

    monkeypatch.setattr(runner_mod.artifacts_repo, "register", flaky_register)

    _orch, project, _run, outcomes, errors = _run_two(db, data_root, registry)

    assert errors == []
    statuses = {o.status for o in outcomes}
    assert statuses == {"succeeded", "failed"}, f"应一成一败，实际 {statuses}"

    rows = _rows(db, project["project_id"])
    assert len(rows) == 1, "失败方不得留下 ArtifactRevision"
    ok_row = rows[0]
    published = data_root / "projects" / project["project_id"] / ok_row["relative_path"]
    assert published.is_file(), "成功产物不得被删除"
    raw = published.read_bytes()
    assert ok_row["sha256"] == hashlib.sha256(raw).hexdigest(), "成功产物内容不得被覆盖"
    assert len(official_artifact_dirs(data_root, project["project_id"])) == 1


# ── 6) handler 抛异常：暂存目录清理、正式区无孤儿 ──


def test_handler_exception_after_staging_leaves_no_orphan(db, data_root) -> None:
    registry = NodeRegistry()
    registry.register(
        _Staging("node_a", payload={"x": 1}, raise_after=True, extra_files=("b.json",))
    )
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker = Worker(db, registry, worker_id="w-raise", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    assert outcome.error_code == "HANDLER_EXCEPTION"
    project_root = data_root / "projects" / project["project_id"]
    assert not attempt_dir(project_root, attempt["attempt_id"]).exists(), (
        "attempt_dir 必须被清理"
    )
    assert official_artifact_dirs(data_root, project["project_id"]) == [], (
        "正式区不得有孤儿"
    )
    with db.transaction() as conn:
        assert artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT) == []


# ── 7) 多产物发布到一半失败：DB 无记录、正式目录全部清理 ──


def test_partial_publish_failure_rolls_back_and_cleans(db, data_root, monkeypatch) -> None:
    registry = NodeRegistry()
    registry.register(
        _Staging("node_a", payload={"x": 1}, extra_files=("b.json", "c.json"))
    )
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker = Worker(db, registry, worker_id="w-partial", data_root=data_root,
                    lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]

    calls = {"n": 0}
    real_replace = runner_mod.os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("模拟第二个产物发布失败")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(runner_mod.os, "replace", flaky_replace)

    outcome = worker.step()

    assert outcome is not None and outcome.status == "failed"
    assert calls["n"] == 2
    with db.transaction() as conn:
        assert artifacts.list_for_node_run(conn, node_a["node_run_id"], TENANT) == []
    assert official_artifact_dirs(data_root, project["project_id"]) == [], (
        "本次已创建的全部正式目录必须被清理"
    )
    attempt_id = orch.attempts_of(node_a["node_run_id"], TENANT)[0]["attempt_id"]
    assert not attempt_dir(
        data_root / "projects" / project["project_id"], attempt_id
    ).exists()


# ── 8) 绝不覆盖既有正式文件/目录 ──


def test_publish_refuses_to_overwrite_existing_target(db, data_root, monkeypatch) -> None:
    fixed_id = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(runner_mod, "new_id", lambda: fixed_id)

    registry = NodeRegistry()
    registry.register(_Staging("node_a", payload={"who": "first"}))
    orch = make_orchestrator(db, data_root, pipeline=ONE_NODE)
    worker1 = Worker(db, registry, worker_id="w-first", data_root=data_root,
                     lease_seconds=30, orchestrator=orch)
    project = make_project(orch)
    run1 = make_run(orch, project=project)
    out1 = worker1.step()
    assert out1 is not None and out1.status == "succeeded"

    first_rows = _rows(db, project["project_id"])
    assert len(first_rows) == 1
    project_root = data_root / "projects" / project["project_id"]
    first_path = project_root / first_rows[0]["relative_path"]
    first_bytes = first_path.read_bytes()
    assert first_rows[0]["artifact_revision_id"] == fixed_id

    # 第二次 attempt 会分配到同一个 revision id 目录 -> 必须拒绝覆盖。
    # 沿用同一 node_run 追加 attempt（首个 Run 已完成，项目进入终态，不能再起新 Run）。
    node_a = orch.run_state(run1["run_id"], TENANT)["nodes"]["node_a"]
    with db.transaction() as conn:
        attempt_no = node_runs.bump_attempt_no(conn, node_a["node_run_id"], TENANT)
        node_attempts.create(
            conn, node_run_id=node_a["node_run_id"], tenant_id=TENANT,
            attempt_no=attempt_no,
        )

    registry2 = NodeRegistry()
    registry2.register(_Staging("node_a", payload={"who": "second"}))
    worker2 = Worker(db, registry2, worker_id="w-second", data_root=data_root,
                     lease_seconds=30, orchestrator=orch)
    out2 = worker2.step()

    assert out2 is not None and out2.status == "failed"
    assert out2.error_code == "ARTIFACT_PUBLISH_CONFLICT"
    assert first_path.read_bytes() == first_bytes, "既有正式产物不得被覆盖"
    assert len(_rows(db, project["project_id"])) == 1, "冲突 attempt 不得新增产物记录"
    # 既有 revision 目录不被复用、也不被删除
    assert len(official_artifact_dirs(data_root, project["project_id"])) == 1
