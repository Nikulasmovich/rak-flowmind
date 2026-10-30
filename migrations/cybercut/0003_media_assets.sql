-- CyberCut WB-02 · 0003：素材模型 + Run 输入冻结
--
-- 1) runs 增加 input_json：冻结本次 Run 的输入（如 asset_ids）。
-- 2) 新建 cybercut.media_assets：素材登记/去重/状态机。
--    - (project_id, sha256) 唯一：同项目相同内容只产生一个逻辑 Asset（并发安全）
--    - (project_id, tenant_id) 复合外键：租户一致性由数据库强制
--    - (asset_id, tenant_id) 复合唯一索引：与其它表同构
-- 不修改 0001 / 0002。幂等：可重复执行。

-- ══════════════════════════════════════════════════════════════════
-- 1) runs.input_json
-- ══════════════════════════════════════════════════════════════════
ALTER TABLE cybercut.runs ADD COLUMN IF NOT EXISTS input_json jsonb;

-- ══════════════════════════════════════════════════════════════════
-- 2) media_assets
-- ══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cybercut.media_assets (
    asset_id            text        PRIMARY KEY,
    project_id          text        NOT NULL,
    tenant_id           text        NOT NULL,
    media_kind          text        NOT NULL,
    original_filename   text        NOT NULL,
    source_relative_path text       NOT NULL,
    sha256              text        NOT NULL,
    size_bytes          bigint      NOT NULL,
    status              text        NOT NULL DEFAULT 'ingesting',
    error_code          text,
    error_message       text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_media_assets_kind
        CHECK (media_kind IN ('video', 'image')),
    CONSTRAINT ck_media_assets_status
        CHECK (status IN ('ingesting', 'ready', 'failed')),
    CONSTRAINT ck_media_assets_size CHECK (size_bytes > 0),
    CONSTRAINT ck_media_assets_sha CHECK (length(sha256) = 64)
);

-- 幂等重建 updated_at 触发器（0001 的 touch_updated_at 仍存在时直接复用）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'cybercut' AND p.proname = 'touch_updated_at') THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_media_assets_updated_at ON cybercut.media_assets';
        EXECUTE 'CREATE TRIGGER trg_media_assets_updated_at BEFORE UPDATE'
             || ' ON cybercut.media_assets FOR EACH ROW'
             || ' EXECUTE FUNCTION cybercut.touch_updated_at()';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_assets_project_sha
    ON cybercut.media_assets (project_id, sha256);
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_assets_id_tenant
    ON cybercut.media_assets (asset_id, tenant_id);
CREATE INDEX IF NOT EXISTS idx_media_assets_project_status
    ON cybercut.media_assets (project_id, status);

-- 复合外键：租户一致性由数据库强制
SELECT 1;  -- 占位保持语句顺序稳定
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_media_assets_project_tenant'
           AND conrelid = 'cybercut.media_assets'::regclass
    ) THEN
        ALTER TABLE cybercut.media_assets
            ADD CONSTRAINT fk_media_assets_project_tenant
            FOREIGN KEY (project_id, tenant_id)
            REFERENCES cybercut.projects (project_id, tenant_id)
            ON DELETE CASCADE;
    END IF;
END $$;

-- ══════════════════════════════════════════════════════════════════
-- 3) 幂等重建外键辅助函数沿用 0002 的模式（仅在需要时创建，用后即删）
-- ══════════════════════════════════════════════════════════════════
