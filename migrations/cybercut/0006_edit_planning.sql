-- CyberCut WB-04 · 0006：编辑规划与模板绑定版本化产物
--
-- 新增两张**版本化**表（与 0004 创作层版本表同构）：
--   edit_plan_revisions     / template_bind_revisions
--
-- 设计要点（与 ADR-0004/0005 一致）：
--   1) 全部表携带 tenant_id，采用与 0004/0005 相同的**复合外键**策略：
--        (project_id, tenant_id) -> projects
--        (run_id, project_id, tenant_id) -> runs   （0005 引入的组合约束）
--        (supersedes_id, tenant_id) -> 同表 (pk, tenant_id)   —— NULL 放行
--   2) revision_no 在同一 project 内单调递增；
--      `UNIQUE (project_id, tenant_id, revision_no)` 保证旧版本不可覆盖；
--   3) payload_json 只保存结构化产物；status 同创作层四态；
--   4) 幂等：可重复执行；不改 0001–0005。

-- ══════════════════════════════════════════════════════════════════
-- 0) 迁移期辅助
-- ══════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cybercut.wb04_ensure_fk(
    p_conname text, p_table regclass, p_definition text
) RETURNS void AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = p_conname AND conrelid = p_table
    ) THEN
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I %s',
                       p_table, p_conname, p_definition);
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ══════════════════════════════════════════════════════════════════
-- 1) edit_plan_revisions · 时间线规划
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.edit_plan_revisions (
    edit_plan_revision_id text        PRIMARY KEY,
    project_id            text        NOT NULL,
    run_id                text        NOT NULL,
    tenant_id             text        NOT NULL,
    revision_no           integer     NOT NULL,
    status                text        NOT NULL DEFAULT 'active',
    payload_json          jsonb       NOT NULL,
    prompt_version        text        NOT NULL,
    model_id              text        NOT NULL,
    created_by            text        NOT NULL DEFAULT 'system',
    supersedes_id         text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_edit_plan_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_edit_plan_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_edit_plan_id_tenant
    ON cybercut.edit_plan_revisions (edit_plan_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_edit_plan_project_revision
    ON cybercut.edit_plan_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_edit_plan_project_run
    ON cybercut.edit_plan_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 2) template_bind_revisions · 模板绑定
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.template_bind_revisions (
    template_bind_revision_id text        PRIMARY KEY,
    project_id                text        NOT NULL,
    run_id                    text        NOT NULL,
    tenant_id                 text        NOT NULL,
    revision_no               integer     NOT NULL,
    status                    text        NOT NULL DEFAULT 'active',
    payload_json              jsonb       NOT NULL,
    prompt_version            text        NOT NULL,
    model_id                  text        NOT NULL,
    created_by                text        NOT NULL DEFAULT 'system',
    supersedes_id             text,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_template_bind_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_template_bind_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_template_bind_id_tenant
    ON cybercut.template_bind_revisions (template_bind_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_template_bind_project_revision
    ON cybercut.template_bind_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_template_bind_project_run
    ON cybercut.template_bind_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 3) updated_at 触发器（复用 0001 的 touch_updated_at）
-- ══════════════════════════════════════════════════════════════════
DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'cybercut' AND p.proname = 'touch_updated_at') THEN
        FOREACH t IN ARRAY ARRAY['edit_plan_revisions', 'template_bind_revisions'] LOOP
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_updated_at ON cybercut.%I', t, t);
            EXECUTE format(
                'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON cybercut.%I'
                ' FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at()', t, t);
        END LOOP;
    END IF;
END $$;

-- ══════════════════════════════════════════════════════════════════
-- 4) 复合外键
-- ══════════════════════════════════════════════════════════════════
SELECT cybercut.wb04_ensure_fk(
    'fk_edit_plan_project_tenant', 'cybercut.edit_plan_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb04_ensure_fk(
    'fk_edit_plan_run_project_tenant', 'cybercut.edit_plan_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb04_ensure_fk(
    'fk_edit_plan_supersedes_tenant', 'cybercut.edit_plan_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.edit_plan_revisions (edit_plan_revision_id, tenant_id)');

SELECT cybercut.wb04_ensure_fk(
    'fk_template_bind_project_tenant', 'cybercut.template_bind_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb04_ensure_fk(
    'fk_template_bind_run_project_tenant', 'cybercut.template_bind_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb04_ensure_fk(
    'fk_template_bind_supersedes_tenant', 'cybercut.template_bind_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.template_bind_revisions (template_bind_revision_id, tenant_id)');

-- ══════════════════════════════════════════════════════════════════
-- 5) 清理迁移期辅助
-- ══════════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS cybercut.wb04_ensure_fk(text, regclass, text);
