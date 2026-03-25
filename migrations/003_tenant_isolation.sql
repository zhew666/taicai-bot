-- =============================================
-- 003: 租戶隔離強化
-- 回填 NULL tenant_id、補齊缺少的 tenant_id 欄位、
-- 加 NOT NULL + DEFAULT、唯一約束改為 per-tenant
-- 請在 Supabase SQL Editor 執行
-- =============================================

-- 1. Backfill NULL tenant_id to default tenant
DO $$
DECLARE
  _tenant_id UUID;
BEGIN
  SELECT id INTO _tenant_id FROM tenants WHERE slug = 'default';

  UPDATE members SET tenant_id = _tenant_id WHERE tenant_id IS NULL;
  UPDATE agents SET tenant_id = _tenant_id WHERE tenant_id IS NULL;
  UPDATE referral_events SET tenant_id = _tenant_id WHERE tenant_id IS NULL;
END $$;

-- 2. Add tenant_id to tables that are missing it
-- Add tenant_id to agent_sessions
ALTER TABLE agent_sessions ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
UPDATE agent_sessions SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default') WHERE tenant_id IS NULL;

-- Add tenant_id to agent_actions_log
ALTER TABLE agent_actions_log ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
UPDATE agent_actions_log SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default') WHERE tenant_id IS NULL;

-- Add tenant_id to gw_deposits
ALTER TABLE gw_deposits ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id);
UPDATE gw_deposits SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default') WHERE tenant_id IS NULL;

-- 3. Set NOT NULL with DEFAULT for future inserts
ALTER TABLE members ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE members ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

ALTER TABLE agents ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agents ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

ALTER TABLE referral_events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE referral_events ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

ALTER TABLE agent_sessions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_sessions ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

ALTER TABLE agent_actions_log ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE agent_actions_log ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

ALTER TABLE gw_deposits ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE gw_deposits ALTER COLUMN tenant_id SET DEFAULT (SELECT id FROM tenants WHERE slug = 'default');

-- 4. Drop old unique constraints if they exist, recreate as per-tenant
-- referral_code: unique per tenant
DROP INDEX IF EXISTS members_referral_code_key;
CREATE UNIQUE INDEX IF NOT EXISTS members_referral_code_tenant_uniq ON members(referral_code, tenant_id);

-- custom_ref_code: unique per tenant
DROP INDEX IF EXISTS agents_custom_ref_code_key;
CREATE UNIQUE INDEX IF NOT EXISTS agents_custom_ref_code_tenant_uniq ON agents(custom_ref_code, tenant_id) WHERE custom_ref_code IS NOT NULL;
