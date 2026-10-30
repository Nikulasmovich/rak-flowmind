"""WB-03 · 0004 迁移测试（真实 PostgreSQL）。"""
from __future__ import annotations

import uuid

import pytest

from cybercut.storage import creative_revisions, migrate, projects, runs

TENANT = "tenant-wb03"
VERSIONS = ["0001_init", "0002_tenant_consistency", "0003_media_assets",
            "0004_creative_revisions", "0005_run_project_composite",
            "0006_edit_planning"]
TABLES = ("creative_brief_revisions", "script_revisions",
          "storyboard_revisions", "asset_match_revisions")

_INSERT = (
    "INSERT INTO cybercut.{table} ({pk}, project_id, run_id, tenant_id,"
    " revision_no, status, payload_json, prompt_version, model_id)"
    " VALUES (%s, %s, %s, %s, %s, 'active', %s, 'p', 'm')"
)


@pytest.fixture
def env(db):
    """建项目 + Run，返回 (project_id, run_id)。"""
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=TENANT, name="wb03")
        run = runs.create(conn, project_id=project["project_id"],
                          tenant_id=TENANT, pipeline_version="creative-v1")
    return project["project_id"], run["run_id"]


def _insert(db, table: str, pk: str, project_id: str, run_id: str,
            revision_no: int, tenant: str = TENANT):
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _INSERT.format(table=table, pk=pk),
                (uuid.uuid4().hex, project_id, run_id, tenant, revision_no,
                 "{}"),
            )


def test_0004_applied_and_idempotent(db) -> None:
    with db.transaction() as conn:
        versions = sorted(migrate.applied_versions(conn))
    assert versions == VERSIONS
    # 重复执行不产生新应用项
    assert migrate.apply_migrations(db) == []


def test_creative_tables_exist(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'cybercut' ORDER BY 1")
            names = {r["table_name"] for r in cur.fetchall()}
    assert set(TABLES) <= names


def test_each_table_has_required_columns(db) -> None:
    expected = {
        "revision_no", "status", "payload_json", "prompt_version", "model_id",
        "created_by", "supersedes_id", "tenant_id", "project_id", "run_id",
        "created_at", "updated_at",
    }
    with db.transaction() as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'cybercut' AND table_name = %s",
                    (table,))
                cols = {r["column_name"] for r in cur.fetchall()}
                assert expected <= cols, f"{table} 缺列：{expected - cols}"


@pytest.mark.parametrize("table", TABLES)
def test_revision_no_unique_per_project(db, env, table: str) -> None:
    project_id, run_id = env
    pk = {"creative_brief_revisions": "brief_revision_id",
          "script_revisions": "script_revision_id",
          "storyboard_revisions": "storyboard_revision_id",
          "asset_match_revisions": "match_revision_id"}[table]
    _insert(db, table, pk, project_id, run_id, 1)
    with pytest.raises(Exception) as excinfo:
        _insert(db, table, pk, project_id, run_id, 1)
    assert excinfo.value.__class__.__name__ == "UniqueViolation", (
        f"重复 (project, tenant, revision_no) 必须被拒绝：{table}")


@pytest.mark.parametrize("table", TABLES)
def test_revision_no_must_be_positive(db, env, table: str) -> None:
    project_id, run_id = env
    pk = {"creative_brief_revisions": "brief_revision_id",
          "script_revisions": "script_revision_id",
          "storyboard_revisions": "storyboard_revision_id",
          "asset_match_revisions": "match_revision_id"}[table]
    with pytest.raises(Exception) as excinfo:
        _insert(db, table, pk, project_id, run_id, 0)
    assert excinfo.value.__class__.__name__ == "CheckViolation"


@pytest.mark.parametrize("table", TABLES)
def test_tenant_mismatch_rejected_by_database(db, env, table: str) -> None:
    """复合外键：project_id 归属的 tenant 与行内 tenant_id 不一致必须被拒绝。"""
    project_id, run_id = env
    pk = {"creative_brief_revisions": "brief_revision_id",
          "script_revisions": "script_revision_id",
          "storyboard_revisions": "storyboard_revision_id",
          "asset_match_revisions": "match_revision_id"}[table]
    with pytest.raises(Exception) as excinfo:
        _insert(db, table, pk, project_id, run_id, 1, tenant="tenant-other")
    assert excinfo.value.__class__.__name__ == "ForeignKeyViolation"


