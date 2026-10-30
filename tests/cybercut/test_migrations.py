"""迁移测试：幂等、重放安全、**升级路径**、版本顺序（真实 PostgreSQL）。"""
from __future__ import annotations

import shutil
from pathlib import Path

from cybercut.storage import migrate, node_attempts, node_runs, projects, runs

_INIT_SQL = migrate.DEFAULT_MIGRATIONS_DIR / "0001_init.sql"

_EXPECTED_TABLES = (
    "schema_migrations",
    "projects",
    "runs",
    "node_runs",
    "node_attempts",
    "artifact_revisions",
    "approvals",
    "events",
    "idempotency_keys",
    "media_assets",
    "creative_brief_revisions",
    "script_revisions",
    "storyboard_revisions",
    "asset_match_revisions",
    "edit_plan_revisions",
    "template_bind_revisions",
)

_EXPECTED_VERSIONS = ["0001_init", "0002_tenant_consistency",
                      "0003_media_assets", "0004_creative_revisions",
                      "0005_run_project_composite",
                      "0006_edit_planning"]


def _table_names(db) -> set[str]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'cybercut'"
            )
            return {r["table_name"] for r in cur.fetchall()}


def _drop_schema(db) -> None:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS cybercut CASCADE")


def _only_0001_dir(data_root: Path) -> Path:
    target = Path(data_root) / "only-0001"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(_INIT_SQL, target / "0001_init.sql")
    return target


def _seed_chain(db, tenant: str) -> dict:  # noqa: D401
    with db.transaction() as conn:
        project = projects.create(conn, tenant_id=tenant, name="upgrade")
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


def _foreign_keys(db) -> set[str]:
    with db.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conname FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE n.nspname = 'cybercut' AND c.contype = 'f'"
            )
            return {r["conname"] for r in cur.fetchall()}


# ── 幂等 ──


def test_migration_is_idempotent(db) -> None:
    assert migrate.apply_migrations(db) == []
    assert migrate.apply_migrations(db) == []
    assert _table_names(db) >= set(_EXPECTED_TABLES)


def test_raw_sql_replay_is_safe(db) -> None:
    """账本丢失也能安全重放：两份 DDL 直接再执行一次不报错。"""
    for path in sorted(migrate.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    assert _table_names(db) >= set(_EXPECTED_TABLES)


def test_ledger_records_versions(db) -> None:
    with db.transaction() as conn:
        versions = migrate.applied_versions(conn)
    assert "0001_init" in versions
    assert "0002_tenant_consistency" in versions


def test_rows_survive_reapply(db) -> None:
    with db.transaction() as conn:
        projects.create(conn, tenant_id="t-survive", name="survivor")
    migrate.apply_migrations(db)
    with db.transaction() as conn:
        rows = projects.list_for_tenant(conn, "t-survive")
    assert len(rows) == 1


# ── 顺序与升级路径 ──


def test_migration_order_on_fresh_schema(db, data_root) -> None:
    """全新数据库：应用顺序必须是 0001 -> 0002。"""
    try:
        _drop_schema(db)
        applied = migrate.apply_migrations(db)
        assert applied == _EXPECTED_VERSIONS, f"应用顺序错误：{applied}"
        with db.transaction() as conn:
            versions = sorted(migrate.applied_versions(conn))
        assert versions == _EXPECTED_VERSIONS
        assert _table_names(db) >= set(_EXPECTED_TABLES)
    finally:
        migrate.apply_migrations(db)
        assert migrate.apply_migrations(db) == []


def test_upgrade_from_0001_only_succeeds(db, data_root) -> None:
    """只应用 0001 的库 -> 升级到最新必须成功，且数据不丢、约束到位。"""
    try:
        _drop_schema(db)
        only1 = _only_0001_dir(data_root)

        assert migrate.apply_migrations(db, only1) == ["0001_init"]
        with db.transaction() as conn:
            projects.create(conn, tenant_id="upgrade-tenant", name="upgrade")
        with db.transaction() as conn:
            seeded = {"project_id": projects.list_for_tenant(
                conn, "upgrade-tenant")[0]["project_id"]}
        before_fks = _foreign_keys(db)
        assert "fk_runs_project_tenant" not in before_fks, "升级前不应有复合外键"

        applied = migrate.apply_migrations(db)
        assert applied == ["0002_tenant_consistency", "0003_media_assets",
                           "0004_creative_revisions",
                           "0005_run_project_composite",
                           "0006_edit_planning"], \
            f"升级应用项错误：{applied}"

        # ① 既有数据仍在
        with db.transaction() as conn:
            project = projects.get(
                conn, seeded["project_id"], "upgrade-tenant"
            )
        assert project is not None and project["name"] == "upgrade"

        # ② 复合外键已建立
        assert "fk_runs_project_tenant" in _foreign_keys(db)
        assert "fk_node_attempts_node_run_tenant" in _foreign_keys(db)

        # ③ 账本记录正确
        with db.transaction() as conn:
            assert sorted(migrate.applied_versions(conn)) == _EXPECTED_VERSIONS
    finally:
        migrate.apply_migrations(db)
        assert migrate.apply_migrations(db) == []


def test_0002_is_repeatable_after_ledger_loss(db) -> None:
    """删掉账本中的 0002 记录后再次应用，必须成功且不报错（幂等 DDL）。"""
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cybercut.schema_migrations WHERE version = %s",
                    ("0002_tenant_consistency",),
                )
        applied = migrate.apply_migrations(db)
        assert applied == ["0002_tenant_consistency"]
        assert "fk_runs_project_tenant" in _foreign_keys(db)
    finally:
        migrate.apply_migrations(db)


def test_0002_creates_constraint_despite_same_name_in_other_schema(db) -> None:
    """WB-01R2 §五：其他 schema 存在同名约束时，CyberCut 约束仍必须被创建。

    判重条件必须同时限定 `conname` 与 `conrelid`（而非仅按名字）。
    """
    other_schema = "cybercut_r2_collision"
    try:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {other_schema} CASCADE")
                cur.execute(f"CREATE SCHEMA {other_schema}")
                cur.execute(f"CREATE TABLE {other_schema}.dummy (id int)")
                # 在**其他 schema** 制造同名约束（CHECK，避免与外键类型混淆）
                cur.execute(
                    f"ALTER TABLE {other_schema}.dummy"
                    " ADD CONSTRAINT fk_runs_project_tenant CHECK (id > 0)"
                )
                # 移除 CyberCut 侧约束 + 账本记录 -> 迫使 0002 重新创建
                cur.execute(
                    "ALTER TABLE cybercut.runs"
                    " DROP CONSTRAINT IF EXISTS fk_runs_project_tenant"
                )
                cur.execute(
                    "DELETE FROM cybercut.schema_migrations WHERE version = %s",
                    ("0002_tenant_consistency",),
                )

        assert migrate.apply_migrations(db) == ["0002_tenant_consistency"]

        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT n.nspname, t.relname, c.contype"
                    "  FROM pg_constraint c"
                    "  JOIN pg_class t ON t.oid = c.conrelid"
                    "  JOIN pg_namespace n ON n.oid = t.relnamespace"
                    " WHERE c.conname = 'fk_runs_project_tenant'"
                )
                found = {(r["nspname"], r["relname"], r["contype"])
                         for r in cur.fetchall()}
        assert ("cybercut", "runs", "f") in found, (
            "其他 schema 同名约束不得导致 CyberCut 外键被跳过创建"
        )
        assert (other_schema, "dummy", "c") in found
    finally:
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {other_schema} CASCADE")
        migrate.apply_migrations(db)
