"""重启恢复测试：queued attempt 在「重启」后仍可被新 worker 领取 —— 真实 PostgreSQL。

「重启」= 丢弃全部进程内对象（Database / Orchestrator / Worker / Registry），
用全新实例从数据库读取状态继续执行 —— 验证执行状态完全持久化。
"""
from __future__ import annotations

from cybercut.storage.db import Database
from tests.cybercut.helpers import (
    TENANT,
    make_orchestrator,
    make_project,
    make_registry,
    make_run,
    make_worker,
    statuses,
)


def test_queued_attempt_is_claimable_after_restart(db, data_root) -> None:
    # ── 重启前：创建项目/Run 并完成 node_a，node_b 处于 queued ──
    orch_before = make_orchestrator(db, data_root)
    worker_before = make_worker(db, orch_before, make_registry(), data_root,
                               worker_id="old-worker")
    project = make_project(orch_before)
    run = make_run(orch_before, project=project)
    worker_before.step()
    assert statuses(orch_before, run["run_id"])["node_b"] == "ready"

    node_b = orch_before.run_state(run["run_id"], TENANT)["nodes"]["node_b"]
    queued = orch_before.attempts_of(node_b["node_run_id"], TENANT)
    assert queued[-1]["status"] == "queued"

    # ── 「重启」：全新实例，无任何内存状态 ──
    db_after = Database(db.dsn)
    orch_after = make_orchestrator(db_after, data_root)
    worker_after = make_worker(db_after, orch_after, make_registry(), data_root,
                               worker_id="new-worker")

    outcomes = worker_after.drain()
    assert outcomes, "重启后应能继续领取 queued attempt"
    assert outcomes[0].node_key == "node_b"
    assert statuses(orch_after, run["run_id"])["node_b"] == "succeeded"

    # 原实例从同一数据库也观察到最新状态
    assert statuses(orch_before, run["run_id"])["node_b"] == "succeeded"


def test_running_attempt_survives_restart_until_lease_expires(db, data_root) -> None:
    """重启不会凭空丢失 running：租约到期前保持 running，到期后按 WORKER_LOST 处理。"""
    from cybercut.executor.claim import claim_next_attempt
    from cybercut.executor.lease import fail_lost_running_attempts, recover_expired_claims
    from cybercut.storage import node_attempts

    orch = make_orchestrator(db, data_root)
    project = make_project(orch)
    run = make_run(orch, project=project)
    node_a = orch.run_state(run["run_id"], TENANT)["nodes"]["node_a"]

    database = Database(db.dsn)
    claimed = claim_next_attempt(database, "worker-doomed")
    with database.transaction() as conn:
        node_attempts.mark_running(conn, claimed["attempt_id"], TENANT, "worker-doomed")

    # 重启后立刻扫描：心跳新鲜 -> 不动
    assert recover_expired_claims(db, lease_seconds=60) == []
    assert fail_lost_running_attempts(db, lease_seconds=60) == []

    # 心跳过期 -> 判死
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cybercut.node_attempts"
                " SET heartbeat_at = now() - interval '10 minutes' WHERE attempt_id = %s",
                (claimed["attempt_id"],),
            )
    lost = fail_lost_running_attempts(db, lease_seconds=60)
    assert [item["attempt_id"] for item in lost] == [claimed["attempt_id"]]

    orch.advance_run(run["run_id"], TENANT)
    attempts = orch.attempts_of(node_a["node_run_id"], TENANT)
    assert len(attempts) == 2
    assert attempts[0]["error_code"] == "WORKER_LOST"
    assert attempts[1]["status"] == "queued"
