"""产物登记测试：哈希/大小、版本链、不可覆盖（含数据库触发器）。

WB-01R 起 `artifact_revisions` 带复合外键（tenant 一致性），因此本测试必须使用
**真实的 project/run/node_run/node_attempt** 父记录，不能再使用占位 ID。
"""
from __future__ import annotations

import hashlib

import pytest
from psycopg2 import errors as pg_errors

from cybercut.contracts.errors import ArtifactImmutableError
from cybercut.storage import artifacts, node_attempts, node_runs, projects, runs

TENANT = "tenant-a"


def _seed(db, tenant: str = TENANT) -> dict:
    """建立合法的同租户父链：project -> run -> node_run -> attempt。"""
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
        attempt = node_attempts.create(
            conn, node_run_id=node_run["node_run_id"], tenant_id=tenant, attempt_no=1,
        )
    return {"project": project, "run": run, "node_run": node_run, "attempt": attempt}


def _register(db, seeded: dict, *, kind="script", revision_no=None,
              path="artifacts/s.json", tenant: str = TENANT) -> dict:
    with db.transaction() as conn:
        return artifacts.register(
            conn,
            project_id=seeded["project"]["project_id"],
            run_id=seeded["run"]["run_id"],
            node_run_id=seeded["node_run"]["node_run_id"],
            attempt_id=seeded["attempt"]["attempt_id"],
            tenant_id=tenant,
            kind=kind,
            relative_path=path,
            sha256=hashlib.sha256(path.encode()).hexdigest(),
            size_bytes=len(path),
            revision_no=revision_no,
        )


def test_artifact_records_hash_and_size(db) -> None:
    seeded = _seed(db)
    row = _register(db, seeded)
    assert row["sha256"] == hashlib.sha256(b"artifacts/s.json").hexdigest()
    assert row["size_bytes"] == len("artifacts/s.json")
    assert row["revision_no"] == 1
    assert row["supersedes_id"] is None


def test_new_revision_supersedes_previous(db) -> None:
    seeded = _seed(db)
    first = _register(db, seeded)
    second = _register(db, seeded, path="artifacts/s-v2.json")
    assert second["revision_no"] == 2
    assert second["supersedes_id"] == first["artifact_revision_id"]
    with db.transaction() as conn:
        old = artifacts.get(conn, first["artifact_revision_id"], TENANT)
    assert old["revision_no"] == 1 and old["relative_path"] == "artifacts/s.json"


def test_artifact_revision_cannot_be_overwritten(db) -> None:
    seeded = _seed(db)
    _register(db, seeded, revision_no=1)
    with pytest.raises(ArtifactImmutableError) as excinfo:
        _register(db, seeded, revision_no=1, path="artifacts/other.json")
    assert excinfo.value.details["revision_no"] == 1


def test_artifact_update_blocked_by_database_trigger(db) -> None:
    seeded = _seed(db)
    row = _register(db, seeded)
    with pytest.raises(pg_errors.RestrictViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cybercut.artifact_revisions SET relative_path = %s"
                    " WHERE artifact_revision_id = %s",
                    ("artifacts/hacked.json", row["artifact_revision_id"]),
                )


def test_artifact_delete_blocked_by_database_trigger(db) -> None:
    seeded = _seed(db)
    row = _register(db, seeded)
    with pytest.raises(pg_errors.RestrictViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cybercut.artifact_revisions"
                    " WHERE artifact_revision_id = %s",
                    (row["artifact_revision_id"],),
                )


def test_latest_for_kind_returns_highest_revision(db) -> None:
    seeded = _seed(db)
    _register(db, seeded, kind="script")
    _register(db, seeded, kind="script", path="artifacts/s2.json")
    _register(db, seeded, kind="audio", path="artifacts/a.json")
    with db.transaction() as conn:
        latest = artifacts.latest_for_kind(
            conn, seeded["project"]["project_id"], TENANT, "script"
        )
    assert latest["revision_no"] == 2
