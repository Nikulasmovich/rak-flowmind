-- CyberCut WB-03 · 0004：创作层版本化产物
--
-- 新增四张**版本化**表（创作层的结构化产物事实源）：
--   creative_brief_revisions / script_revisions /
--   storyboard_revisions    / asset_match_revisions
--
-- 设计要点（与 ADR-0004 一致）：
--   1) 全部表携带 tenant_id，并采用与 WB-01R/01R2 相同的**复合外键**策略：
--        (project_id, tenant_id) -> projects (project_id, tenant_id)
--        (run_id,     tenant_id) -> runs     (run_id,     tenant_id)
--        (supersedes_id, tenant_id) -> 同表 (pk, tenant_id)   —— NULL 时放行
--      不建立任何跨 schema 外键。
--   2) revision_no 在同一 project 内按逻辑产物单调递增；
--      `UNIQUE (project_id, tenant_id, revision_no)` 保证**旧版本不可被覆盖**。
--   3) payload_json 只保存**结构化产物**（经 Pydantic 校验的模型 JSON），
--      不保存原始思维链；思维链摘要单独存于 payload_json.decision_trace。
--   4) 幂等：可重复执行（IF NOT EXISTS / 判重后 ADD CONSTRAINT）。
--   5) 不修改 0001 / 0002 / 0003。

-- ══════════════════════════════════════════════════════════════════
-- 0) 迁移期辅助：仅在 (conname, conrelid) 双命中时才跳过
--    （用后即删，不留在 cybercut schema）
-- ══════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cybercut.wb03_ensure_fk(
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
-- 1) creative_brief_revisions · 创作简报
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.creative_brief_revisions (
    brief_revision_id text        PRIMARY KEY,
    project_id        text        NOT NULL,
    run_id            text        NOT NULL,
    tenant_id         text        NOT NULL,
    revision_no       integer     NOT NULL,
    status            text        NOT NULL DEFAULT 'active',
    payload_json      jsonb       NOT NULL,
    prompt_version    text        NOT NULL,
    model_id          text        NOT NULL,
    created_by        text        NOT NULL DEFAULT 'system',
    supersedes_id     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_brief_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_brief_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_brief_id_tenant
    ON cybercut.creative_brief_revisions (brief_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_brief_project_revision
    ON cybercut.creative_brief_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_brief_project_run
    ON cybercut.creative_brief_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 2) script_revisions · 脚本版本
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.script_revisions (
    script_revision_id text        PRIMARY KEY,
    project_id         text        NOT NULL,
    run_id             text        NOT NULL,
    tenant_id          text        NOT NULL,
    revision_no        integer     NOT NULL,
    status             text        NOT NULL DEFAULT 'active',
    payload_json       jsonb       NOT NULL,
    prompt_version     text        NOT NULL,
    model_id           text        NOT NULL,
    created_by         text        NOT NULL DEFAULT 'system',
    supersedes_id      text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_script_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_script_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_script_id_tenant
    ON cybercut.script_revisions (script_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_script_project_revision
    ON cybercut.script_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_script_project_run
    ON cybercut.script_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 3) storyboard_revisions · 分镜版本
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.storyboard_revisions (
    storyboard_revision_id text        PRIMARY KEY,
    project_id             text        NOT NULL,
    run_id                 text        NOT NULL,
    tenant_id              text        NOT NULL,
    revision_no            integer     NOT NULL,
    status                 text        NOT NULL DEFAULT 'active',
    payload_json           jsonb       NOT NULL,
    prompt_version         text        NOT NULL,
    model_id               text        NOT NULL,
    created_by             text        NOT NULL DEFAULT 'system',
    supersedes_id          text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_storyboard_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_storyboard_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_storyboard_id_tenant
    ON cybercut.storyboard_revisions (storyboard_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_storyboard_project_revision
    ON cybercut.storyboard_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_storyboard_project_run
    ON cybercut.storyboard_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 4) asset_match_revisions · 素材匹配版本
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.asset_match_revisions (
    match_revision_id text        PRIMARY KEY,
    project_id        text        NOT NULL,
    run_id            text        NOT NULL,
    tenant_id         text        NOT NULL,
    revision_no       integer     NOT NULL,
    status            text        NOT NULL DEFAULT 'active',
    payload_json      jsonb       NOT NULL,
    prompt_version    text        NOT NULL,
    model_id          text        NOT NULL,
    created_by        text        NOT NULL DEFAULT 'system',
    supersedes_id     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_match_revision_no CHECK (revision_no >= 1),
    CONSTRAINT ck_match_status
        CHECK (status IN ('draft', 'active', 'superseded', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_match_id_tenant
    ON cybercut.asset_match_revisions (match_revision_id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_match_project_revision
    ON cybercut.asset_match_revisions (project_id, tenant_id, revision_no);
CREATE INDEX IF NOT EXISTS idx_match_project_run
    ON cybercut.asset_match_revisions (project_id, run_id, revision_no DESC);

-- ══════════════════════════════════════════════════════════════════
-- 5) updated_at 触发器（幂等；复用 0001 的 touch_updated_at）
-- ══════════════════════════════════════════════════════════════════
DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'cybercut' AND p.proname = 'touch_updated_at') THEN
        FOREACH t IN ARRAY ARRAY[
            'creative_brief_revisions', 'script_revisions',
            'storyboard_revisions', 'asset_match_revisions'
        ] LOOP
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_updated_at ON cybercut.%I', t, t);
            EXECUTE format(
                'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON cybercut.%I'
                ' FOR EACH ROW EXECUTE FUNCTION cybercut.touch_updated_at()', t, t);
        END LOOP;
    END IF;
END $$;

-- ══════════════════════════════════════════════════════════════════
-- 6) 复合外键（租户一致性由数据库强制）
-- ══════════════════════════════════════════════════════════════════
SELECT cybercut.wb03_ensure_fk(
    'fk_brief_project_tenant', 'cybercut.creative_brief_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_brief_run_tenant', 'cybercut.creative_brief_revisions'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_brief_supersedes_tenant', 'cybercut.creative_brief_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.creative_brief_revisions (brief_revision_id, tenant_id)');

SELECT cybercut.wb03_ensure_fk(
    'fk_script_project_tenant', 'cybercut.script_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_script_run_tenant', 'cybercut.script_revisions'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_script_supersedes_tenant', 'cybercut.script_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.script_revisions (script_revision_id, tenant_id)');

SELECT cybercut.wb03_ensure_fk(
    'fk_storyboard_project_tenant', 'cybercut.storyboard_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_storyboard_run_tenant', 'cybercut.storyboard_revisions'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_storyboard_supersedes_tenant', 'cybercut.storyboard_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.storyboard_revisions (storyboard_revision_id, tenant_id)');

SELECT cybercut.wb03_ensure_fk(
    'fk_match_project_tenant', 'cybercut.asset_match_revisions'::regclass,
    'FOREIGN KEY (project_id, tenant_id)'
    ' REFERENCES cybercut.projects (project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_match_run_tenant', 'cybercut.asset_match_revisions'::regclass,
    'FOREIGN KEY (run_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03_ensure_fk(
    'fk_match_supersedes_tenant', 'cybercut.asset_match_revisions'::regclass,
    'FOREIGN KEY (supersedes_id, tenant_id)'
    ' REFERENCES cybercut.asset_match_revisions (match_revision_id, tenant_id)');

-- ══════════════════════════════════════════════════════════════════
-- 7) 清理迁移期辅助函数
-- ══════════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS cybercut.wb03_ensure_fk(text, regclass, text);
