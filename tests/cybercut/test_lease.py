"""租约测试：claimed 超时恢复、running 心跳超时判死与新 attempt（真实 PostgreSQL）。"""
from __future__ import annotations

from cybercut.executor.claim import claim_next_attempt
from cybercut.executor.lease import (
    fail_lost_running_attempts,
    recover_expired_claims,
)
from cybercut.storage import node_attempts, node_runs, projects, runs
from cybercut.storage.db import Database
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_run,
)

STALE = "UPDATE cybercut.node_attempts SET heartbeat_at = now() - interval '10 minutes' WHERE attempt_id = %s"


def _seed_running(db, *, tenant: str = TENANT) -> tuple[dict, dict, dict]:
    """建 project/run/node_run，并把 node_a 的 attempt 推进到 claimed+running。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=tenant, name="p")
        run = runs.create(
            conn, project_id=project["project_id"], tenant_id=tenant,
            pipeline_version="v1", status="running",
        )
        node_run = node_runs.create(
            conn, run_id=run["run_id"], tenant_id=tenant, node_key="node_a",
            status="running",
        )
        node_attempts.create(
            conn, node_run_id=node_run["node_run_id"], tenant_id=tenant, attempt_no=1,
        )
    database = Database(db.dsn)
    claimed = claim_next_attempt(database, "w1")
    with database.transaction() as conn:
        node_attempts.mark_running(
            conn, claimed["attempt_id"], tenant, "w1"
        )
    return project, run, node_run


def _attempt_status(db, attempt_id: str, tenant: str = TENANT) -> dict:
    with db.transaction() as conn:
        return node_attempts.get(conn, attempt_id, tenant)


def test_expired_claim_is_requeued(db) -> None:
    """claimed 租约超时且无心跳 -> 恢复 queued（可被再次领取）。"""
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
    claimed = claim_next_attempt(Database(db.dsn), "w1")
    assert claimed["status"] == "claimed"

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(STALE, (attempt["attempt_id"],))

    assert recover_expired_claims(db, lease_seconds=60) == [attempt["attempt_id"]]
    row = _attempt_status(db, attempt["attempt_id"])
    assert row["status"] == "queued"
    assert row["claimed_by"] is None
    assert row["heartbeat_at"] is None

    # 恢复后可被重新领取
    again = claim_next_attempt(Database(db.dsn), "w2")
    assert again is not None and again["claimed_by"] == "w2"


def test_fresh_claim_is_not_requeued(db) -> None:
    _seed_running(db)
    assert recover_expired_claims(db, lease_seconds=60) == []


def test_worker_lost_fails_attempt_and_node(db) -> None:
    """running 心跳超时 -> attempt=failed/WORKER_LOST，node_run=failed。"""
    _, run, node_run = _seed_running(db)
    attempt_id = _attempt_status_of_node(db, node_run["node_run_id"])

    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(STALE, (attempt_id,))

    lost = fail_lost_running_attempts(db, lease_seconds=60)
    assert [item["attempt_id"] for item in lost] == [attempt_id]
    row = _attempt_status(db, attempt_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "WORKER_LOST"

    with db.transaction() as conn:
        nr = node_runs.get(conn, node_run["node_run_id"], TENANT)
    assert nr["status"] == "failed"


def _attempt_status_of_node(db, node_run_id: str) -> str:
    with db.transaction() as conn:
        rows = node_attempts.list_for_node_run(conn, node_run_id, TENANT)
    return rows[0]["attempt_id"]


def test_worker_lost_creates_new_attempt_via_retry_policy(db, data_root) -> None:
    """判死后由重试策略新建下一个 NodeAttempt（不重跑被判定丢失的那条）。"""
    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)

    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]
    first_attempt = orch.attempts_of(node_a["node_run_id"], TENANT)[0]

    database = Database(db.dsn)
    claimed = claim_next_attempt(database, "w1")
    assert claimed["attempt_id"] == first_attempt["attempt_id"]
    with database.transaction() as conn:
        node_attempts.mark_running(
            conn, claimed["attempt_id"], TENANT, "w1"
        )
    with db.transaction() as conn:
        node_runs.set_status(conn, node_a["node_run_id"], TENANT, to_status="running")
        with conn.cursor() as cur:
            cur.execute(STALE, (first_attempt["attempt_id"],))

    lost = fail_lost_running_attempts(db, lease_seconds=60)
    assert [item["attempt_id"] for item in lost] == [first_attempt["attempt_id"]]

    orch.advance_run(run["run_id"], TENANT)

    attempts = orch.attempts_of(node_a["node_run_id"], TENANT)
    assert len(attempts) == 2, "重试应新建 attempt 而非复用"
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "WORKER_LOST"
    assert attempts[1]["attempt_no"] == 2
    assert attempts[1]["status"] == "queued"
    assert node_runs_status(orch, run["run_id"], "node_a") == "ready"


def node_runs_status(orch, run_id: str, node_key: str) -> str:
    return orch.run_state(run_id, TENANT)["statuses"][node_key]
