"""WB-01R §二.5：产物版本号并发分配测试（真实 PostgreSQL）。

`revision_no` 由 `projects.lock` 串行化，因此并发的合法登记必须：
- 全部成功（不因 max()+1 竞争而失败）；
- 获得唯一、连续、单调的版本号；
- `supersedes_id` 构成正确的版本链。
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from cybercut.storage import artifacts, node_attempts, node_runs, projects, runs
from cybercut.storage.db import Database

TENANT = "tenant-a"
N = 6


def _seed(db) -> dict:
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="p")
        run = runs.create(
            conn, project_id=project["project_id"], tenant_id=TENANT,
            pipeline_version="v1",
        )
        node_run = node_runs.create(
            conn, run_id=run["run_id"], tenant_id=TENANT, node_key="node_a",
            status="ready",
        )
        attempt = node_attempts.create(
            conn, node_run_id=node_run["node_run_id"], tenant_id=TENANT, attempt_no=1,
        )
    return {"project": project, "run": run, "node_run": node_run, "attempt": attempt}


def _race_register(db, seeded: dict, n: int = N) -> tuple[list[dict], list[BaseException]]:
    barrier = threading.Barrier(n)
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=30)
            database = Database(db.dsn)
            rel = f"artifacts/script-{idx}.json"
            with database.transaction() as conn:
                row = artifacts.register(
                    conn,
                    project_id=seeded["project"]["project_id"],
                    run_id=seeded["run"]["run_id"],
                    node_run_id=seeded["node_run"]["node_run_id"],
                    attempt_id=seeded["attempt"]["attempt_id"],
                    tenant_id=TENANT,
                    kind="script",
                    relative_path=rel,
                    sha256=hashlib.sha256(rel.encode()).hexdigest(),
                    size_bytes=len(rel),
                )
            with lock:
                results.append(row)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(i,), name=f"artifact-{i}")
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_concurrent_revision_allocation_is_unique_and_contiguous(db) -> None:
    seeded = _seed(db)
    results, errors = _race_register(db, seeded)

    assert errors == [], f"合法并发登记不得失败：{errors}"
    assert len(results) == N

    revisions = sorted(r["revision_no"] for r in results)
    assert revisions == list(range(1, N + 1)), f"应唯一且连续：{revisions}"

    by_rev = {r["revision_no"]: r for r in results}
    assert by_rev[1]["supersedes_id"] is None
    for rev in range(2, N + 1):
        assert by_rev[rev]["supersedes_id"] == by_rev[rev - 1]["artifact_revision_id"], (
            f"版本 {rev} 的 supersedes_id 应指向版本 {rev - 1}"
        )


def test_allocated_revisions_match_database_state(db) -> None:
    seeded = _seed(db)
    results, errors = _race_register(db, seeded)
    assert errors == []

    with db.transaction() as conn:
        rows = artifacts.list_for_node_run(
            conn, seeded["node_run"]["node_run_id"], TENANT
        )
    db_revisions = sorted(r["revision_no"] for r in rows)
    assert db_revisions == list(range(1, N + 1))
    assert len({r["artifact_revision_id"] for r in rows}) == N


def test_sequential_registrations_also_chain(db) -> None:
    """对照：串行登记同样产生连续版本链。"""
    seeded = _seed(db)
    previous = None
    for idx in range(1, 4):
        rel = f"artifacts/seq-{idx}.json"
        with db.transaction() as conn:
            if previous is None:
                row = artifacts.register(
                    conn,
                    project_id=seeded["project"]["project_id"],
                    run_id=seeded["run"]["run_id"],
                    node_run_id=seeded["node_run"]["node_run_id"],
                    attempt_id=seeded["attempt"]["attempt_id"],
                    tenant_id=TENANT,
                    kind="script",
                    relative_path=rel,
                    sha256=hashlib.sha256(rel.encode()).hexdigest(),
                    size_bytes=len(rel),
                )
            else:
                row: Any = artifacts.register(
                    conn,
                    project_id=seeded["project"]["project_id"],
                    run_id=seeded["run"]["run_id"],
                    node_run_id=seeded["node_run"]["node_run_id"],
                    attempt_id=seeded["attempt"]["attempt_id"],
                    tenant_id=TENANT,
                    kind="script",
                    relative_path=rel,
                    sha256=hashlib.sha256(rel.encode()).hexdigest(),
                    size_bytes=len(rel),
                )
        assert row["revision_no"] == idx
        if previous is not None:
            assert row["supersedes_id"] == previous
        previous = row["artifact_revision_id"]


def test_different_kinds_have_independent_sequences(db) -> None:
    """不同 kind 的版本序列相互独立。"""
    seeded = _seed(db)
    with db.transaction() as conn:
        for kind in ("script", "audio"):
            for _ in range(2):
                artifacts.register(
                    conn,
                    project_id=seeded["project"]["project_id"],
                    run_id=seeded["run"]["run_id"],
                    node_run_id=seeded["node_run"]["node_run_id"],
                    attempt_id=seeded["attempt"]["attempt_id"],
                    tenant_id=TENANT,
                    kind=kind,
                    relative_path=f"artifacts/{kind}.json",
                    sha256="0" * 64,
                    size_bytes=1,
                )
    with db.transaction() as conn:
        script = artifacts.latest_for_kind(
            conn, seeded["project"]["project_id"], TENANT, "script"
        )
        audio = artifacts.latest_for_kind(
            conn, seeded["project"]["project_id"], TENANT, "audio"
        )
    assert script["revision_no"] == 2
    assert audio["revision_no"] == 2
