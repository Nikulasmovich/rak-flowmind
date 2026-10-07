"""claim 协议测试：并发领取不重复、单条领取、available_at 生效（真实 PostgreSQL）。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from cybercut.executor.claim import claim_next_attempt
from cybercut.storage import node_attempts, node_runs, projects, runs
from cybercut.storage.db import Database

TENANT = "tenant-a"


def _seed(db, count: int = 1, *, tenant: str = TENANT) -> list[str]:
    """建 project/run/node_run 并挂 count 条 queued attempt。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=tenant, name="p")
        run = runs.create(
            conn, project_id=project["project_id"], tenant_id=tenant,
            pipeline_version="v1",
        )
        node_run = node_runs.create(
            conn, run_id=run["run_id"], tenant_id=tenant, node_key="node_a",
            status="ready",
        )
        ids = []
        for i in range(count):
            attempt = node_attempts.create(
                conn, node_run_id=node_run["node_run_id"], tenant_id=tenant,
                attempt_no=i + 1,
            )
            ids.append(attempt["attempt_id"])
    return ids


def _status(db, attempt_id: str, tenant: str = TENANT) -> dict:
    with db.transaction() as conn:
        return node_attempts.get(conn, attempt_id, tenant)


def test_claim_marks_single_attempt(db) -> None:
    ids = _seed(db, 1)
    claimed = claim_next_attempt(Database(db.dsn), "w1")
    assert claimed is not None
    assert claimed["attempt_id"] == ids[0]
    assert claimed["status"] == "claimed"
    row = _status(db, ids[0])
    assert row["status"] == "claimed"
    assert row["claimed_by"] == "w1"
    assert row["claimed_at"] is not None
    assert row["heartbeat_at"] is not None


def test_claim_takes_only_one_at_a_time(db) -> None:
    """单次 claim 只取一条，禁止一次锁住批量任务。"""
    ids = _seed(db, 3)
    database = Database(db.dsn)
    first = claim_next_attempt(database, "w1")
    assert first is not None
    remaining = [i for i in ids if i != first["attempt_id"]]
    assert all(_status(db, i)["status"] == "queued" for i in remaining)


def test_concurrent_claim_never_duplicates(db) -> None:
    """两个 worker 并发领取同一 attempt -> 恰好一个成功，另一个拿空。"""
    ids = _seed(db, 1)
    results: list[dict | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _worker(worker_id: str, database: Database) -> None:
        barrier.wait()
        row = claim_next_attempt(database, worker_id)
        with lock:
            results.append(row)

    threads = [
        threading.Thread(target=_worker, args=(f"w{i}", Database(db.dsn)))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, f"应恰好 1 个 worker 领到，实际 {len(winners)}"
    assert len(losers) == 1
    assert _status(db, ids[0])["status"] == "claimed"


def test_concurrent_claim_distributes_across_attempts(db) -> None:
    """3 条 attempt、3 个 worker 并发 -> 各领各的，无重复。"""
    ids = _seed(db, 3)
    results: list[dict | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def _worker(worker_id: str) -> None:
        database = Database(db.dsn)
        barrier.wait()
        row = claim_next_attempt(database, worker_id)
        with lock:
            results.append(row)

    threads = [
        threading.Thread(target=_worker, args=(f"w{i}",)) for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    claimed_ids = [r["attempt_id"] for r in results if r is not None]
    assert sorted(claimed_ids) == sorted(ids)
    assert len(set(claimed_ids)) == len(claimed_ids)


def test_claim_respects_available_at(db) -> None:
    """未到 available_at 的 attempt 不可被领取。"""
    ids = _seed(db, 1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.node_attempts SET available_at = %s WHERE attempt_id = %s",
                (future, ids[0]),
            )
    assert claim_next_attempt(Database(db.dsn), "w1") is None

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.node_attempts SET available_at = %s WHERE attempt_id = %s",
                (past, ids[0]),
            )
    assert claim_next_attempt(Database(db.dsn), "w1") is not None


def test_claim_returns_none_when_empty(db) -> None:
    assert claim_next_attempt(Database(db.dsn), "w1") is None
