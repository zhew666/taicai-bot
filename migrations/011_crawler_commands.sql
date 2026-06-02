-- 爬蟲控制指令佇列：LINE Bot 寫入、VPS daemon 拉取執行
CREATE TABLE IF NOT EXISTS crawler_commands (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     UUID NOT NULL,
  command       TEXT NOT NULL,                  -- mt_restart / dg_restart / all_restart
  issued_by     TEXT,                            -- LINE user_id
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status        TEXT NOT NULL DEFAULT 'pending', -- pending / processing / done / failed
  result        TEXT,                            -- 處理結果文字（成功或錯誤訊息）
  processed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crawler_commands_pending
  ON crawler_commands (status, issued_at)
  WHERE status = 'pending';

COMMENT ON TABLE crawler_commands IS
  'LINE Bot 寫入重啟指令，VPS daemon polling 執行';
