-- 兌換碼自動綁定代理：兌換時若用戶尚無推廣碼，自動歸到指定代理線下
-- 注意：agents.agent_id 是 TEXT（同時存 LINE user_id 與 UUID）
ALTER TABLE redemption_codes
  ADD COLUMN IF NOT EXISTS auto_bind_agent_id TEXT NULL REFERENCES agents(agent_id);

COMMENT ON COLUMN redemption_codes.auto_bind_agent_id IS
  '兌換時若該用戶 referred_by 為 NULL 且未用過推廣碼，自動綁到此代理；NULL=不綁';

-- 補：把現有的 EYE425 兌換碼（百家之眼）綁到 ADMIN(v022)
UPDATE redemption_codes
   SET auto_bind_agent_id = '5f78a494-a8db-488a-9789-5a47e4275c02'
 WHERE code = 'EYE425'
   AND tenant_id = 'ce7ba113-9655-4155-b1b3-32ab12e7a004'
   AND auto_bind_agent_id IS NULL;
