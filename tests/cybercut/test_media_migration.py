"""WB-02 · 0003 迁移与 media_assets 约束测试（真实 PostgreSQL）。"""
from __future__ import annotations

import uuid

import pytest
from psycopg2 import errors as pg_errors

from cybercut.storage import media_assets, migrate, projects
from tests.cybercut.helpers import TENANT

OTHER = "tenant-b"


def _asset(db, tenant: str = TENANT, project_id: str | None = None,
           sha: str | None = None, status: str = "ready",
           *, create_project: bool = True):
    sha256 = sha or uuid.uuid4().hex * 2
    with db.transaction() as conn:
        if create_project:
            created = projects.create(
                conn, tenant_id=tenant,
                name=project_id or f"proj-{uuid.uuid4().hex[:6]}",
                project_id=project_id,
            )
            pid = created["project_id"]
        else:
            pid = project_id
        row = media_assets.insert_ingesting(
            conn, project_id=pid, tenant_id=tenant, media_kind="video",
            original_filename="a.mp4",
            source_relative_path="assets/x/original/a.mp4",
            sha256=sha256, size_bytes=10, asset_id=uuid.uuid4().hex,
        )
        if row is None:
            row = media_assets.get_by_sha(conn, pid, tenant, sha256)
        if status != "ingesting":
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cybercut.media_assets SET status = %s,"
                    " source_relative_path = %s WHERE asset_id = %s",
                    (status, f"assets/{row['asset_id']}/original/a.mp4",
                     row["asset_id"]),
                )
    return {"project_id": pid, "sha256": sha256, "asset": row}


def test_0003_applied_and_idempotent(db) -> None:
    assert migrate.apply_migrations(db) == []
    with db.transaction() as conn:
        versions = migrate.applied_versions(conn)
    assert "0003_media_assets" in versions


def test_media_assets_table_exists(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_schema='cybercut' AND table_name='media_assets'"
            )
            assert cur.fetchone() is not None


def test_runs_input_json_column_exists(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_schema='cybercut' AND table_name='runs'"
                "   AND column_name='input_json'"
            )
            assert cur.fetchone() is not None


def test_tenant_mismatch_rejected_by_database(db) -> None:
    seeded = _asset(db, TENANT)
    with pytest.raises(pg_errors.ForeignKeyViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cybercut.media_assets (asset_id, project_id,"
                    " tenant_id, media_kind, original_filename,"
                    " source_relative_path, sha256, size_bytes, status)"
                    " VALUES (%s, %s, %s, 'video', 'a.mp4', 'assets/x/original/a.mp4',"
                    " %s, 10, 'ready')",
                    (uuid.uuid4().hex, seeded["project_id"], OTHER,
                     uuid.uuid4().hex * 2),
                )


def test_project_sha_unique_constraint(db) -> None:
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    sha = uuid.uuid4().hex * 2
    with db.transaction() as conn:
        projects.create(conn, tenant_id=TENANT, name=project_id,
                        project_id=project_id)
    _asset(db, project_id=project_id, sha=sha, create_project=False)
    with pytest.raises(pg_errors.UniqueViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cybercut.media_assets (asset_id, project_id,"
                    " tenant_id, media_kind, original_filename,"
                    " source_relative_path, sha256, size_bytes, status)"
                    " VALUES (%s, %s, %s, 'video', 'b.mp4',"
                    " 'assets/y/original/b.mp4', %s, 12, 'ready')",
                    (uuid.uuid4().hex, project_id, TENANT, sha),
                )


def test_status_check_constraint(db) -> None:
    with pytest.raises(pg_errors.CheckViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cybercut.media_assets (asset_id, project_id,"
                    " tenant_id, media_kind, original_filename,"
                    " source_relative_path, sha256, size_bytes, status)"
                    " VALUES (%s, %s, %s, 'video', 'a.mp4', 'assets/x/original/a.mp4',"
                    " %s, 10, 'weird')",
                    (uuid.uuid4().hex, f"proj-{uuid.uuid4().hex[:8]}", TENANT,
                     uuid.uuid4().hex * 2),
                )


def test_same_sha_across_projects_is_allowed(db) -> None:
    sha = uuid.uuid4().hex * 2
    a = _asset(db, project_id=f"proj-{uuid.uuid4().hex[:8]}", sha=sha)
    b = _asset(db, project_id=f"proj-{uuid.uuid4().hex[:8]}", sha=sha)
    assert a["project_id"] != b["project_id"]
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM cybercut.media_assets WHERE sha256 = %s",
                (sha,),
            )
            assert cur.fetchone()["n"] == 2
