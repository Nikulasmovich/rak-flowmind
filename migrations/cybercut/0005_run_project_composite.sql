-- CyberCut WB-03R · 0005：修复 Project/Run 组合约束
--
-- 问题：创作层四张版本表原先只用 (run_id, tenant_id) 外键约束 run 归属，
--       无法阻止「把 Project B 的 run_id 写进 Project A 的行」这种组合错误
--       （只要两个 run 属于同一租户，弱外键就会放行）。
--
-- 修复：
--   1) 为 runs 建立可被引用的 (run_id, project_id, tenant_id) 唯一键；
--   2) 四张创作表改用 (run_id, project_id, tenant_id) 复合外键，
--      并移除原 (run_id, tenant_id) 弱外键（新约束严格更强，避免冗余）；
--   3) 直接 SQL 插入「Project A + Project B 的 run」时必须
--      ForeignKeyViolation。
--
-- 幂等：可重复执行（IF NOT EXISTS / 判重后 ADD / DROP CONSTRAINT IF EXISTS）。
-- 不修改 0001 / 0002 / 0003 / 0004。

-- ══════════════════════════════════════════════════════════════════
-- 0) 迁移期辅助：仅在 (conname, conrelid) 双命中时才跳过
-- ══════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION cybercut.wb03r_ensure_fk(
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
-- 1) runs 的可引用唯一键 (run_id, project_id, tenant_id)
-- ══════════════════════════════════════════════════════════════════
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_id_project_tenant
    ON cybercut.runs (run_id, project_id, tenant_id);

-- ══════════════════════════════════════════════════════════════════
-- 2) 移除旧的弱外键 (run_id, tenant_id)
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE cybercut.creative_brief_revisions
    DROP CONSTRAINT IF EXISTS fk_brief_run_tenant;
ALTER TABLE cybercut.script_revisions
    DROP CONSTRAINT IF EXISTS fk_script_run_tenant;
ALTER TABLE cybercut.storyboard_revisions
    DROP CONSTRAINT IF EXISTS fk_storyboard_run_tenant;
ALTER TABLE cybercut.asset_match_revisions
    DROP CONSTRAINT IF EXISTS fk_match_run_tenant;

-- ══════════════════════════════════════════════════════════════════
-- 3) 建立 (run_id, project_id, tenant_id) 复合外键
-- ══════════════════════════════════════════════════════════════════
SELECT cybercut.wb03r_ensure_fk(
    'fk_brief_run_project_tenant', 'cybercut.creative_brief_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03r_ensure_fk(
    'fk_script_run_project_tenant', 'cybercut.script_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03r_ensure_fk(
    'fk_storyboard_run_project_tenant', 'cybercut.storyboard_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');
SELECT cybercut.wb03r_ensure_fk(
    'fk_match_run_project_tenant', 'cybercut.asset_match_revisions'::regclass,
    'FOREIGN KEY (run_id, project_id, tenant_id)'
    ' REFERENCES cybercut.runs (run_id, project_id, tenant_id) ON DELETE CASCADE');

-- ══════════════════════════════════════════════════════════════════
-- 4) 清理迁移期辅助函数
-- ══════════════════════════════════════════════════════════════════
DROP FUNCTION IF EXISTS cybercut.wb03r_ensure_fk(text, regclass, text);
