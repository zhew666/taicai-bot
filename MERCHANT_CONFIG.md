# 新商戶上線設定表

## 必填環境變數（Render Service）

| 變數 | 說明 | 範例 |
|------|------|------|
| TENANT_ID | Supabase tenant UUID | ce7ba113-... |
| LINE_CHANNEL_SECRET | LINE Bot secret | |
| LINE_CHANNEL_ACCESS_TOKEN | LINE Bot token | |
| SUPABASE_URL | Supabase URL | https://akxpousdcrlrnxblxpxf.supabase.co |
| SUPABASE_KEY | Supabase anon key | |
| ADMIN_USER_ID | 管理員 LINE user ID | |
| ADMIN_REF_CODE | 管理員推薦碼 | REF-XXXX |
| TELEGRAM_BOT_TOKEN | TG Bot token | |
| TELEGRAM_GW_CHAT_IDS | TG 客服群 ID | -5277205417 |

## 品牌客製（選填，有預設值）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| BRAND_NAME | 百家之眼 | 品牌名稱，影響所有用戶面向文案 |
| GW_NAME | 金盈匯 | 儲值平台名稱 |
| REGISTER_URL | gw55.GW1688.NET | 儲值註冊網址 |
| CHAT_URL | （空=顯示尚未開放） | LINE 聊天室連結 |

## 功能客製（選填，有預設值）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| CMD_GUIDE | 仙人指路 | 查全廳最佳 EV 的觸發詞 |
| CMD_AIRDROP | 空投 | 全廳掃描推播的觸發詞 |
| CMD_FOLLOW | 跟隨 | 跟隨桌台的觸發詞 |
| TRIAL_HOURS | 1 | 免費試用時數 |
| GW_TIER_1_AMOUNT | 5000 | 儲值方案一金額 |
| GW_TIER_1_DAYS | 15 | 儲值方案一天數 |
| GW_TIER_2_AMOUNT | 10000 | 儲值方案二金額 |
| GW_TIER_2_DAYS | 31 | 儲值方案二天數 |

## 現有商戶設定參考

### 百家之眼
```
BRAND_NAME=百家之眼
GW_NAME=金盈匯
REGISTER_URL=gw55.GW1688.NET
CHAT_URL=https://line.me/ti/g/ddjjpjznQL
CMD_GUIDE=仙人指路
CMD_AIRDROP=空投
CMD_FOLLOW=跟隨
TRIAL_HOURS=1
GW_TIER_1_AMOUNT=5000
GW_TIER_1_DAYS=15
GW_TIER_2_AMOUNT=10000
GW_TIER_2_DAYS=31
```

### 百家勝率天秤
```
BRAND_NAME=百家勝率天秤
GW_NAME=金盈匯
REGISTER_URL=BC66.gw1688.net
CMD_GUIDE=最佳推薦
CMD_AIRDROP=全局監控
CMD_FOLLOW=跟隨
TRIAL_HOURS=1
GW_TIER_1_AMOUNT=5000
GW_TIER_1_DAYS=15
GW_TIER_2_AMOUNT=10000
GW_TIER_2_DAYS=31
```

## 上線步驟

### 1. Supabase 建租戶
```sql
INSERT INTO tenants (slug, name) VALUES ('商戶代號', '商戶名稱');
-- 記下產生的 UUID 作為 TENANT_ID
```

### 2. Supabase 建管理員
```sql
INSERT INTO agents (
    agent_id, agent_code, display_name, name,
    is_admin, is_active, tenant_id,
    path, depth, level, max_extend_days, grant_hours,
    password_hash
) VALUES (
    '自訂ID', 'ADMIN代碼', '管理員', '管理員',
    true, true, '上面的TENANT_ID',
    '/自訂ID/', 1, 1, 31, 6,
    -- 密碼 hash 用 Python 產生：
    -- python -c "import bcrypt; print(bcrypt.hashpw(b'密碼', bcrypt.gensalt()).decode())"
    '密碼hash'
);
```

### 3. Render 建 Service
- New Web Service → 選 repo `zhew666/taicai-bot` → branch: **main**
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- 填入上方所有環境變數

### 4. LINE 設定
- Webhook URL: `https://xxx.onrender.com/webhook`
- 關閉自動回應訊息
- 開啟 Webhook

### 5. Telegram 設定
```bash
# 建 TG Bot（找 @BotFather）
# 建 TG 客服群，把 Bot 加進去
# 設定 webhook：
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://xxx.onrender.com/telegram"
```

### 6. 商戶自行設定
- LINE 圖文選單（觸發文字對應 CMD_GUIDE / CMD_AIRDROP 等）
- LINE 加入好友歡迎詞

## 注意事項

- **所有商戶共用同一個 GitHub branch (main)**
- push main 會觸發所有商戶的 Render 重新部署
- **不要建新 branch**，差異全靠環境變數
- 不設選填變數就用預設值，不會壞
- 觸發詞支援多組相容（仙人指路 + 最佳推薦 都能觸發同一功能）
- 後台登入用管理員的 agent_code 或 custom_ref_code + 密碼