@pytest.mark.parametrize("table", TABLES)
def test_status_check_constraint(db, env, table: str) -> None:
    project_id, run_id = env
    pk = {"creative_brief_revisions": "brief_revision_id",
          "script_revisions": "script_revision_id",
          "storyboard_revisions": "storyboard_revision_id",
          "asset_match_revisions": "match_revision_id"}[table]
    with pytest.raises(Exception) as excinfo:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO cybercut.{table} ({pk}, project_id, run_id,"
                    " tenant_id, revision_no, status, payload_json,"
                    " prompt_version, model_id)"
                    " VALUES (%s, %s, %s, %s, 1, 'bogus', '{}', 'p', 'm')",
                    (uuid.uuid4().hex, project_id, run_id, TENANT))
    assert excinfo.value.__class__.__name__ == "CheckViolation"


def test_supersedes_foreign_key_enforced(db, env) -> None:
    """版本链：supersedes_id 必须指向同租户的既有版本。"""
    project_id, run_id = env
    with pytest.raises(Exception) as excinfo:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cybercut.script_revisions (script_revision_id,"
                    " project_id, run_id, tenant_id, revision_no, status,"
                    " payload_json, prompt_version, model_id, supersedes_id)"
                    " VALUES (%s, %s, %s, %s, 1, 'active', '{}', 'p', 'm', %s)",
                    (uuid.uuid4().hex, project_id, run_id, TENANT,
                     "no-such-revision"))
    assert excinfo.value.__class__.__name__ == "ForeignKeyViolation"


def test_repository_roundtrip_and_no_overwrite(db, env) -> None:
    """仓储：版本号自动递增 + 旧版本不可覆盖 + 版本链正确。"""
    project_id, run_id = env
    with db.transaction() as conn:
        first = creative_revisions.insert(
            conn, "brief", project_id=project_id, run_id=run_id,
            tenant_id=TENANT, payload={"k": 1}, prompt_version="p", model_id="m")
        second = creative_revisions.insert(
            conn, "brief", project_id=project_id, run_id=run_id,
            tenant_id=TENANT, payload={"k": 2}, prompt_version="p", model_id="m")
        latest = creative_revisions.latest(conn, "brief", project_id, TENANT)
        listed = creative_revisions.list_for_project(
            conn, "brief", project_id, TENANT)

    assert first["revision_no"] == 1
    assert second["revision_no"] == 2
    assert second["supersedes_id"] == first["brief_revision_id"]
    assert latest["brief_revision_id"] == second["brief_revision_id"]
    assert [row["revision_no"] for row in listed] == [1, 2]
    assert creative_revisions.payload_of(listed[0]) == {"k": 1}


def test_repository_rejects_explicit_duplicate_revision_no(db, env) -> None:
    from cybercut.contracts.errors import ConflictError

    project_id, run_id = env
    with db.transaction() as conn:
        creative_revisions.insert(
            conn, "storyboard", project_id=project_id, run_id=run_id,
            tenant_id=TENANT, payload={}, prompt_version="p", model_id="m",
            revision_no=7)
    with pytest.raises(ConflictError):
        with db.transaction() as conn:
            creative_revisions.insert(
                conn, "storyboard", project_id=project_id, run_id=run_id,
                tenant_id=TENANT, payload={}, prompt_version="p", model_id="m",
                revision_no=7)


def test_repository_set_status(db, env) -> None:
    project_id, run_id = env
    with db.transaction() as conn:
        row = creative_revisions.insert(
            conn, "match", project_id=project_id, run_id=run_id,
            tenant_id=TENANT, payload={}, prompt_version="p", model_id="m")
    with db.transaction() as conn:
        updated = creative_revisions.set_status(
            conn, "match", row["match_revision_id"], TENANT, status="rejected")
    assert updated["status"] == "rejected"
