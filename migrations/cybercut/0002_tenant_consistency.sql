-- CyberCut WB-01R/01R2 · 0002：数据库强制租户一致性（复合唯一键 + 复合外键）
--
-- 目标：把「父子记录 tenant_id 必须一致」从「应用层约定」升级为「数据库强制约束」。
--   * 父表：为 (主键, tenant_id) 建复合唯一索引（作为复合外键的引用目标）；
--   * 子表：移除 0001 的单列外键，改为 (父主键, tenant_id) 复合外键。
--   * 复合外键采用默认 MATCH SIMPLE：任一列为 NULL 时不校验 —— 因此可空引用
--     （artifact_revisions.supersedes_id、approvals.artifact_revision_id）在 NULL 时自然放行。
--
-- 幂等：本文件可重复执行（IF NOT EXISTS / DROP CONSTRAINT IF EXISTS / 判重后 ADD）。
-- 顺序：不修改 0001；本文件只在 0001 之后应用（由 cybercut.schema_migrations 保证）。
--
-- WB-01R2 §五：约束判重条件**同时限定 conname 与 conrelid**，
--   避免其他 schema 存在同名约束时误判为「已存在」而跳过创建。
--
-- events 表**刻意不设外键**，理由见 ADR-0002 §决策 8（append-only 审计日志与引用解耦）。

-- ══════════════════════════════════════════════════════════════════
-- 0) 迁移期辅助函数：仅在 (conname, conrelid) 均未命中时才添加外键
--    （用后即删，不留在 cybercut schema 中）
-- ══════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cybercut.ensure_fk_if_absent(
    p_conname text,
    p_table   regclass,
    p_definition text
) RETURNS void AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = p_conname
           AND conrelid = p_table          -- ← 关键：限定到目标表（表属于哪个 schema 由 regclass 解析）
    ) THEN
        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I %s',
            p_table, p_conname, p_definition
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ══════════════════════════════════════════════════════════════════
-- 1) 父表复合唯一索引：(主键, tenant_id)
-- ══════════════════════════════════════════════════════════════════
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_id_tenant
    ON cybercut.projects (project_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_id_tenant
    ON cybercut.runs (run_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_runs_id_tenant
    ON cybercut.node_runs (node_run_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_attempts_id_tenant
    ON cybercut.node_attempts (attempt_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_revisions_id_tenant
    ON cybercut.artifact_revisions (artifact_revision_id, tenant_id);

-- ══════════════════════════════════════════════════════════════════
-- 2) 移除 0001 的单列外键（改由复合外键承担，避免重复约束）
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE cybercut.runs               DROP CONSTRAINT IF EXISTS runs_project_id_fkey;
ALTER TABLE cybercut.node_runs          DROP CONSTRAINT IF EXISTS node_runs_run_id_fkey;
ALTER TABLE cybercut.node_attempts      DROP CONSTRAINT IF EXISTS node_attempts_node_run_id_fkey;
ALTER TABLE cybercut.artifact_revisions DROP CONSTRAINT IF EXISTS artifact_revisions_project_id_fkey;

-- ══════════════════════════════════════════════════════════════════
-- 3) 复合外键（幂等添加；判重 = conname + conrelid）
-- ══════════════════════════════════════════════════════════════════

-- runs -> projects
SELECT cybercut.ensure_fk_if_absent(
    'fk_runs_project_tenant', 'cybercut.runs'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');

-- node_runs -> runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_node_runs_run_tenant', 'cybercut.node_runs'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id) ON DELETE CASCADE');

-- node_attempts -> node_runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_node_attempts_node_run_tenant', 'cybercut.node_attempts'::regclass,
    'FOREIGN KEY (node_run_id, tenant_id)'
    ' REFERENCES cybercut.node_runs (node_run_id, tenant_id) ON DELETE CASCADE');

-- artifact_revisions -> projects
SELECT cybercut.ensure_fk_if_absent(
    'fk_artifacts_project_tenant', 'cybercut.artifact_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');

-- artifact_revisions -> runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_artifacts_run_tenant', 'cybercut.artifact_revisions'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id)');

-- artifact_revisions -> node_runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_artifacts_node_run_tenant', 'cybercut.artifact_revisions'::regclass,
    'FOREIGN KEY (node_run_id, tenant_id)'
    ' REFERENCES cybercut.node_runs (node_run_id, tenant_id)');

-- artifact_revisions -> node_attempts
SELECT cybercut.ensure_fk_if_absent(
    'fk_artifacts_attempt_tenant', 'cybercut.artifact_revisions'::regclass,
    'FOREIGN KEY (attempt_id, tenant_id)'
    ' REFERENCES cybercut.node_attempts (attempt_id, tenant_id)');

-- artifact_revisions.supersedes_id -> artifact_revisions（同租户版本链；允许 NULL）
SELECT cybercut.ensure_fk_if_absent(
    'fk_artifacts_supersedes_tenant', 'cybercut.artifact_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.artifact_revisions (artifact_revision_id, tenant_id)');

-- approvals -> projects
SELECT cybercut.ensure_fk_if_absent(
    'fk_approvals_project_tenant', 'cybercut.approvals'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');

-- approvals -> runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_approvals_run_tenant', 'cybercut.approvals'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id)');

-- approvals -> node_runs
SELECT cybercut.ensure_fk_if_absent(
    'fk_approvals_node_run_tenant', 'cybercut.approvals'::regclass,
    'FOREIGN KEY (node_run_id, tenant_id)'
    ' REFERENCES cybercut.node_runs (node_run_id, tenant_id)');

-- approvals -> artifact_revisions（允许 NULL）
SELECT cybercut.ensure_fk_if_absent(
    'fk_approvals_artifact_tenant', 'cybercut.approvals'::regclass,
    'FOREIGN KEY (artifact_revision_id, tenant_id)'
    ' REFERENCES cybercut.artifact_revisions (artifact_revision_id, tenant_id)');

-- ══════════════════════════════════════════════════════════════════
-- 4) 清理迁移期辅助函数（保持 cybercut schema 无额外对象）
-- ══════════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS cybercut.ensure_fk_if_absent(text, regclass, text);
