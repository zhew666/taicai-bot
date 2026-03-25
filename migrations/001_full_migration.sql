-- =============================================
-- 百家之眼 代理後台 DB Migration
-- 請在 Supabase SQL Editor 執行
-- =============================================

-- 1. 租戶表
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    line_channel_id TEXT,
    line_channel_secret TEXT,
    line_channel_access_token TEXT,
    branding JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 插入預設租戶
INSERT INTO tenants (slug, name)
VALUES ('default', '百家之眼')
ON CONFLICT (slug) DO NOTHING;

-- 2. members 加 tenant_id
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='members' AND column_name='tenant_id') THEN
        ALTER TABLE members ADD COLUMN tenant_id UUID REFERENCES tenants(id);
    END IF;
END $$;

-- 回填 members tenant_id
UPDATE members SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
WHERE tenant_id IS NULL;

-- 3. agents 加新欄位
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='tenant_id') THEN
        ALTER TABLE agents ADD COLUMN tenant_id UUID REFERENCES tenants(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='path') THEN
        ALTER TABLE agents ADD COLUMN path TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='depth') THEN
        ALTER TABLE agents ADD COLUMN depth INTEGER DEFAULT 1;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='display_name') THEN
        ALTER TABLE agents ADD COLUMN display_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='password_hash') THEN
        ALTER TABLE agents ADD COLUMN password_hash TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='custom_ref_code') THEN
        ALTER TABLE agents ADD COLUMN custom_ref_code TEXT;
    END IF;
END $$;

-- 回填 agents
UPDATE agents SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
WHERE tenant_id IS NULL;

UPDATE agents SET path = '/' || agent_id || '/', depth = 1
WHERE path IS NULL;

-- 4. referral_events 加 tenant_id
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='referral_events' AND column_name='tenant_id') THEN
        ALTER TABLE referral_events ADD COLUMN tenant_id UUID REFERENCES tenants(id);
    END IF;
END $$;

UPDATE referral_events SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
WHERE tenant_id IS NULL;

-- 5. agent_sessions 表
CREATE TABLE IF NOT EXISTS agent_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON agent_sessions(token);

-- 6. custom_referral_codes 表
CREATE TABLE IF NOT EXISTS custom_referral_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    owner_id TEXT NOT NULL,
    tenant_id UUID REFERENCES tenants(id),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 7. agent_actions_log 表
CREATE TABLE IF NOT EXISTS agent_actions_log (
    id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_user_id TEXT,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_agent ON agent_actions_log(agent_id);

-- 8. 索引
CREATE INDEX IF NOT EXISTS idx_members_tenant ON members(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_agent_id);

-- 完成
SELECT 'Migration completed successfully' AS result;
