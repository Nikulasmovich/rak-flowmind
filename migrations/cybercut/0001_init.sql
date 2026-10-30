-- CyberCut WB-01 · control plane schema (PostgreSQL)
-- 约定：
--   * 全部业务对象位于 cybercut schema；不改动 flowmind schema / tasks 表。
--   * 时间统一 timestamptz（UTC）；ID 为 UUID4 字符串；JSON 载荷用 jsonb。
--   * 本文件必须可重复执行（幂等）：全部 CREATE ... IF NOT EXISTS / DROP TRIGGER IF EXISTS。
--   * 不做 DROP TABLE：二次执行不改变既有数据。

CREATE SCHEMA IF NOT EXISTS cybercut;

-- ── 迁移账本（供 migrate.py 记录已应用版本）──
CREATE TABLE IF NOT EXISTS cybercut.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- ── 通用触发器函数 ──
CREATE OR REPLACE FUNCTION cybercut.touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 不可变表守卫：任何 UPDATE/DELETE 直接报错（append-only / immutable 语义）
CREATE OR REPLACE FUNCTION cybercut.forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'immutable table %: % is forbidden', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

-- ══════════════════════════════════════════════════════════════════
-- 1) projects
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.projects (
    project_id             text PRIMARY KEY,
    tenant_id              text NOT NULL,
    name                   text NOT NULL,
    goal_json              jsonb NOT NULL DEFAULT '{}'::jsonb,
    automation_policy_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                 text NOT NULL,
    current_run_id         text,
    version                integer NOT NULL DEFAULT 1,
    idempotency_key        text,
    archived_at            timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_tenant
    ON cybercut.projects (tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_tenant_idem
    ON cybercut.projects (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

DROP TRIGGER IF EXISTS trg_projects_touch ON cybercut.projects;
CREATE TRIGGER trg_projects_touch BEFORE UPDATE ON cybercut.projects
    FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at();

-- ══════════════════════════════════════════════════════════════════
-- 2) runs
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.runs (
    run_id              text PRIMARY KEY,
    project_id          text NOT NULL REFERENCES cybercut.projects(project_id) ON DELETE CASCADE,
    tenant_id           text NOT NULL,
    run_no              integer NOT NULL,
    status              text NOT NULL,
    pipeline_version    text NOT NULL,
    requested_from_node text,
    started_at          timestamptz,
    finished_at         timestamptz,
    cancel_requested_at timestamptz,
    failure_code        text,
    failure_message     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, run_no)
);
CREATE INDEX IF NOT EXISTS idx_runs_tenant
    ON cybercut.runs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_project
    ON cybercut.runs (project_id, run_no DESC);

DROP TRIGGER IF EXISTS trg_runs_touch ON cybercut.runs;
CREATE TRIGGER trg_runs_touch BEFORE UPDATE ON cybercut.runs
    FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at();

-- ══════════════════════════════════════════════════════════════════
-- 3) node_runs
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.node_runs (
    node_run_id        text PRIMARY KEY,
    run_id             text NOT NULL REFERENCES cybercut.runs(run_id) ON DELETE CASCADE,
    tenant_id          text NOT NULL,
    node_key           text NOT NULL,
    status             text NOT NULL,
    depends_on         jsonb NOT NULL DEFAULT '[]'::jsonb,
    input_revision_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
    latest_attempt_no  integer NOT NULL DEFAULT 0,
    approval_required  boolean NOT NULL DEFAULT false,
    invalidated_at     timestamptz,
    invalidated_reason text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_node_runs_run
    ON cybercut.node_runs (run_id, node_key);
CREATE INDEX IF NOT EXISTS idx_node_runs_tenant
    ON cybercut.node_runs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_node_runs_status
    ON cybercut.node_runs (status);

DROP TRIGGER IF EXISTS trg_node_runs_touch ON cybercut.node_runs;
CREATE TRIGGER trg_node_runs_touch BEFORE UPDATE ON cybercut.node_runs
    FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at();

-- ══════════════════════════════════════════════════════════════════
-- 4) node_attempts
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.node_attempts (
    attempt_id    text PRIMARY KEY,
    node_run_id   text NOT NULL REFERENCES cybercut.node_runs(node_run_id) ON DELETE CASCADE,
    tenant_id     text NOT NULL,
    attempt_no    integer NOT NULL,
    status        text NOT NULL,
    executor_kind text NOT NULL,
    executor_ref  text,
    input_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_json   jsonb,
    claimed_by    text,
    claimed_at    timestamptz,
    heartbeat_at  timestamptz,
    available_at  timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    error_code    text,
    error_message text,
    error_details jsonb,
    progress      numeric NOT NULL DEFAULT 0,
    stage         text,
    message       text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_run_id, attempt_no)
);
-- claim 主查询索引：status=queued 且 available_at<=now()，按 available_at/created_at 升序
CREATE INDEX IF NOT EXISTS idx_attempts_claimable
    ON cybercut.node_attempts (available_at, created_at)
    WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_attempts_lease
    ON cybercut.node_attempts (status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_attempts_node_run
    ON cybercut.node_attempts (node_run_id, attempt_no DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_tenant
    ON cybercut.node_attempts (tenant_id);

DROP TRIGGER IF EXISTS trg_node_attempts_touch ON cybercut.node_attempts;
CREATE TRIGGER trg_node_attempts_touch BEFORE UPDATE ON cybercut.node_attempts
    FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at();

-- ══════════════════════════════════════════════════════════════════
-- 5) artifact_revisions（不可变）
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.artifact_revisions (
    artifact_revision_id text PRIMARY KEY,
    project_id           text NOT NULL REFERENCES cybercut.projects(project_id) ON DELETE CASCADE,
    run_id               text NOT NULL,
    node_run_id          text NOT NULL,
    attempt_id           text NOT NULL,
    tenant_id            text NOT NULL,
    kind                 text NOT NULL,
    revision_no          integer NOT NULL,
    relative_path        text NOT NULL,
    sha256               text NOT NULL,
    size_bytes           bigint NOT NULL,
    metadata_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    supersedes_id        text,
    status               text NOT NULL DEFAULT 'active',
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, kind, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_node_run
    ON cybercut.artifact_revisions (node_run_id, kind, revision_no DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_tenant
    ON cybercut.artifact_revisions (tenant_id);

DROP TRIGGER IF EXISTS trg_artifact_revisions_immutable ON cybercut.artifact_revisions;
CREATE TRIGGER trg_artifact_revisions_immutable
    BEFORE UPDATE OR DELETE ON cybercut.artifact_revisions
    FOR EACH ROW EXECUTE FUNCTION cybercut.forbid_mutation();

-- ══════════════════════════════════════════════════════════════════
-- 6) approvals
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.approvals (
    approval_id          text PRIMARY KEY,
    project_id           text NOT NULL,
    run_id               text NOT NULL,
    node_run_id          text NOT NULL,
    artifact_revision_id text,
    tenant_id            text NOT NULL,
    status               text NOT NULL,
    feedback             text,
    scope_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    expected_revision    integer NOT NULL,
    decided_by           text,
    decided_at           timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_run_id, expected_revision)
);
CREATE INDEX IF NOT EXISTS idx_approvals_node_run
    ON cybercut.approvals (node_run_id, expected_revision DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_tenant
    ON cybercut.approvals (tenant_id, status);

DROP TRIGGER IF EXISTS trg_approvals_touch ON cybercut.approvals;
CREATE TRIGGER trg_approvals_touch BEFORE UPDATE ON cybercut.approvals
    FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at();

-- ══════════════════════════════════════════════════════════════════
-- 7) events（只追加）
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.events (
    event_id    bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    project_id  text,
    run_id      text,
    node_run_id text,
    event_type  text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_scope
    ON cybercut.events (tenant_id, project_id, event_id DESC);

DROP TRIGGER IF EXISTS trg_events_append_only ON cybercut.events;
CREATE TRIGGER trg_events_append_only
    BEFORE UPDATE OR DELETE ON cybercut.events
    FOR EACH ROW EXECUTE FUNCTION cybercut.forbid_mutation();

-- ══════════════════════════════════════════════════════════════════
-- 8) idempotency_keys
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.idempotency_keys (
    tenant_id      text NOT NULL,
    operation      text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash   text NOT NULL,
    response_json  jsonb,
    resource_id    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz,
    PRIMARY KEY (tenant_id, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idem_expires
    ON cybercut.idempotency_keys (expires_at);
