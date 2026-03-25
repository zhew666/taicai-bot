-- =============================================
-- 002: 代理碼系統升級
-- 請在 Supabase SQL Editor 執行
-- =============================================

-- 1. agents 加 grant_hours 欄位
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='agents' AND column_name='grant_hours') THEN
        ALTER TABLE agents ADD COLUMN grant_hours INTEGER DEFAULT 6;
    END IF;
END $$;

-- 2. 移除 agent_id 的外鍵約束（如果有的話），允許非 LINE user_id
-- (agents 表的 agent_id 是 text PK，本身不需改)

-- 3. 建立 11 個新代理帳號
DO $$
DECLARE
    _tenant_id UUID;
    _pw_hash TEXT := '$2b$12$PLACEHOLDER'; -- 會在 Python 裡設定真正的密碼
BEGIN
    SELECT id INTO _tenant_id FROM tenants WHERE slug = 'default';

    -- chen972
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-CHEN972', 1, NULL, 'chen972', 31, TRUE, _tenant_id, NULL, 1, 'chen972', 'CHEN972', 24)
    ON CONFLICT (agent_id) DO NOTHING;

    -- evpro
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-EVPRO', 1, NULL, 'evpro', 31, TRUE, _tenant_id, NULL, 1, 'EVPRO', 'EVPRO', 24)
    ON CONFLICT (agent_id) DO NOTHING;

    -- rex99
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-REX99', 1, NULL, 'rex99', 31, TRUE, _tenant_id, NULL, 1, 'REX99', 'REX99', 24)
    ON CONFLICT (agent_id) DO NOTHING;

    -- setheye
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-SETHEYE', 1, NULL, 'setheye', 31, TRUE, _tenant_id, NULL, 1, 'seth-eye', 'SETHEYE', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- leo666
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-LEO666', 1, NULL, '里歐', 31, TRUE, _tenant_id, NULL, 1, '里歐', 'LEO666', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- win999
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-WIN999', 1, NULL, 'bii', 31, TRUE, _tenant_id, NULL, 1, 'bii', 'WIN999', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- ku888
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-KU888', 1, NULL, '酷酷', 31, TRUE, _tenant_id, NULL, 1, '酷酷', 'KU888', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- dnf6688
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-DNF6688', 1, NULL, 'mi', 31, TRUE, _tenant_id, NULL, 1, 'mi', 'DNF6688', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- supersix
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-SUPERSIX', 1, NULL, 'dog', 31, TRUE, _tenant_id, NULL, 1, 'dog', 'SUPERSIX', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- ncc168
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-NCC168', 1, NULL, 'NC', 31, TRUE, _tenant_id, NULL, 1, 'NC', 'NCC168', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- gg18cm
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-GG18CM', 1, NULL, '奶油獅', 31, TRUE, _tenant_id, NULL, 1, '奶油獅', 'GG18CM', 6)
    ON CONFLICT (agent_id) DO NOTHING;

    -- fa8888
    INSERT INTO agents (agent_id, agent_code, level, parent_agent_id, name, max_extend_days, is_active, tenant_id, path, depth, display_name, custom_ref_code, grant_hours)
    VALUES (gen_random_uuid()::text, 'AGENT-FA8888', 1, NULL, 'bird', 31, TRUE, _tenant_id, NULL, 1, 'bird', 'FA8888', 6)
    ON CONFLICT (agent_id) DO NOTHING;
END $$;

-- 4. 回填 path（新建的代理 path 為 NULL）
UPDATE agents SET path = '/' || agent_id || '/' WHERE path IS NULL;

-- 5. 舊代理也補上 grant_hours（原本 12 個 LINE 代理預設 6h）
UPDATE agents SET grant_hours = 6 WHERE grant_hours IS NULL;

-- 完成
SELECT agent_code, custom_ref_code, display_name, grant_hours FROM agents ORDER BY created_at;
