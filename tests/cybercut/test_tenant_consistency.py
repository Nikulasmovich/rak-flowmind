"""WB-01R §二.3：数据库强制租户一致性测试（复合外键，真实 PostgreSQL）。"""
from __future__ import annotations

import uuid

import pytest
from psycopg2 import errors as pg_errors

from cybercut.storage import node_attempts, node_runs, projects, runs

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _seed_chain(db, tenant: str) -> dict:
    """建立一条合法的同租户父链。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=tenant, name=f"p-{tenant}")
        run = runs.create(
            conn, project_id=project["project_id"], tenant_id=tenant,
            pipeline_version="v1",
        )
        node_run = node_runs.create(
            conn, run_id=run["run_id"], tenant_id=tenant, node_key="node_a",
            status="ready",
        )
        attempt = node_attempts.create(
            conn, node_run_id=node_run["node_run_id"], tenant_id=tenant, attempt_no=1,
        )
    return {"project": project, "run": run, "node_run": node_run, "attempt": attempt}


def _insert_run(conn, *, run_id, project_id, tenant, run_no=99):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.runs (run_id, project_id, tenant_id, run_no, status,"
            " pipeline_version) VALUES (%s, %s, %s, %s, 'queued', 'v1')",
            (run_id, project_id, tenant, run_no),
        )


def _insert_node_run(conn, *, node_run_id, run_id, tenant, node_key="nx"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.node_runs (node_run_id, run_id, tenant_id, node_key,"
            " status) VALUES (%s, %s, %s, %s, 'blocked')",
            (node_run_id, run_id, tenant, node_key),
        )


def _insert_attempt(conn, *, attempt_id, node_run_id, tenant, attempt_no=99):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.node_attempts (attempt_id, node_run_id, tenant_id,"
            " attempt_no, status, executor_kind)"
            " VALUES (%s, %s, %s, %s, 'queued', 'cybercut_worker')",
            (attempt_id, node_run_id, tenant, attempt_no),
        )


def _insert_artifact(conn, *, artifact_revision_id, seeded, tenant, kind="script",
                     revision_no=99, supersedes_id=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.artifact_revisions (artifact_revision_id, project_id,"
            " run_id, node_run_id, attempt_id, tenant_id, kind, revision_no,"
            " relative_path, sha256, size_bytes, supersedes_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                artifact_revision_id,
                seeded["project"]["project_id"],
                seeded["run"]["run_id"],
                seeded["node_run"]["node_run_id"],
                seeded["attempt"]["attempt_id"],
                tenant,
                kind,
                revision_no,
                f"artifacts/{kind}.json",
                "0" * 64,
                1,
                supersedes_id,
            ),
        )


def _insert_approval(conn, *, approval_id, seeded, tenant, expected_revision=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cybercut.approvals (approval_id, project_id, run_id, node_run_id,"
            " tenant_id, status, expected_revision)"
            " VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
            (
                approval_id,
                seeded["project"]["project_id"],
                seeded["run"]["run_id"],
                seeded["node_run"]["node_run_id"],
                tenant,
                expected_revision,
            ),
        )


# ── 跨租户引用必须被数据库拒绝 ──


def test_cross_tenant_run_rejected(db) -> None:
    a = _seed_chain(db, TENANT_A)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_run(
                conn, run_id=str(uuid.uuid4()), project_id=a["project"]["project_id"],
                tenant=TENANT_B,
            )


def test_cross_tenant_node_run_rejected(db) -> None:
    a = _seed_chain(db, TENANT_A)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_node_run(
                conn, node_run_id=str(uuid.uuid4()), run_id=a["run"]["run_id"],
                tenant=TENANT_B,
            )


def test_cross_tenant_attempt_rejected(db) -> None:
    a = _seed_chain(db, TENANT_A)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_attempt(
                conn, attempt_id=str(uuid.uuid4()),
                node_run_id=a["node_run"]["node_run_id"], tenant=TENANT_B,
            )


def test_cross_tenant_artifact_rejected(db) -> None:
    a = _seed_chain(db, TENANT_A)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_artifact(
                conn, artifact_revision_id=str(uuid.uuid4()), seeded=a,
                tenant=TENANT_B,
            )


def test_cross_tenant_approval_rejected(db) -> None:
    a = _seed_chain(db, TENANT_A)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_approval(
                conn, approval_id=str(uuid.uuid4()), seeded=a, tenant=TENANT_B,
            )


def test_cross_tenant_supersedes_rejected(db) -> None:
    """版本链必须同租户：A 的产物不得 supersede B 的产物。"""
    a = _seed_chain(db, TENANT_A)
    b = _seed_chain(db, TENANT_B)
    b_artifact_id = str(uuid.uuid4())
    with db.transaction() as conn:
        _insert_artifact(
            conn, artifact_revision_id=b_artifact_id, seeded=b, tenant=TENANT_B,
            kind="script", revision_no=1,
        )
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            _insert_artifact(
                conn, artifact_revision_id=str(uuid.uuid4()), seeded=a,
                tenant=TENANT_A, kind="script", revision_no=1,
                supersedes_id=b_artifact_id,
            )


def test_approval_artifact_reference_tenant_enforced(db) -> None:
    """approvals.artifact_revision_id 也必须同租户。"""
    a = _seed_chain(db, TENANT_A)
    b = _seed_chain(db, TENANT_B)
    a_artifact_id = str(uuid.uuid4())
    with db.transaction() as conn:
        _insert_artifact(
            conn, artifact_revision_id=a_artifact_id, seeded=a, tenant=TENANT_A,
            kind="script", revision_no=1,
        )
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cybercut.approvals (approval_id, project_id, run_id,"
                    " node_run_id, artifact_revision_id, tenant_id, status,"
                    " expected_revision) VALUES (%s, %s, %s, %s, %s, %s, 'pending', 1)",
                    (
                        str(uuid.uuid4()), b["project"]["project_id"],
                        b["run"]["run_id"], b["node_run"]["node_run_id"],
                        a_artifact_id, TENANT_B,
                    ),
                )


# ── 正常同租户链路不受影响 ──


def test_same_tenant_chain_is_accepted(db) -> None:
    a = _seed_chain(db, TENANT_A)
    artifact_id = str(uuid.uuid4())
    with db.transaction() as conn:
        _insert_artifact(
            conn, artifact_revision_id=artifact_id, seeded=a, tenant=TENANT_A,
            kind="script", revision_no=1,
        )
        _insert_approval(
            conn, approval_id=str(uuid.uuid4()), seeded=a, tenant=TENANT_A,
            expected_revision=1,
        )
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM cybercut.artifact_revisions"
                " WHERE artifact_revision_id = %s AND tenant_id = %s",
                (artifact_id, TENANT_A),
            )
            assert cur.fetchone()["n"] == 1


def test_nullable_references_allowed(db) -> None:
    """可空引用（supersedes_id / approvals.artifact_revision_id）为 NULL 时放行。"""
    a = _seed_chain(db, TENANT_A)
    with db.transaction() as conn:
        _insert_artifact(
            conn, artifact_revision_id=str(uuid.uuid4()), seeded=a, tenant=TENANT_A,
            kind="script", revision_no=1, supersedes_id=None,
        )
        _insert_approval(
            conn, approval_id=str(uuid.uuid4()), seeded=a, tenant=TENANT_A,
            expected_revision=1,
        )


def test_composite_constraints_exist_in_catalog(db) -> None:
    """目录断言：0002 定义的 12 个复合外键确实存在。"""
    expected = {
        "fk_runs_project_tenant",
        "fk_node_runs_run_tenant",
        "fk_node_attempts_node_run_tenant",
        "fk_artifacts_project_tenant",
        "fk_artifacts_run_tenant",
        "fk_artifacts_node_run_tenant",
        "fk_artifacts_attempt_tenant",
        "fk_artifacts_supersedes_tenant",
        "fk_approvals_project_tenant",
        "fk_approvals_run_tenant",
        "fk_approvals_node_run_tenant",
        "fk_approvals_artifact_tenant",
    }
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE n.nspname = 'cybercut' AND c.contype = 'f'"
            )
            found = {r["conname"] for r in cur.fetchall()}
    missing = expected - found
    assert not missing, f"缺少复合外键：{sorted(missing)}"
    # 旧单列外键必须已被移除
    assert "runs_project_id_fkey" not in found
    assert "node_runs_run_id_fkey" not in found
    assert "node_attempts_node_run_id_fkey" not in found
    assert "artifact_revisions_project_id_fkey" not in found


def test_events_has_no_foreign_key_by_design(db) -> None:
    """events 刻意不设外键（append-only 审计日志，见 ADR-0002 §决策 8）。"""
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE n.nspname = 'cybercut' AND t.relname = 'events'"
                "   AND c.contype = 'f'"
            )
            assert cur.fetchone()["n"] == 0
