# -*- coding: utf-8 -*-
"""
百家之眼 LINE Bot Server
功能：跟隨系統 / 空投系統 / 仙人指路 / 會員系統（推薦碼+儲值）
"""
import os, threading, time, random, string, re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from supabase import create_client

app     = Flask(__name__)
handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
config  = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

# ── Web Dashboard ────────────────────────────────────────
from web import create_dashboard_blueprint
app.register_blueprint(create_dashboard_blueprint(), url_prefix="/dashboard")
_sb_url = os.environ["SUPABASE_URL"]
_sb_key = os.environ["SUPABASE_KEY"]
_thread_local = threading.local()

def sb():
    """每個 thread 使用獨立的 Supabase client，避免多 thread 共用同一 httpx socket"""
    if not hasattr(_thread_local, 'client'):
        _thread_local.client = create_client(_sb_url, _sb_key)
    return _thread_local.client

import httpx as _httpx

REGISTER_URL    = os.environ.get("REGISTER_URL", "v088.gw1688.net")
TENANT_ID       = os.environ.get("TENANT_ID", "")
ADMIN_USER_ID   = os.environ.get("ADMIN_USER_ID", "")
ADMIN_REF_CODE  = os.environ.get("ADMIN_REF_CODE", "")
TG_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")
TG_GW_CHAT_IDS  = [x.strip() for x in os.environ.get("TELEGRAM_GW_CHAT_IDS", "").split(",") if x.strip()]
BRAND_NAME      = os.environ.get("BRAND_NAME", "百家之眼")
GW_NAME         = os.environ.get("GW_NAME", "金盈匯")
CHAT_URL        = os.environ.get("CHAT_URL", "")
WARN_MINUTES   = 15
CMD_GUIDE      = os.environ.get("CMD_GUIDE", "仙人指路")
CMD_AIRDROP    = os.environ.get("CMD_AIRDROP", "空投")
CMD_FOLLOW     = os.environ.get("CMD_FOLLOW", "跟隨")
DEFAULT_PLATFORM = os.environ.get("DEFAULT_PLATFORM", "MT")
GW_AMOUNT      = int(os.environ.get("GW_AMOUNT", "3000"))
GW_HOURS       = int(os.environ.get("GW_HOURS", "48"))
GW_TIERS_TEXT  = f"💡 儲值 {GW_AMOUNT:,} 點 → {GW_HOURS} 小時"
ALL_TABLES_MT = [f"BAG{i:02d}" for i in range(1, 14)] + ["BAG03A", "TEST01"]

# DG 標準桌映射：01~07 → DGR1~DGR7
DG_STD_MAP  = {f"DGR{i}": f"{i:02d}" for i in range(1, 8)}
DG_STD_REV  = {v: k for k, v in DG_STD_MAP.items()}
# DG 性感桌：動態映射 S01~S07（每 60 秒刷新）
_dg_sexy_cache = {"fwd": {}, "rev": {}, "ts": 0}

def _refresh_dg_sexy():
    now = time.time()
    if now - _dg_sexy_cache["ts"] < 60:
        return
    try:
        rows = sb().table("live_tables").select("table_id").eq("platform", "DG").like("table_id", "DGS%").execute().data
        sids = sorted([r["table_id"] for r in rows]) if rows else []
        _dg_sexy_cache["fwd"] = {sid: f"S{i+1:02d}" for i, sid in enumerate(sids)}
        _dg_sexy_cache["rev"] = {v: k for k, v in _dg_sexy_cache["fwd"].items()}
        _dg_sexy_cache["ts"] = now
    except Exception as e:
        print(f"[DG Sexy] 刷新失敗: {e}", flush=True)
EV_FIELDS    = ["ev_banker", "ev_player", "ev_super6", "ev_pair_p", "ev_pair_b", "ev_tie"]
EV_LABELS    = {"ev_banker": "莊", "ev_player": "閒", "ev_tie": "和",
                "ev_super6": "超六", "ev_pair_p": "閒對", "ev_pair_b": "莊對"}

# ── 記憶體狀態 ─────────────────────────────────────────────
following    = {}   # user_id → {table_id, last_shoe, last_hand}
airdrop      = {}   # user_id → {expire_at, notified: {table_id: last_hand}}
follow_lock  = threading.Lock()
airdrop_lock = threading.Lock()
_cooldown    = {}   # user_id → last_cmd_time
_pending_extend = {}  # agent_user_id → {target_ref, days, expire_ts}
_pending_bind   = {}  # user_id → {"state": "awaiting_account", "expire_ts": ...}
_pending_follow = {}  # user_id → {"expire_ts": ...}  二段式跟隨
_poll_stats  = {"count": 0, "airdrop_triggers": 0, "last_trigger": None}  # poll 健康監控
# ── 數據新鮮度監控（Render 端獨立告警）──
WATCHDOG_TG_TOKEN   = os.environ.get("WATCHDOG_TG_TOKEN", "")
ADMIN_TG_CHAT_ID    = os.environ.get("ADMIN_TG_CHAT_ID", "")
_stale_alert_active = False
_last_stale_check   = 0
STALE_THRESHOLD     = 180   # 數據超過 180 秒算過期
STALE_CHECK_INTERVAL = 60   # 每 60 秒檢查一次

def tg_send(chat_id: str, text: str):
    """發送 Telegram 私訊"""
    if not TG_BOT_TOKEN:
        print("[TG] BOT_TOKEN 未設定，跳過發送", flush=True); return
    try:
        r = _httpx.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10)
        if r.status_code != 200:
            print(f"[TG] 發送失敗 chat_id={chat_id}: {r.text}", flush=True)
    except Exception as e:
        print(f"[TG] 發送異常: {e}", flush=True)

def tg_notify_gw(text: str):
    """通知所有 GW 客服（Telegram）"""
    for chat_id in TG_GW_CHAT_IDS:
        tg_send(chat_id, text)

def _find_top_agent(user_id, depth=0):
    """往上追溯 referred_by，直到找到代理"""
    if depth > 10:
        return f"{BRAND_NAME}官方"
    # 查是否為代理
    try:
        r = sb().table("agents").select("display_name,custom_ref_code").eq("agent_id", user_id).eq("tenant_id", TENANT_ID).limit(1).execute()
        if r.data:
            a = r.data[0]
            return a.get("display_name") or a.get("custom_ref_code") or f"{BRAND_NAME}官方"
    except Exception:
        pass
    # 不是代理，查這個人的上線
    try:
        r2 = sb().table("members").select("referred_by").eq("user_id", user_id).eq("tenant_id", TENANT_ID).limit(1).execute()
        if r2.data and r2.data[0].get("referred_by"):
            return _find_top_agent(r2.data[0]["referred_by"], depth + 1)
    except Exception:
        pass
    return f"{BRAND_NAME}官方"

def get_agent_name(user_id):
    """取得會員的上級代理名稱"""
    member = sb().table("members").select("referred_by").eq("user_id", user_id).eq("tenant_id", TENANT_ID).limit(1).execute()
    if not member.data or not member.data[0].get("referred_by"):
        return f"{BRAND_NAME}官方"
    return _find_top_agent(member.data[0]["referred_by"])

# ── system_config 快取（30 秒刷新一次，減少 DB 查詢）──
_config_cache = {"data": {}, "ts": 0}
_CONFIG_TTL = 30  # 秒

def _get_config(key: str, default: str = "") -> str:
    now = time.time()
    if now - _config_cache["ts"] > _CONFIG_TTL:
        try:
            rows = sb().table("system_config").select("key,value").execute().data or []
            _config_cache["data"] = {r["key"]: r["value"] for r in rows}
            _config_cache["ts"] = now
        except Exception as e:
            print(f"[Config] 快取刷新失敗: {e}", flush=True)
    return _config_cache["data"].get(key, default)

def _set_config(key: str, value: str):
    sb().table("system_config").upsert({"key": key, "value": value}).execute()
    _config_cache["data"][key] = value
    _config_cache["ts"] = time.time()

def is_platform_enabled(platform: str) -> bool:
    return _get_config(f"{platform.lower()}_enabled", "true") != "false"

def set_platform_enabled(platform: str, on: bool):
    _set_config(f"{platform.lower()}_enabled", "true" if on else "false")

def is_maintenance() -> bool:
    return _get_config("maintenance_mode", "false") == "true"

def set_maintenance(on: bool):
    _set_config("maintenance_mode", "true" if on else "false")

def is_test_mode() -> bool:
    return _get_config("test_mode", "false") == "true"

def set_test_mode(on: bool):
    _set_config("test_mode", "true" if on else "false")
COOLDOWN_SEC = 5

def check_cooldown(user_id: str) -> bool:
    """True = 可以執行；False = 冷卻中"""
    now = time.time()
    if now - _cooldown.get(user_id, 0) < COOLDOWN_SEC:
        return False
    _cooldown[user_id] = now
    return True
_last_trial_check = 0
_poll_started = False
_poll_start_lock = threading.Lock()

@app.before_request
def ensure_poll_running():
    global _poll_started
    if not _poll_started:
        with _poll_start_lock:
            if not _poll_started:
                t = threading.Thread(target=poll_loop, daemon=True)
                t.start()
                _poll_started = True
                print(f"[app] poll_loop started pid={os.getpid()}", flush=True)

# ── 工具函式 ──────────────────────────────────────────────
def tnum(table_id: str) -> str:
    if table_id.startswith("DGR"):
        return DG_STD_MAP.get(table_id, table_id)  # DGR1→01
    if table_id.startswith("DGS"):
        _refresh_dg_sexy()
        return _dg_sexy_cache["fwd"].get(table_id, table_id)  # DGS348→S01
    return table_id.replace("BAG", "").lstrip("0") or table_id

_CN_NUM = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,
            "八":8,"九":9,"十":10,"十一":11,"十二":12,"十三":13}

def normalize_table(text: str, platform: str = "MT"):
    t = text.strip()
    # 去掉常見多餘字
    for c in ("廳","號","桌","台","第","厅"):
        t = t.replace(c, "")
    t = t.strip()
    # DG 模式
    if platform == "DG":
        u = t.upper()
        # 直接輸入完整 ID：DGR1, DGS348
        if u.startswith("DG"):
            return u
        # 輸入 01~07 → DGR1~DGR7
        if u.isdigit():
            n = u.zfill(2)
            if n in DG_STD_REV:
                return DG_STD_REV[n]
            return None
        # 輸入 S01~S07 → 查性感桌映射
        if u.startswith("S") and u[1:].isdigit():
            sn = u[0] + u[1:].zfill(2)  # S1→S01
            _refresh_dg_sexy()
            if sn in _dg_sexy_cache["rev"]:
                return _dg_sexy_cache["rev"][sn]
            return None
        # 輸入 R1, R2 等
        if u.startswith("R"):
            return f"DG{u}"
        return None
    # MT 模式
    if t.upper() in ("3A", "03A"):
        return "BAG03A"
    if t.upper() in ("TEST01", "TEST1", "TEST"):
        return "TEST01"
    if t.isdigit():
        n = int(t)
        if 1 <= n <= 13:
            return f"BAG{n:02d}"
        return None
    # 中文數字
    if t in _CN_NUM:
        return f"BAG{_CN_NUM[t]:02d}"
    t = t.upper()
    if t.startswith("BAG"):
        return t
    return None

def push_text(user_id: str, text: str):
    for attempt in range(3):
        try:
            with ApiClient(config) as api:
                MessagingApi(api).push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=text)]))
            return
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                print(f"[push_text] 429 限速，重試 {attempt+1}", flush=True)
                continue
            if attempt == 2:
                print(f"[push_text] 失敗 3 次放棄: {e}", flush=True)

def reply_text(token: str, text: str):
    for attempt in range(3):
        try:
            with ApiClient(config) as api:
                MessagingApi(api).reply_message(
                    ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)]))
            return
        except Exception as e:
            if attempt == 2:
                print(f"[reply_text] 失敗 3 次放棄: {e}", flush=True)

def get_user_platform(member: dict) -> str:
    """取得用戶偏好平台"""
    return member.get("game", DEFAULT_PLATFORM) or DEFAULT_PLATFORM

def _sexy_enabled() -> bool:
    return _get_config("sexy_enabled", "false") == "true"

def _hide_sexy(table_id: str, admin: bool = False) -> bool:
    """是否隱藏該桌（管理員永遠可見）"""
    if not table_id.startswith("DGS"):
        return False
    if admin:
        return False
    return not _sexy_enabled()

def get_platform_tables(platform: str, admin: bool = False) -> list:
    """取得該平台的有效桌號列表（MT 固定，DG 從 DB 動態取）"""
    if platform == "DG":
        try:
            rows = sb().table("live_tables").select("table_id").eq("platform", "DG").execute().data
            return [r["table_id"] for r in rows if not _hide_sexy(r["table_id"], admin)] if rows else []
        except Exception:
            return []
    return ALL_TABLES_MT

def get_all_latest_hands(platform: str = None) -> dict:
    """從 live_tables 取得每桌最新狀態，可選平台過濾"""
    q = sb().table("live_tables").select("*")
    if platform:
        q = q.eq("platform", platform)
    rows = q.execute().data
    return {row["table_id"]: row for row in rows}

def _card_point(card_str):
    """牌面字串 → 百家樂點數（♠A→1, ♥10→0, ♦K→0）"""
    if not card_str or card_str == "-":
        return 0
    r = card_str[1:]  # 去掉花色符號
    if r == "A": return 1
    if r in ("10", "J", "Q", "K"): return 0
    try: return int(r)
    except: return 0

def _hand_result(row: dict) -> str:
    """計算上局結果 → 🔴莊贏 / 🔵閒贏 / 🟢和局"""
    p_pts = sum(_card_point(row.get(f"p{i}")) for i in range(1, 4)) % 10
    b_pts = sum(_card_point(row.get(f"b{i}")) for i in range(1, 4)) % 10
    if b_pts > p_pts:
        return f"🔴莊 {b_pts}:{p_pts}"
    elif p_pts > b_pts:
        return f"🔵閒 {p_pts}:{b_pts}"
    else:
        return f"🟢和 {p_pts}:{b_pts}"

def format_hand(row: dict) -> str:
    """回傳單則訊息：EV在前（置頂通知可見莊閒），牌面結果在後"""
    p = " ".join(str(row.get(f"p{i}","-")) for i in range(1,4) if row.get(f"p{i}"))
    b = " ".join(str(row.get(f"b{i}","-")) for i in range(1,4) if row.get(f"b{i}"))
    tid    = tnum(row['table_id'])
    plat   = row.get('platform', 'MT')
    shoe   = row['shoe']
    hand   = row['hand_num']
    def ev_str(val):
        if val is None: return "N/A"
        star = " ✅" if val > 0 else ""
        return f"{val:+.4f}{star}"

    pair_ev = max(v for v in [row.get("ev_pair_p"), row.get("ev_pair_b")] if v is not None) \
              if any(row.get(f) is not None for f in ["ev_pair_p","ev_pair_b"]) else None

    dealer = row.get("dealer") or ""
    dealer_str = f"｜荷官：{dealer}" if dealer and dealer != "未知" else ""

    next_hand = hand + 1
    plat_tag = f"[{plat}] "
    result = _hand_result(row)

    # 莊閒 EV 比較 → 推薦標記放前面
    ev_b = row.get("ev_banker") or 0
    ev_p = row.get("ev_player") or 0
    b_prefix = "🔴" if ev_b > ev_p else "  "
    p_prefix = "🔵" if ev_p > ev_b else "  "

    return "\n".join([
        f"{plat_tag}第{tid}廳{dealer_str} | 第{next_hand}局預期收益",
        f"  {b_prefix}莊：{ev_str(row.get('ev_banker'))}  {p_prefix}閒：{ev_str(row.get('ev_player'))}",
        f"  超六：{ev_str(row.get('ev_super6'))}  對子：{ev_str(pair_ev)}",
        f"  和：{ev_str(row.get('ev_tie'))}",
        f"──────────",
        f"靴{shoe} | 第{hand}局結果 {result}",
        f"閒牌：{p}",
        f"莊牌：{b}",
    ])

# ── 會員系統 ──────────────────────────────────────────────
def gen_referral_code() -> str:
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "REF-" + "".join(random.choices(chars, k=4))
        if not sb().table("members").select("user_id").eq("referral_code", code).eq("tenant_id", TENANT_ID).execute().data:
            return code
    return "REF-" + "".join(random.choices(chars, k=6))

def get_or_create_member(user_id: str) -> dict:
    r = sb().table("members").select("*").eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
    if r.data:
        return r.data[0]
    now = datetime.now(timezone.utc)
    member = {
        "user_id":       user_id,
        "tenant_id":     TENANT_ID,
        "trial_start":   None,
        "expire_at":     None,
        "is_member":     False,
        "referral_code": gen_referral_code(),
        "referred_by":   None,
        "warned_15min":  False,
        "game":          DEFAULT_PLATFORM,
    }
    sb().table("members").insert(member).execute()
    return member

def is_allowed(member: dict) -> bool:
    if member.get("is_member"):
        return True
    exp = member.get("expire_at")
    if not exp:
        return False
    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) < exp_dt

def has_referral(member: dict) -> bool:
    """是否已輸入過推薦碼，或是既有用戶"""
    if member.get("referred_by"):
        return True
    # 既有用戶（有任何使用紀錄）不需要推薦碼
    if member.get("trial_start") or member.get("expire_at") or member.get("is_member") or member.get("gw_status"):
        return True
    return False

def no_code_reply(token: str):
    reply_text(token, "請先輸入推薦碼才能使用")

def expired_reply(token: str, member: dict):
    code = member.get("referral_code", "N/A")
    reply_text(token,
        f"⏰ 使用期限已到期\n\n"
        f"想繼續使用嗎？\n"
        f"回覆「繼續」即可了解儲值方案\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📋 你的推薦碼：{code}\n"
        f"推薦好友：雙方各 +6 小時")

def cmd_continue_info(user_id, token, member):
    """用戶回覆「繼續」，推送 GW 儲值引導"""
    has_account = member.get("gw_account")
    if has_account:
        reply_text(token,
            f"前往{GW_NAME}儲值點數\n"
            f"👉 {REGISTER_URL}\n\n"
            f"💰 點數可直接用來玩{GW_NAME}平台上的遊戲\n"
            f"儲值後即可同時開通使用權\n\n"
                        f"{GW_TIERS_TEXT}\n\n"
            f"儲值完成後回來輸入「確認儲值」")
    else:
        reply_text(token,
            f"前往{GW_NAME}註冊並儲值點數\n"
            f"👉 {REGISTER_URL}\n\n"
            f"💰 點數可直接用來玩{GW_NAME}平台上的遊戲\n"
            f"儲值後即可同時開通使用權\n\n"
                        f"{GW_TIERS_TEXT}\n\n"
            f"註冊完成後，輸入「綁定帳號」綁定\n"
            f"儲值完成後輸入「確認儲值」")

def cmd_confirm_deposit(user_id, token, member):
    """用戶輸入「確認儲值」，通知 GW 客服"""
    account = member.get("gw_account")
    if not account:
        reply_text(token,
            f"尚未綁定{GW_NAME}帳號\n\n"
            "請先輸入「綁定帳號」綁定後再確認儲值")
        return
    status = member.get("gw_status", "none")
    if status == "pending":
        reply_text(token,
            f"📋 帳號：{account}\n"
            f"上次儲值仍在審核中 ⏳\n"
            f"客服確認後會自動延長，請耐心等候")
        return
    # verified / rejected / none 都可以發起新的確認
    sb().table("members").update({"gw_status": "pending"}).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
    reply_text(token,
        f"📋 帳號：{account}\n"
        f"已通知客服確認您的最新儲值\n"
        f"確認後將自動延長使用期限\n\n"
                f"{GW_TIERS_TEXT}")
    # 通知 GW 客服（Telegram）
    agent_name = get_agent_name(user_id)
    tg_notify_gw(
        f"📋 儲值待確認\n"
        f"━━━━━━━━━━━━━━\n"
        f"{GW_NAME}帳號：{account}\n"
        f"上級代理：{agent_name}\n\n"
        f"請確認儲值後回覆：\n"
        f"  確認 {account} <金額>\n\n"
        f"未通過請回覆：\n"
        f"  未通過 {account}")

# ── 指令處理 ──────────────────────────────────────────────
def cmd_follow(user_id, token, text, member):
    if not has_referral(member):
        no_code_reply(token); return
    if not is_allowed(member):
        expired_reply(token, member); return
    plat = get_user_platform(member)
    valid_tables = get_platform_tables(plat, admin=is_admin(user_id))
    tid = normalize_table(text[2:].strip(), plat)

    # 沒帶廳號：如果正在跟隨就關閉，否則顯示引導
    if not tid or (valid_tables and tid not in valid_tables):
        with follow_lock:
            if user_id in following:
                old_tid = following.pop(user_id)["table_id"]
                reply_text(token, f"👁 已停止跟隨{tnum(old_tid)}"); return
        _pending_follow[user_id] = {"expire_ts": time.time() + 30}
        if plat == "DG":
            reply_text(token,
                "👁 請輸入桌號：01~07\n"
                f"📡 DG（{len(valid_tables)} 桌在線）")
        else:
            reply_text(token,
                "👁 請輸入廳號：1~13\n"
                "📡 MT 13 廳")
        return

    # 已在跟隨同一廳 → 關閉
    with follow_lock:
        if user_id in following and following[user_id]["table_id"] == tid:
            following.pop(user_id)
            reply_text(token, f"👁 已停止跟隨第{tnum(tid)}廳"); return

    with follow_lock:
        following[user_id] = {"table_id": tid, "last_shoe": None, "last_hand": 0,
                              "started_at": time.time()}
    reply_text(token, f"⏳ 正在連線第{tnum(tid)}廳...")

def cmd_airdrop(user_id, token, text, member):
    if not has_referral(member):
        no_code_reply(token); return
    if not is_allowed(member):
        expired_reply(token, member); return

    # 開關機制：已開啟就關閉
    with airdrop_lock:
        if user_id in airdrop:
            airdrop.pop(user_id)
            reply_text(token, f"📡 {CMD_AIRDROP}已關閉"); return

    m = re.search(r'(\d+)', text)
    hours = max(1, min(3, int(m.group(1)))) if m else 1
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    with airdrop_lock:
        airdrop[user_id] = {"expire_at": exp, "notified": {}, "push_count": 0}

    # 即時狀態快照
    plat = get_user_platform(member)
    exp_tw = exp.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
    try:
        q = sb().table("live_tables").select("table_id,platform," + ",".join(EV_FIELDS))
        q = q.eq("platform", plat)
        fresh = q.execute().data or []
    except Exception:
        fresh = []
    real = [r for r in fresh if r["table_id"] != "TEST01"]
    active = len(real)
    pos = sum(1 for r in real if any(r.get(f) and r[f] > 0 for f in EV_FIELDS))

    lines = [
        f"📡 全局掃描已開啟",
        "━━━━━━━━━━━━━━",
        "",
        f"監控廳數：{active} 廳",
        f"結束時間：{exp_tw}",
    ]
    reply_text(token, "\n".join(lines))

def cmd_stop(user_id, token):
    removed = []
    with follow_lock:
        if following.pop(user_id, None): removed.append("跟隨")
    with airdrop_lock:
        if airdrop.pop(user_id, None): removed.append("空投")
    reply_text(token, f"已停止：{'、'.join(removed)}" if removed else "目前沒有進行中的監控")

def cmd_guide(user_id, token, member):
    if not has_referral(member):
        no_code_reply(token); return
    if not is_allowed(member):
        expired_reply(token, member); return
    plat = get_user_platform(member)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
    try:
        fresh_rows = [r for r in sb().table("live_tables").select("*").eq("platform", plat).gte("updated_at", cutoff).execute().data
                      if not _hide_sexy(r["table_id"], is_admin(user_id))]
    except Exception:
        fresh_rows = []
    if not fresh_rows:
        reply_text(token, "目前無即時數據，請稍後再試"); return
    best_row, best_field, best_val = None, None, -999
    for row in fresh_rows:
        for f in EV_FIELDS:
            v = row.get(f)
            if v is not None and v > best_val:
                best_val, best_field, best_row = v, f, row
    if not best_row:
        reply_text(token, "目前無即時數據，請稍後再試"); return
    label = EV_LABELS.get(best_field, best_field)
    t     = tnum(best_row["table_id"])
    hand  = best_row["hand_num"]
    dealer = best_row.get("dealer") or ""
    d_str = f" 荷官：{dealer}" if dealer and dealer != "未知" else ""
    next_hand = hand + 1
    # 判斷莊/閒推薦標記
    ev_b = best_row.get("ev_banker") or 0
    ev_p = best_row.get("ev_player") or 0
    if ev_b > ev_p:
        rec = "🔴莊"
    elif ev_p > ev_b:
        rec = "🔵閒"
    else:
        rec = None
    plat_tag = f"[{plat}] "
    rec_str = f"  {rec}" if rec else ""
    if best_val > 0:
        msg = (f"🧙 {CMD_GUIDE} {plat_tag}第{t}廳{rec_str}\n"
               f"第{next_hand}局 {label} 預期收益={best_val:+.4f} ✅{d_str}\n"
               f"正預期收益，可考慮出手")
    else:
        msg = (f"🧙 {CMD_GUIDE} {plat_tag}第{t}廳{rec_str}\n"
               f"第{next_hand}局{d_str}\n"
               f"目前最佳選項：{label} 預期收益：{best_val:+.4f}\n"
               f"靴牌進行中，持續監控")
    reply_text(token, msg)

def cmd_my_code(user_id, token, member):
    code = member.get("referral_code", "N/A")
    exp  = member.get("expire_at", "")
    if exp:
        exp_dt  = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        exp_str = exp_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    elif member.get("is_member"):
        exp_str = "永久"
    else:
        exp_str = "尚未使用（請輸入推薦碼）"
    reply_text(token,
        f"📋 你的推薦碼：{code}\n"
        f"使用期限：{exp_str}\n\n"
        f"推薦好友：雙方各 +6 小時\n"
        f"好友首次儲值：你 +48 小時")

def get_member_type(user_id: str, member: dict) -> str:
    """回傳會員類型標籤"""
    if is_admin(user_id):
        return "👑 管理員"
    agent = get_agent(user_id)
    if agent:
        return "💼 代理"
    is_paid = member.get("gw_status") == "verified" or member.get("is_member") is True
    if is_paid and not member.get("expire_at"):
        return "✅ 正式帳號"
    exp = member.get("expire_at", "")
    if exp:
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < exp_dt:
            return "✅ 正式帳號" if is_paid else "⏳ 試用會員"
        return "⏰ 使用期限已到期"
    if not member.get("referred_by"):
        return "🆕 請先輸入推薦碼"
    return "⏰ 使用期限已到期"

def get_expire_str(member: dict) -> str:
    """回傳到期時間描述"""
    if member.get("is_member"):
        return "永久使用"
    exp = member.get("expire_at", "")
    if not exp:
        if not member.get("referred_by"):
            return "輸入推薦碼開始使用"
        return "已結束"
    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    exp_str = exp_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    remaining = exp_dt - datetime.now(timezone.utc)
    mins = int(remaining.total_seconds() / 60)
    if mins <= 0:
        return "已結束"
    days = mins // 1440
    hours = (mins % 1440) // 60
    if days >= 7:
        return f"至 {exp_str}（約 {days} 天）"
    elif days >= 1:
        return f"剩餘約 {days} 天（{exp_str}）"
    elif hours >= 1:
        return f"剩餘約 {hours} 小時（{exp_str}）"
    return f"剩餘約 {mins} 分鐘（{exp_str}）"

def cmd_intro(user_id, token, member):
    code = member.get("referral_code", "N/A")
    mtype = get_member_type(user_id, member)
    exp_info = get_expire_str(member)
    plat = get_user_platform(member)
    plat_str = "DG" if plat == "DG" else "MT 13 廳"
    has_account = member.get("gw_account")
    is_paid = member.get("gw_status") == "verified" or member.get("is_member") is True
    is_expired = False
    exp = member.get("expire_at")
    if exp:
        try:
            is_expired = datetime.now(timezone.utc) >= datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except Exception:
            is_expired = True

    # 狀態引導
    if not member.get("referred_by"):
        # 新用戶：沒有推薦碼
        guide = ("━━━━━━━━━━━━━━\n"
                 "👋 請先輸入推薦碼才能使用\n"
                 "━━━━━━━━━━━━━━")
    elif not has_account:
        # 有推薦碼但沒綁帳號
        guide = (f"━━━━━━━━━━━━━━\n"
                 f"💰 升級正式會員（3 步驟）\n"
                 f"1️⃣ 前往{GW_NAME}註冊並儲值\n"
                 f"   👉 {REGISTER_URL}\n"
                 f"   {GW_TIERS_TEXT}\n"
                 f"2️⃣ 回來輸入「綁定帳號」\n"
                 f"3️⃣ 輸入「確認儲值」通知客服\n"
                 f"━━━━━━━━━━━━━━")
    elif not is_paid:
        # 有帳號但沒儲值過
        guide = (f"━━━━━━━━━━━━━━\n"
                 f"💰 下一步：前往{GW_NAME}儲值\n"
                 f"👉 {REGISTER_URL}\n"
                 f"{GW_TIERS_TEXT}\n\n"
                 f"儲值完成後輸入「確認儲值」\n"
                 f"客服確認後自動開通\n"
                 f"━━━━━━━━━━━━━━")
    elif is_expired:
        # 正式會員已到期
        guide = (f"━━━━━━━━━━━━━━\n"
                 f"💰 儲值延長使用期限\n"
                 f"👉 {REGISTER_URL}\n"
                 f"{GW_TIERS_TEXT}\n\n"
                 f"儲值完成後輸入「確認儲值」即可延長\n"
                 f"━━━━━━━━━━━━━━")
    else:
        # 正式會員使用中
        guide = (f"━━━━━━━━━━━━━━\n"
                 f"💰 到期前可提前儲值續費\n"
                 f"👉 {REGISTER_URL}\n"
                 f"{GW_TIERS_TEXT}\n"
                 f"━━━━━━━━━━━━━━")

    reply_text(token,
        f"📋 帳號狀態：\n"
        f"身份：{mtype}\n"
        f"期限：{exp_info}\n"
        f"📡 目前場館：{plat_str}\n\n"
        f"{guide}\n\n"
        f"🔗 你的專屬推薦碼：{code}\n"
        f"・推薦好友 → 雙方各 +6 小時\n"
        f"・好友首次儲值 → 你 +48 小時\n\n"
        f"💡 輸入「指令」查詢更多功能\n"
        f"💡 輸入「切換」可切換 MT/DG")

def get_agent(user_id: str):
    """查詢 agents 表，回傳 agent row 或 None"""
    try:
        r = sb().table("agents").select("*").eq("agent_id", user_id).eq("is_active", True).eq("tenant_id", TENANT_ID).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

_admin_cache = {"uids": set(), "ts": 0}
_ADMIN_CACHE_TTL = 60  # 秒

def is_admin(user_id: str) -> bool:
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        return True
    if ADMIN_REF_CODE:
        now = time.time()
        if now - _admin_cache["ts"] > _ADMIN_CACHE_TTL:
            try:
                codes = [c.strip().upper() for c in ADMIN_REF_CODE.split(",") if c.strip()]
                r = sb().table("members").select("user_id").in_("referral_code", codes).eq("tenant_id", TENANT_ID).execute()
                _admin_cache["uids"] = {row["user_id"] for row in (r.data or [])}
                _admin_cache["ts"] = now
            except Exception:
                pass
        if user_id in _admin_cache["uids"]:
            return True
    return False

def cmd_admin_activate(user_id, token, text):
    """開通 REF-XXXX 或 開通 <user_id>"""
    if not is_admin(user_id):
        reply_text(token, "❌ 無權限"); return
    target = text[2:].strip()
    if not target:
        reply_text(token, "格式：開通 REF-XXXX 或 開通 <user_id>"); return

    # 查目標成員
    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).eq("tenant_id", TENANT_ID).execute()

    if not r.data:
        reply_text(token, f"找不到成員：{target}"); return

    target_member = r.data[0]
    target_uid    = target_member["user_id"]

    if target_member.get("is_member"):
        reply_text(token, f"⚠️ {target_uid} 已是正式會員"); return

    # 開通正式會員
    sb().table("members").update({"is_member": True, "expire_at": None}).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()
    # 記錄轉換事件
    try:
        sb().table("referral_events").insert({
            "tenant_id": TENANT_ID,
            "referee_id": target_uid,
            "referrer_id": target_member.get("referred_by"),
            "code_used": target_member.get("referral_code", ""),
            "code_type": "activation",
            "event_type": "converted_paid",
            "bonus_given_hours": 0,
        }).execute()
    except Exception as e:
        print(f"[Admin] 記錄轉換事件失敗: {e}", flush=True)
    reply_text(token, f"✅ 已開通正式會員：{target_uid}")

    # 通知被開通者
    try:
        push_text(target_uid, "🎉 恭喜！你的帳號已升級為正式會員，可永久使用所有功能！")
    except Exception as e:
        print(f"[Admin] 通知被開通者失敗: {e}", flush=True)

    # 推薦人 +48h
    referrer_uid = target_member.get("referred_by")
    if referrer_uid:
        try:
            rr = sb().table("members").select("expire_at").eq("user_id", referrer_uid).eq("tenant_id", TENANT_ID).execute()
            if rr.data:
                exp = rr.data[0].get("expire_at")
                base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
                new_exp = (base + timedelta(hours=48)).isoformat()
                sb().table("members").update({"expire_at": new_exp}).eq("user_id", referrer_uid).eq("tenant_id", TENANT_ID).execute()
                # 記錄推薦人獎勵事件
                try:
                    sb().table("referral_events").insert({
                        "tenant_id": TENANT_ID,
                        "referrer_id": referrer_uid,
                        "referee_id": target_uid,
                        "code_used": target_member.get("referral_code", ""),
                        "code_type": "referral",
                        "event_type": "referral_bonus",
                        "bonus_given_hours": 48,
                    }).execute()
                except Exception as e:
                    print(f"[Admin] 記錄推薦獎勵事件失敗: {e}", flush=True)
                push_text(referrer_uid, "🎉 你推薦的好友完成正式註冊，使用期限 +48 小時！")
                reply_text(token, f"✅ 推薦人 {referrer_uid} 已 +48 小時")
        except Exception as e:
            print(f"[Admin] 推薦人加天失敗: {e}", flush=True)

def _parse_duration(s: str):
    """解析時間字串，回傳 (timedelta, 顯示文字) 或 None"""
    s = s.strip().lower()
    m = re.match(r'^(\d+)\s*(h|小時|hour|hours|天|d|day|days)?$', s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or "天"
    if val <= 0:
        return None
    if unit in ("h", "小時", "hour", "hours"):
        return timedelta(hours=val), f"{val} 小時"
    return timedelta(days=val), f"{val} 天"

def cmd_admin_extend(user_id, token, text):
    """延長 <user_id或REF碼> <天數或小時>"""
    if not is_admin(user_id):
        reply_text(token, "❌ 無權限"); return
    parts = text.strip().split()
    if len(parts) < 2:
        reply_text(token, "格式：延長 REF-XXXX 7天 或 延長 REF-XXXX 3h"); return
    target = parts[1]
    dur_str = parts[2] if len(parts) >= 3 else "7天"
    parsed = _parse_duration(dur_str)
    if not parsed:
        reply_text(token, "格式錯誤\n例如：延長 REF-XXXX 7天 / 3h / 24小時"); return
    delta, label = parsed

    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).eq("tenant_id", TENANT_ID).execute()

    if not r.data:
        reply_text(token, f"找不到成員：{target}"); return

    m = r.data[0]
    uid = m["user_id"]
    exp = m.get("expire_at")
    base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + delta).isoformat()
    sb().table("members").update({"expire_at": new_exp}).eq("user_id", uid).eq("tenant_id", TENANT_ID).execute()

    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    reply_text(token, f"✅ {uid} 延長 {label}\n新到期：{new_exp_tw}")
    try:
        push_text(uid, f"🎉 你的使用期限已延長 {label}！新到期時間：{new_exp_tw}")
    except Exception as e:
        print(f"[Extend] 通知用戶失敗: {e}", flush=True)

def cmd_admin_query(user_id, token, text):
    """查詢 REF-XXXX → 顯示該用戶的下線裂變數據"""
    target = text[2:].strip()
    if not target:
        reply_text(token, "格式：查詢 REF-XXXX"); return

    # 找目標用戶
    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        reply_text(token, f"找不到：{target}"); return

    m = r.data[0]
    target_uid = m["user_id"]
    target_code = m.get("referral_code", "?")
    is_agent = m.get("is_member") and m.get("expire_at") is None
    exp = m.get("expire_at")
    if is_agent:
        status = "代理（永久）"
    elif m.get("is_member"):
        status = "正式會員"
    elif exp:
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < exp_dt:
            status = "試用中"
        else:
            status = "已過期"
    else:
        status = "未啟用"

    # 遞迴查下線：用 referred_by 鏈追蹤
    all_downstream = []
    queue_uids = [target_uid]
    visited = {target_uid}
    while queue_uids:
        batch = queue_uids[:]
        queue_uids = []
        for uid in batch:
            children = sb().table("members").select("user_id,is_member,expire_at,referral_code").eq("referred_by", uid).eq("tenant_id", TENANT_ID).execute().data or []
            for child in children:
                if child["user_id"] not in visited:
                    visited.add(child["user_id"])
                    all_downstream.append(child)
                    queue_uids.append(child["user_id"])

    # 統計
    now = datetime.now(timezone.utc)
    total = len(all_downstream)
    paid = sum(1 for d in all_downstream if d.get("is_member"))
    trial = sum(1 for d in all_downstream if not d.get("is_member") and d.get("expire_at") and datetime.fromisoformat(d["expire_at"].replace("Z","+00:00")) > now)
    expired = total - paid - trial

    # 直屬
    direct = sb().table("members").select("user_id").eq("referred_by", target_uid).eq("tenant_id", TENANT_ID).execute().data or []
    direct_count = len(direct)

    reply_text(token,
        f"📋 查詢 {target_code}\n"
        f"━━━━━━━━━━━━━━\n"
        f"狀態：{status}\n"
        f"直屬：{direct_count} 人\n"
        f"裂變總計：{total} 人\n"
        f"  付費：{paid}\n"
        f"  試用：{trial}\n"
        f"  過期：{expired}")

def cmd_admin_set_agent(user_id, token, text):
    """設代理 REF-XXXX → 設為代理，永久使用"""
    target = text[3:].strip()
    if not target:
        reply_text(token, "格式：設代理 REF-XXXX"); return

    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        reply_text(token, f"找不到：{target}"); return

    m = r.data[0]
    target_uid = m["user_id"]
    target_code = m.get("referral_code", "?")

    # 設為正式會員 + 永久（expire_at=null）
    sb().table("members").update({
        "is_member": True,
        "expire_at": None,
    }).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()

    # 同步到 agents 表
    try:
        sb().table("agents").upsert({
            "agent_id": target_uid,
            "tenant_id": TENANT_ID,
            "is_active": True,
            "max_extend_days": 31,
        }).execute()
    except Exception as e:
        print(f"[SetAgent] agents 表寫入失敗: {e}", flush=True)

    reply_text(token, f"✅ {target_code} 已設為代理（永久使用）")
    try:
        push_text(target_uid, "🎉 你的帳號已升級為代理，永久使用所有功能！")
    except Exception:
        pass

def cmd_admin_set_ref_code(user_id, token, text):
    """設推廣碼 REF-XXXX BOSS888 → 幫代理設自訂推廣碼"""
    parts = text.replace("設推廣碼", "").replace("设推广码", "").strip().split()
    if len(parts) < 2:
        reply_text(token, "格式：設推廣碼 REF-XXXX BOSS888"); return
    target, new_code = parts[0].strip(), parts[1].strip().upper()
    if len(new_code) < 3 or len(new_code) > 20:
        reply_text(token, "推廣碼長度須 3~20 字元"); return
    if not re.match(r'^[A-Z0-9_-]+$', new_code):
        reply_text(token, "推廣碼只能包含英文、數字、底線、連字號"); return
    # 找代理
    agent = None
    if target.upper().startswith("REF-") or target.upper().startswith("AGENT-"):
        r = sb().table("agents").select("*").eq("agent_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        r = sb().table("agents").select("*").eq("custom_ref_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        r = sb().table("agents").select("*").eq("agent_id", target).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        reply_text(token, f"找不到代理：{target}"); return
    # 檢查碼是否已被使用
    existing = sb().table("agents").select("agent_id").eq("custom_ref_code", new_code).neq("agent_id", agent["agent_id"]).eq("tenant_id", TENANT_ID).execute()
    if existing.data:
        reply_text(token, f"推廣碼 {new_code} 已被其他代理使用"); return
    sb().table("agents").update({"custom_ref_code": new_code}).eq("agent_id", agent["agent_id"]).eq("tenant_id", TENANT_ID).execute()
    reply_text(token, f"✅ {agent['agent_code']} 的推廣碼已設為 {new_code}")

def cmd_admin_set_grant(user_id, token, text):
    """設贈送 REF-XXXX 24h → 設定代理碼贈送時間"""
    parts = text.replace("設贈送", "").replace("设赠送", "").strip().split()
    if len(parts) < 2:
        reply_text(token, "格式：設贈送 REF-XXXX 24h 或 7天"); return
    target, duration_str = parts[0].strip(), parts[1].strip()
    # 解析時間
    m = re.match(r'^(\d+)\s*(h|小時|hour|hours|天|d|day|days)$', duration_str.lower())
    if not m:
        reply_text(token, "格式錯誤，例如：24h、7天、168h"); return
    val = int(m.group(1))
    unit = m.group(2)
    if unit in ("天", "d", "day", "days"):
        hours = val * 24
    else:
        hours = val
    if hours <= 0 or hours > 8760:
        reply_text(token, "贈送時間須在 1h ~ 365天 之間"); return
    # 找代理
    agent = None
    if target.upper().startswith("REF-") or target.upper().startswith("AGENT-"):
        r = sb().table("agents").select("*").eq("agent_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        r = sb().table("agents").select("*").eq("custom_ref_code", target.upper()).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        r = sb().table("agents").select("*").eq("agent_id", target).eq("tenant_id", TENANT_ID).execute()
        if r.data: agent = r.data[0]
    if not agent:
        reply_text(token, f"找不到代理：{target}"); return
    sb().table("agents").update({"grant_hours": hours}).eq("agent_id", agent["agent_id"]).eq("tenant_id", TENANT_ID).execute()
    label = f"{hours//24} 天" if hours >= 24 and hours % 24 == 0 else f"{hours} 小時"
    reply_text(token, f"✅ {agent.get('custom_ref_code') or agent['agent_code']} 的贈送時間已設為 {label}")

def cmd_agent_extend(user_id, token, text, agent):
    """代理延長指令：延長 REF-XXXX X天"""
    parts = text.strip().split()
    if len(parts) < 3:
        reply_text(token, "格式：延長 REF-XXXX X天\n例如：延長 REF-AB12 7天"); return
    target_ref = parts[1].upper()
    # 解析天數：支援 "7天" 或 "7"
    day_str = parts[2].replace("天", "").strip()
    try:
        days = int(day_str)
    except ValueError:
        reply_text(token, "天數格式錯誤，請輸入數字\n例如：延長 REF-AB12 7天"); return

    if days <= 0:
        reply_text(token, "❌ 天數必須大於 0"); return

    max_days = agent.get("max_extend_days", 31)
    if days > max_days:
        reply_text(token, f"❌ 超過上限，你最多可延長 {max_days} 天"); return

    # 查找目標用戶
    if target_ref.startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target_ref).eq("tenant_id", TENANT_ID).execute()
    else:
        reply_text(token, "請使用推薦碼格式：REF-XXXX"); return

    if not r.data:
        reply_text(token, f"找不到用戶：{target_ref}"); return

    m = r.data[0]
    target_name = target_ref

    # 存入待確認
    _pending_extend[user_id] = {
        "target_ref": target_ref,
        "target_uid": m["user_id"],
        "days": days,
        "expire_ts": time.time() + 120,  # 2 分鐘內有效
    }
    reply_text(token,
        f"📋 延長確認\n"
        f"━━━━━━━━━━━━━━\n"
        f"用戶：{target_name}\n"
        f"天數：{days} 天\n"
        f"━━━━━━━━━━━━━━\n"
        f"輸入「確定」執行，或輸入其他取消")

def cmd_agent_confirm(user_id, token):
    """代理確認延長"""
    pending = _pending_extend.pop(user_id, None)
    if not pending:
        return False  # 沒有待確認的操作

    if time.time() > pending["expire_ts"]:
        reply_text(token, "⏰ 確認已過期，請重新輸入延長指令")
        return True

    target_uid = pending["target_uid"]
    target_ref = pending["target_ref"]
    days = pending["days"]

    # 執行延長
    r = sb().table("members").select("*").eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        reply_text(token, f"❌ 找不到用戶 {target_ref}"); return True

    m = r.data[0]
    exp = m.get("expire_at")
    base = max(datetime.fromisoformat(exp.replace("Z", "+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + timedelta(days=days)).isoformat()
    sb().table("members").update({"expire_at": new_exp}).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()

    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    reply_text(token, f"✅ 已延長 {target_ref} {days} 天\n新到期：{new_exp_tw}")
    try:
        push_text(target_uid, f"🎉 你的使用期限已延長 {days} 天！新到期時間：{new_exp_tw}")
    except Exception:
        pass
    return True

# ── GW {GW_NAME}相關 ──────────────────────────────────────────
def cmd_bind_gw_start(user_id, token, member):
    """用戶輸入「綁定帳號」，進入二段式問答"""
    existing = member.get("gw_account")
    if existing:
        status = member.get("gw_status", "pending")
        if status == "pending":
            reply_text(token,
                f"📋 你的帳號：{existing}\n"
                f"狀態：審核中 ⏳\n"
                f"━━━━━━━━━━━━━━\n"
                f"客服正在審核，請耐心等候\n"
                f"通過後系統會立即通知你\n\n"
                f"帳號填錯了？→ 輸入「更換帳號」")
        elif status == "verified":
            reply_text(token,
                f"📋 你的帳號：{existing}\n"
                f"狀態：已通過 ✅\n\n"
                f"如需再次延長，請在{GW_NAME}儲值點數後\n"
                f"客服確認後會自動延長期限")
        elif status == "rejected":
            reply_text(token,
                f"📋 你的帳號：{existing}\n"
                f"狀態：未通過 ❌\n\n"
                f"請確認是否已完成註冊及儲值\n"
                f"帳號填錯了？→ 輸入「更換帳號」")
        else:
            reply_text(token,
                f"📋 你的帳號：{existing}\n\n"
                f"帳號填錯了？→ 輸入「更換帳號」")
        return

    _pending_bind[user_id] = {"state": "awaiting_account", "expire_ts": time.time() + 120}
    reply_text(token,
        f"請輸入您在{GW_NAME}的帳號：\n"
        "━━━━━━━━━━━━━━\n"
        "直接輸入帳號即可（英文、數字皆可）\n\n"
        "⏳ 請在 2 分鐘內輸入")

def cmd_bind_gw_rebind(user_id, token, member):
    """用戶輸入「更換帳號」，重新綁定"""
    old = member.get("gw_account")
    if member.get("gw_status") == "verified":
        reply_text(token,
            f"⚠️ 你的帳號 {old} 已通過驗證\n"
            f"如需更換請聯繫客服")
        return
    _pending_bind[user_id] = {"state": "awaiting_account", "expire_ts": time.time() + 120}
    msg = f"目前綁定：{old}\n\n" if old else ""
    reply_text(token,
        f"{msg}請輸入新的{GW_NAME}帳號：\n"
        "━━━━━━━━━━━━━━\n"
        "直接輸入帳號即可\n\n"
        "⏳ 請在 2 分鐘內輸入")

def cmd_bind_gw_capture(user_id, token, text):
    """捕捉用戶輸入的 GW 帳號"""
    pending = _pending_bind.pop(user_id, None)
    if not pending or time.time() > pending["expire_ts"]:
        return False
    account = text.strip()
    if len(account) < 2 or len(account) > 30:
        reply_text(token, "帳號格式不正確，請重新輸入「綁定帳號」")
        return True
    # 檢查是否已被其他人綁定
    existing = sb().table("members").select("user_id").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if existing.data and existing.data[0]["user_id"] != user_id:
        reply_text(token, "⚠️ 此帳號已被其他用戶綁定，請確認後再試")
        return True
    sb().table("members").update({
        "gw_account": account,
    }).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
    reply_text(token,
        f"✅ 已記錄您的{GW_NAME}帳號：{account}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 下一步：前往{GW_NAME}儲值\n"
        f"👉 {REGISTER_URL}\n"
        f"{GW_TIERS_TEXT}\n\n"
        f"儲值完成後回來輸入「確認儲值」\n"
        f"客服確認後自動開通\n"
        f"━━━━━━━━━━━━━━\n"
        f"填錯了？→ 輸入「更換帳號」")
    return True

def cmd_gw_status(user_id, token, member):
    """用戶查詢審核狀態"""
    account = member.get("gw_account")
    if not account:
        reply_text(token,
            f"尚未綁定{GW_NAME}帳號\n\n"
            f"請先前往註冊：{REGISTER_URL}\n"
            "註冊後輸入「綁定帳號」綁定")
        return
    status = member.get("gw_status", "none")
    status_label = {"none": "未提交", "pending": "審核中", "verified": "已通過", "rejected": "未通過"}.get(status, status)
    reply_text(token,
        f"📋 {GW_NAME}帳號：{account}\n"
        f"審核狀態：{status_label}\n"
        f"━━━━━━━━━━━━━━\n"
                f"{GW_TIERS_TEXT}")

def _do_gw_verify(text: str, verified_by: str = "telegram") -> str:
    """純邏輯：確認儲值，回傳結果文字。成功時 LINE push 通知用戶。"""
    parts = text.strip().split()
    if len(parts) < 3:
        return "格式：確認 <帳號> <金額>\n例如：確認 abc123 5000"
    account = parts[1]
    try:
        amount = int(parts[2])
    except ValueError:
        return "金額請輸入數字"
    # 單一方案：金額 ±10% 彈性
    if amount < int(GW_AMOUNT * 0.9):
        return f"金額不足\n最低 {GW_AMOUNT:,} 點 → {GW_HOURS} 小時"
    hours = GW_HOURS
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    m = r.data[0]
    target_uid = m["user_id"]
    exp = m.get("expire_at")
    now = datetime.now(timezone.utc)
    base = max(datetime.fromisoformat(exp.replace("Z", "+00:00")), now) if exp else now
    new_exp = (base + timedelta(hours=hours)).isoformat()
    updates = {
        "expire_at": new_exp,
        "gw_status": "verified",
    }
    if not m.get("trial_start"):
        updates["trial_start"] = now.isoformat()
    sb().table("members").update(updates).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()
    try:
        sb().table("gw_deposits").insert({
            "tenant_id": TENANT_ID,
            "user_id": target_uid,
            "gw_account": account,
            "amount": amount,
            "hours_granted": hours,
            "verified_by": verified_by,
        }).execute()
    except Exception as e:
        print(f"[GW] 記錄儲值失敗: {e}", flush=True)
    # 首次儲值：推薦者 +48h
    referrer_uid = m.get("referred_by")
    if referrer_uid:
        try:
            already = sb().table("referral_events").select("id") \
                .eq("referee_id", target_uid).eq("event_type", "first_deposit_bonus") \
                .eq("tenant_id", TENANT_ID).limit(1).execute()
            if not already.data:
                rr = sb().table("members").select("expire_at").eq("user_id", referrer_uid).eq("tenant_id", TENANT_ID).execute()
                if rr.data:
                    ref_exp = rr.data[0].get("expire_at")
                    ref_base = max(datetime.fromisoformat(ref_exp.replace("Z","+00:00")), now) if ref_exp else now
                    new_ref_exp = (ref_base + timedelta(hours=48)).isoformat()
                    sb().table("members").update({"expire_at": new_ref_exp}).eq("user_id", referrer_uid).eq("tenant_id", TENANT_ID).execute()
                    sb().table("referral_events").insert({
                        "tenant_id": TENANT_ID,
                        "referrer_id": referrer_uid,
                        "referee_id": target_uid,
                        "code_used": m.get("referral_code", ""),
                        "code_type": "referral",
                        "event_type": "first_deposit_bonus",
                        "bonus_given_hours": 48,
                    }).execute()
                    try:
                        push_text(referrer_uid, "🎉 你推薦的好友完成首次儲值，使用期限 +48 小時！")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[GW] 首儲推薦獎勵失敗: {e}", flush=True)
    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    try:
        push_text(target_uid,
            f"🎉 帳號驗證通過！\n"
            f"━━━━━━━━━━━━━━\n"
            f"使用期限延長 {hours} 小時\n"
            f"新到期時間：{new_exp_tw}\n\n"
            f"感謝使用{BRAND_NAME}！")
    except Exception:
        pass
    return f"✅ 已確認 {account}\n儲值 {amount} 點 → 延長 {hours} 小時\n新到期：{new_exp_tw}"

def _do_gw_reject(text: str) -> str:
    """純邏輯：拒絕，回傳結果文字。"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：未通過 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    sb().table("members").update({"gw_status": "rejected"}).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()
    try:
        push_text(target_uid,
            "📋 帳號查詢結果\n"
            "━━━━━━━━━━━━━━\n"
            "驗證未通過，請確認是否已完成註冊及儲值\n\n"
            f"註冊網址：{REGISTER_URL}\n"
            "完成後請重新輸入「確認儲值」")
    except Exception:
        pass
    return f"✅ 已標記 {account} 未通過"

def _do_gw_not_deposited(text: str) -> str:
    """客服回報：有帳號但未儲值"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：未儲值 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            "📋 儲值查詢結果\n"
            "━━━━━━━━━━━━━━\n"
            "近 48 小時查無儲值紀錄\n"
            f"請先前往{GW_NAME}完成儲值\n\n"
            f"👉 {REGISTER_URL}\n"
            f"{GW_TIERS_TEXT}\n\n"
            "儲值完成後回來輸入「確認儲值」")
    except Exception:
        pass
    return f"✅ 已通知 {account} 用戶尚未儲值"

def _do_gw_not_found(text: str) -> str:
    """客服回報：查無此帳號"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：查無 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    sb().table("members").update({"gw_status": "rejected"}).eq("user_id", target_uid).eq("tenant_id", TENANT_ID).execute()
    try:
        push_text(target_uid,
            "📋 帳號查詢結果\n"
            "━━━━━━━━━━━━━━\n"
            "查無此帳號，請確認帳號是否正確\n\n"
            "填錯了？→ 輸入「更換帳號」")
    except Exception:
        pass
    return f"✅ 已通知 {account} 用戶查無此帳號"

def _do_gw_ask_cs(text: str) -> str:
    """客服回報：請用戶聯繫 GW 客服"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：請詢問 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            f"📋 請聯繫{GW_NAME}客服\n"
            "━━━━━━━━━━━━━━\n"
            f"您的帳號需要由{GW_NAME}客服協助處理\n"
            f"請直接聯繫{GW_NAME}線上客服\n\n"
            f"👉 {REGISTER_URL}")
    except Exception:
        pass
    return f"✅ 已通知 {account} 用戶聯繫{GW_NAME}客服"

def _do_gw_reply(text: str) -> str:
    """客服自訂回覆"""
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return "格式：回覆 <帳號> <訊息內容>"
    account = parts[1]
    message = parts[2]
    r = sb().table("members").select("*").eq("gw_account", account).eq("tenant_id", TENANT_ID).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            f"📋 {GW_NAME}客服回覆\n"
            f"━━━━━━━━━━━━━━\n"
            f"{message}")
    except Exception:
        pass
    return f"✅ 已傳送自訂訊息給 {account}"

def get_agent_by_custom_code(code_str: str):
    """用 custom_ref_code 查代理，回傳 agent row 或 None"""
    r = (sb().table("agents").select("*")
           .eq("custom_ref_code", code_str.upper())
           .eq("is_active", True)
           .eq("tenant_id", TENANT_ID)
           .execute())
    return r.data[0] if r.data else None

def _has_used(user_id: str, code_type: str) -> bool:
    """檢查用戶是否已使用過某類型的碼（agent_code / referral）"""
    r = (sb().table("referral_events").select("id")
           .eq("referee_id", user_id)
           .eq("code_type", code_type)
           .eq("tenant_id", TENANT_ID)
           .limit(1).execute())
    return bool(r.data)

def cmd_enter_code(user_id, token, text, member):
    # 清理輸入：去掉前綴、空格、冒號，取得純碼
    for prefix in ("好友推薦碼","好友推荐码","輸入推薦碼","输入推荐码","我的推薦碼是","推薦碼","推荐码","推廣碼","推广码"):
        text = text.replace(prefix, "")
    raw = text.strip().strip(":").strip()
    code_clean = re.sub(r'^REF-', '', raw, flags=re.IGNORECASE).strip()

    # 1. 查代理碼（custom_ref_code）
    agent = get_agent_by_custom_code(code_clean)
    if agent:
        if _has_used(user_id, "agent_code"):
            reply_text(token, "⚠️ 每人限用一次推廣碼"); return
        if agent["agent_id"] == user_id:
            reply_text(token, "不能輸入自己的推廣碼"); return
        bonus_hours = agent.get("grant_hours") or 6
        exp = member.get("expire_at")
        now = datetime.now(timezone.utc)
        base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), now) if exp else now
        new_exp = (base + timedelta(hours=bonus_hours)).isoformat()
        sb().table("members").update({
            "expire_at": new_exp,
            "referred_by": agent["agent_id"],
        }).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
        sb().table("referral_events").insert({
            "tenant_id": TENANT_ID,
            "referrer_id": agent["agent_id"],
            "referee_id": user_id,
            "code_used": agent["custom_ref_code"],
            "code_type": "agent_code",
            "event_type": "used_agent_code",
            "bonus_given_hours": bonus_hours,
        }).execute()
        days = bonus_hours // 24
        hours_rem = bonus_hours % 24
        if days and hours_rem:
            label = f"+{days} 天 {hours_rem} 小時"
        elif days:
            label = f"+{days} 天"
        else:
            label = f"+{bonus_hours} 小時"
        reply_text(token,
            f"✅ 推廣碼兌換成功！使用期限 {label} 🎁\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"🃏 功能說明\n\n"
            f"📡 {CMD_AIRDROP} X\n"
            f"→ 開啟全桌掃描 X 小時（1~3），\n"
            f"　偵測到優勢選項立即通知\n\n"
            f"🔗 {CMD_FOLLOW} X廳\n"
            f"→ 鎖定某張桌即時跟蹤，\n"
            f"　每局推送牌面+預期收益\n\n"
            f"🧙 {CMD_GUIDE}\n"
            f"→ 一鍵查詢最佳桌台\n\n"
            f"🛑 停止\n"
            f"→ 停止鎖定/掃描\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 升級正式會員\n"
            f"1️⃣ 前往{GW_NAME}註冊並儲值\n"
            f"   👉 {REGISTER_URL}\n"
            f"   {GW_TIERS_TEXT}\n"
            f"2️⃣ 回來輸入「綁定帳號」\n"
            f"3️⃣ 輸入「確認儲值」通知客服"); return

    # 2. 查推薦碼（REF-XXXX，會員互推）
    code_upper = code_clean.upper()
    if not code_upper.startswith("REF-"):
        code_upper = "REF-" + code_upper
    r_check = sb().table("members").select("user_id,expire_at").eq("referral_code", code_upper).eq("tenant_id", TENANT_ID).execute()
    if not r_check.data:
        reply_text(token, "找不到這個碼，請確認後再試"); return
    code = code_upper
    if _has_used(user_id, "referral"):
        reply_text(token, "⚠️ 每人限用一次推薦碼"); return
    referrer = r_check.data[0]
    if referrer["user_id"] == user_id:
        reply_text(token, "不能輸入自己的推薦碼"); return
    # 被推薦人 +6h
    now = datetime.now(timezone.utc)
    exp = member.get("expire_at")
    user_base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), now) if exp else now
    new_user_exp = (user_base + timedelta(hours=6)).isoformat()
    sb().table("members").update({
        "referred_by": referrer["user_id"],
        "expire_at": new_user_exp,
        "trial_start": now.isoformat() if not member.get("trial_start") else member["trial_start"],
    }).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
    # 推薦人 +6h
    ref_exp = referrer["expire_at"]
    ref_base = max(datetime.fromisoformat(ref_exp.replace("Z","+00:00")), now) if ref_exp else now
    new_ref_exp = (ref_base + timedelta(hours=6)).isoformat()
    sb().table("members").update({"expire_at": new_ref_exp}).eq("user_id", referrer["user_id"]).eq("tenant_id", TENANT_ID).execute()
    sb().table("referral_events").insert({
        "tenant_id": TENANT_ID,
        "referrer_id": referrer["user_id"],
        "referee_id": user_id,
        "code_used": code,
        "code_type": "referral",
        "event_type": "referral_used",
        "bonus_given_hours": 6,
    }).execute()
    reply_text(token,
        f"✅ 推薦碼輸入成功！使用期限 +6 小時 🎁\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"🃏 功能說明\n\n"
        f"📡 {CMD_AIRDROP} X\n"
        f"→ 開啟全桌掃描 X 小時（1~3），\n"
        f"　偵測到優勢選項立即通知\n\n"
        f"🔗 {CMD_FOLLOW} X廳\n"
        f"→ 鎖定某張桌即時跟蹤，\n"
        f"　每局推送牌面+預期收益\n\n"
        f"🧙 {CMD_GUIDE}\n"
        f"→ 一鍵查詢最佳桌台\n\n"
        f"🛑 停止\n"
        f"→ 停止鎖定/掃描\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 升級正式會員\n"
        f"1️⃣ 前往{GW_NAME}註冊並儲值\n"
        f"   👉 {REGISTER_URL}\n"
        f"   {GW_TIERS_TEXT}\n"
        f"2️⃣ 回來輸入「綁定帳號」\n"
        f"3️⃣ 輸入「確認儲值」通知客服")
    try:
        push_text(referrer["user_id"], "🎉 有好友使用你的推薦碼，使用期限 +6 小時！")
    except Exception:
        pass


def cmd_ev_intro(user_id, token):
    reply_text(token,
        "📊 預期收益是什麼？\n"
        "━━━━━━━━━━━━━━\n\n"
        "預期收益（EV = Expected Value），\n"
        "代表每一注的長期平均報酬。\n\n"
        "預期收益 > 0 → 這注長期有利可圖\n"
        "預期收益 < 0 → 這注長期會虧\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂通常莊閒預期收益都是負的，\n"
        "但隨著牌靴消耗，偶爾會出現\n"
        "預期收益翻正的瞬間 — 這就是出手時機。\n\n"
        f"{BRAND_NAME}替你即時計算每張桌的預期收益，\n"
        "在正預期收益出現時第一時間通知你。\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"如果還是難以理解，直接輸入「{CMD_GUIDE}」\n"
        "系統會從 MT + DG 近 30 張桌中，\n"
        "即時選出當下最佳的投注選項給你。\n"
        "這是目前全網最強的百家樂輔助功能。")

def cmd_card_intro(user_id, token):
    reply_text(token,
        f"🃏 {BRAND_NAME}怎麼算？\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂用 8 副牌（416 張），\n"
        "每發一局牌，剩餘牌組就會改變。\n\n"
        "我們的系統：\n"
        "1️⃣ 即時記錄已出的每一張牌\n"
        "2️⃣ 根據剩餘牌組，窮舉所有可能\n"
        "3️⃣ 計算莊/閒/和/超六/對子的預期收益\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "跟 21 點算牌同理：\n"
        "已出的牌會影響後續的機率分佈。\n\n"
        "差別是我們用電腦完整計算，\n"
        "不是靠人腦估算。\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"如果還是難以理解，直接輸入「{CMD_GUIDE}」\n"
        "系統會從 MT + DG 近 30 張桌中，\n"
        "即時選出當下最佳的投注選項給你。\n"
        "這是目前全網最強的百家樂輔助功能。")

def cmd_feature_intro(user_id, token):
    # 第一則：功能與使用方式
    reply_text(token,
        f"{BRAND_NAME} ── 功能介紹\n"
        "━━━━━━━━━━━━━━\n\n"
        "即時監控兩大場館百家樂：\n"
        "  MT 13 廳 ＋ DG 14 桌\n"
        "8 副牌完整追蹤，計算 6 種注區預期收益：\n"
        "莊 / 閒 / 和 / 超級六 / 閒對 / 莊對\n\n"
        f"▸ {CMD_GUIDE}\n"
        "  一鍵掃描全桌，推薦🔴莊或🔵閒。\n"
        f"  → 點選單「{CMD_GUIDE}」\n\n"
        f"▸ {CMD_AIRDROP}\n"
        "  開啟後，優勢選項出現立刻推播通知你。\n"
        f"  → 點選單「{CMD_AIRDROP}」\n\n"
        f"▸ {CMD_FOLLOW}\n"
        f"  鎖定單桌，即時推送牌面、預期收益與結果。\n"
        f"  → MT：{CMD_FOLLOW} 3廳 / DG：{CMD_FOLLOW} 01\n\n"
        "▸ 切換場館\n"
        "  → 輸入「切換」即可在 MT / DG 間切換\n\n"
        "▸ 預期收益與算牌原理\n"
        "  → 輸入「EV介紹」或「算牌介紹」\n\n"
        "━━━━━━━━━━━━━━\n"
        "💡 不知道預期收益是什麼也沒關係，\n"
        f"先試「{CMD_GUIDE}」，看到正預期收益就是出手訊號。")
    # 第二則：關於我們 + 推薦機制
    push_text(user_id,
        f"關於{BRAND_NAME}\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂不是純運氣的遊戲。\n"
        "8 副牌、416 張牌，每發一張牌，\n"
        "剩餘牌組的機率結構就在改變。\n\n"
        f"{BRAND_NAME}做的事很單純：\n"
        "同時監控 MT + DG 兩大場館，\n"
        "把數學算好，即時告訴你。\n\n"
        "我們不賣牌路、不帶單、不保證贏，\n"
        "只提供透明的數據，讓你自己判斷。\n\n"
        "━━━━━━━━━━━━━━\n"
        "🎁 推薦好友計畫\n\n"
        "把你的專屬推薦碼分享給朋友：\n"
        "  ✦ 推薦好友 → 雙方各得 6 小時\n"
        "  ✦ 好友首次儲值 → 你再得 48 小時\n\n"
        "→ 輸入「我的推薦碼」查看你的推薦碼\n"
        "→ 輸入「指令」查看所有指令")

# ── Webhook ──────────────────────────────────────────────
@app.route("/flow")
def registration_flow():
    from flask import send_from_directory
    return send_from_directory("static", "registration-flow.html")

@app.route("/health")
def health():
    from flask import jsonify
    return jsonify({
        "status": "ok",
        "poll_count": _poll_stats["count"],
        "airdrop_triggers": _poll_stats["airdrop_triggers"],
        "last_trigger": _poll_stats["last_trigger"],
        "following_users": len(following),
        "airdrop_users": len(airdrop),
    })

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    """Telegram webhook — GW 客服指令入口"""
    from flask import jsonify
    # 驗證 Telegram secret token
    if TG_WEBHOOK_SECRET:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != TG_WEBHOOK_SECRET:
            return "Forbidden", 403
    data = request.get_json(silent=True)
    if not data or "message" not in data:
        return "OK"
    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()
    if not text:
        return "OK"
    print(f"[TG] chat_id={chat_id}: {text}", flush=True)
    # /start 指令 — 回傳 chat_id 方便設定
    if text == "/start":
        tg_send(chat_id, f"歡迎使用{BRAND_NAME} GW 客服系統\n\n你的 Chat ID：{chat_id}\n\n請將此 ID 提供給管理員完成設定")
        return "OK"
    # 驗證是否為 GW 客服
    if chat_id not in TG_GW_CHAT_IDS:
        tg_send(chat_id, f"⚠️ 無權限\n你的 Chat ID：{chat_id}\n請聯繫管理員開通")
        return "OK"
    # 處理指令
    CMD_HELP = (f"📋 {BRAND_NAME} GW 客服指令\n"
                "━━━━━━━━━━━━━━\n\n"
                "確認 <帳號> <金額>\n→ 儲值確認，自動延長期限\n\n"
                "未儲值 <帳號>\n→ 帳號存在但尚未儲值\n\n"
                "查無 <帳號>\n→ 查無此帳號\n\n"
                "未通過 <帳號>\n→ 驗證未通過\n\n"
                f"請詢問 <帳號>\n→ 請用戶聯繫{GW_NAME}客服\n\n"
                "回覆 <帳號> <訊息>\n→ 自訂回覆內容\n\n"
                "範例：確認 abc123 3000")
    if text.startswith("確認") or text.startswith("确认"):
        result = _do_gw_verify(text, verified_by=f"tg_{chat_id}")
        tg_send(chat_id, result)
    elif text.startswith("未儲值") or text.startswith("未储值"):
        result = _do_gw_not_deposited(text)
        tg_send(chat_id, result)
    elif text.startswith("查無") or text.startswith("查无"):
        result = _do_gw_not_found(text)
        tg_send(chat_id, result)
    elif text.startswith("未通過") or text.startswith("未通过"):
        result = _do_gw_reject(text)
        tg_send(chat_id, result)
    elif text.startswith("請詢問") or text.startswith("请询问"):
        result = _do_gw_ask_cs(text)
        tg_send(chat_id, result)
    elif text.startswith("回覆") or text.startswith("回复"):
        result = _do_gw_reply(text)
        tg_send(chat_id, result)
    elif "指令" in text:
        tg_send(chat_id, CMD_HELP)
    else:
        pass  # 群組一般聊天不回覆
    return "OK"

@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"[Webhook] 未預期錯誤: {e}", flush=True)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    token   = event.reply_token
    text    = event.message.text.strip()
    print(f"[MSG] {user_id}: {text}", flush=True)

    try:
        member = get_or_create_member(user_id)
    except Exception as e:
        print(f"[Member Error] {e}", flush=True)
        reply_text(token, "系統暫時忙碌，請稍後再試")
        return

    if text == "管理員指令":
        if not is_admin(user_id):
            reply_text(token, "❌ 無管理員權限"); return
        reply_text(token,
            "🔧 管理員指令列表\n"
            "────────────────\n"
            "開通 REF-XXXX\n"
            "開通 <user_id>\n"
            "→ 升級為正式會員（永久）\n\n"
            "延長 <user_id或REF碼> <天數或小時>\n"
            "→ 例如：延長 REF-XXXX 7天 / 3h\n\n"
            "管理員指令\n"
            "→ 顯示本列表\n\n"
            "維護開 / 維護關\n"
            "→ 開關維護模式\n\n"
            "測試開 / 測試關\n"
            "→ 測試模式（只有管理員能操作和收到推送）\n\n"
            "DG開 / DG關 / MT開 / MT關\n"
            "→ 開關場館（關閉後用戶無法切換）\n\n"
            "切換 / 切換DG / 切換MT\n"
            "→ 切換數據平台（所有用戶可用）\n\n"
            "查詢 REF-XXXX\n"
            "→ 查看該用戶下線裂變數據\n\n"
            "設代理 REF-XXXX\n"
            "→ 設為代理（永久使用）\n\n"
            "設推廣碼 REF-XXXX BOSS888\n"
            "→ 幫代理設自訂推廣碼\n\n"
            "設贈送 REF-XXXX 24h\n"
            "→ 設定代理碼贈送時間（h/天）"
        ); return
    if text == "維護開":
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_maintenance(True)
        reply_text(token, "🔧 維護模式已開啟，用戶指令暫停回應"); return
    if text == "維護關":
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_maintenance(False)
        reply_text(token, "✅ 維護模式已關閉，恢復正常"); return
    if text == "測試開":
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_test_mode(True)
        reply_text(token, "🧪 測試模式已開啟\n只有管理員能使用指令和收到推送"); return
    if text == "測試關":
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_test_mode(False)
        reply_text(token, "✅ 測試模式已關閉，所有用戶恢復正常"); return
    if text in ("DG開", "dg開", "Dg開"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_platform_enabled("DG", True)
        reply_text(token, "✅ DG 場館已開放"); return
    if text in ("DG關", "dg關", "Dg關"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_platform_enabled("DG", False)
        reply_text(token, "🔒 DG 場館已關閉，用戶無法切換至 DG"); return
    if text in ("MT開", "mt開", "Mt開"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_platform_enabled("MT", True)
        reply_text(token, "✅ MT 場館已開放"); return
    if text in ("MT關", "mt關", "Mt關"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        set_platform_enabled("MT", False)
        reply_text(token, "🔒 MT 場館已關閉，用戶無法切換至 MT"); return
    if text in ("切換", "切換平台", "切換遊戲"):
        cur = member.get("game") or "MT"
        new_plat = "DG" if cur == "MT" else "MT"
        if not is_platform_enabled(new_plat) and not is_admin(user_id):
            reply_text(token, f"🔒 {new_plat} 場館目前未開放"); return
        sb().table("members").update({"game": new_plat}).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
        with follow_lock:
            following.pop(user_id, None)
        with airdrop_lock:
            airdrop.pop(user_id, None)
        if new_plat == "DG":
            try:
                dg_count = len(sb().table("live_tables").select("table_id").eq("platform", "DG").execute().data or [])
            except Exception:
                dg_count = 0
            reply_text(token, f"✅ 已切換到 DG 平台\n目前 {dg_count} 桌在線\n\n桌號：01~07\n\n{CMD_FOLLOW}/{CMD_AIRDROP}/{CMD_GUIDE} 將使用 DG 數據"); return
        reply_text(token, f"✅ 已切換到 MT 平台\n13 廳在線\n\n{CMD_FOLLOW}/{CMD_AIRDROP}/{CMD_GUIDE} 將使用 MT 數據"); return
    if text in ("切換DG", "切換dg", "切換Dg"):
        if not is_platform_enabled("DG") and not is_admin(user_id):
            reply_text(token, "🔒 DG 場館目前未開放"); return
        sb().table("members").update({"game": "DG"}).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
        with follow_lock:
            following.pop(user_id, None)
        with airdrop_lock:
            airdrop.pop(user_id, None)
        try:
            dg_count = len(sb().table("live_tables").select("table_id").eq("platform", "DG").execute().data or [])
        except Exception:
            dg_count = 0
        reply_text(token, f"✅ 已切換到 DG 平台\n目前 {dg_count} 桌在線\n\n桌號：01~07\n\n{CMD_FOLLOW}/{CMD_AIRDROP}/{CMD_GUIDE} 將使用 DG 數據"); return
    if text in ("切換MT", "切換mt", "切換Mt"):
        if not is_platform_enabled("MT") and not is_admin(user_id):
            reply_text(token, "🔒 MT 場館目前未開放"); return
        sb().table("members").update({"game": "MT"}).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
        with follow_lock:
            following.pop(user_id, None)
        with airdrop_lock:
            airdrop.pop(user_id, None)
        reply_text(token, f"✅ 已切換到 MT 平台\n13 廳在線\n\n{CMD_FOLLOW}/{CMD_AIRDROP}/{CMD_GUIDE} 將使用 MT 數據"); return
    if text.startswith("查詢") or text.startswith("查询"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_query(user_id, token, text); return
    if text.startswith("設代理") or text.startswith("设代理"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_set_agent(user_id, token, text); return
    if text.startswith("設推廣碼") or text.startswith("设推广码"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_set_ref_code(user_id, token, text); return
    if text.startswith("設贈送") or text.startswith("设赠送"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_set_grant(user_id, token, text); return
    if text.startswith("開通"):
        cmd_admin_activate(user_id, token, text); return
    if text.startswith("延長"):
        if is_admin(user_id):
            cmd_admin_extend(user_id, token, text); return
        agent = get_agent(user_id)
        if agent:
            cmd_agent_extend(user_id, token, text, agent); return
        reply_text(token, "❌ 無權限"); return

    # 代理確認（「確定」）
    if text == "確定" and user_id in _pending_extend:
        cmd_agent_confirm(user_id, token); return

    # 二段式跟隨（捕捉用戶回覆的桌號，但如果是其他指令則直接執行）
    if user_id in _pending_follow:
        pf = _pending_follow.get(user_id, {})
        if time.time() < pf.get("expire_ts", 0):
            # 檢查是否為其他有效指令，如果是就取消等待、直接走正常流程
            _is_cmd = any(text.startswith(k) for k in (CMD_AIRDROP,"空投","開始空投","全局監控","掃描桌檯","掃描桌台","停止","結束","stop","推薦碼","推廣碼","好友推薦碼")) or \
                      CMD_GUIDE in text or "仙人指路" in text or "最佳推薦" in text or "開始報牌" in text or \
                      re.match(r'^[A-Za-z0-9\-]{3,20}$', text) or \
                      text in ("介紹","說明","指令","help","切換","切換平台","繼續","綁定帳號","確認儲值","審核狀態",
                               "功能介紹","EV介紹","算牌介紹","我的推薦碼","推薦碼","聊天室")
            if not _is_cmd:
                _pending_follow.pop(user_id, None)
                cmd_follow(user_id, token, "跟隨" + text, member)
                return
        _pending_follow.pop(user_id, None)

    # 二段式 GW 帳號綁定（捕捉用戶回覆）
    if user_id in _pending_bind:
        if cmd_bind_gw_capture(user_id, token, text): return

    # 維護模式：非管理員直接不回應（LINE 自動回覆處理）
    if is_maintenance() and not is_admin(user_id):
        return

    # 測試模式：只有管理員能操作
    if is_test_mode() and not is_admin(user_id):
        return

    # 標記新用戶已歡迎（歡迎訊息由 LINE 官方帳號歡迎詞處理）
    if not member.get("welcomed"):
        try:
            sb().table("members").update({"welcomed": True}).eq("user_id", user_id).eq("tenant_id", TENANT_ID).execute()
            member["welcomed"] = True
        except Exception as e:
            print(f"[Welcome] 更新 welcomed 失敗: {e}", flush=True)

    # 指令匹配（寬鬆化）
    txt_lower = text.lower()

    # ── 不需要 CD 的指令（純文字 / 輕量查詢）──
    if text in ("介紹", "我的帳號", "我的帐号") or "全廳掃描" in text:
        cmd_intro(user_id, token, member); return
    if txt_lower in ("ev介紹", "ev介绍", "ev 介紹", "ev 介绍"):
        cmd_ev_intro(user_id, token); return
    if text in ("算牌介紹", "算牌介绍"):
        cmd_card_intro(user_id, token); return
    if text in ("功能介紹", "功能介绍", "詳細介紹", "详细介绍", "了解更多"):
        cmd_feature_intro(user_id, token); return
    if text in ("我的推薦碼", "推薦碼", "我的推荐码", "推荐码"):
        cmd_my_code(user_id, token, member); return
    if text in ("繼續", "继续", "繼續使用", "继续使用"):
        cmd_continue_info(user_id, token, member); return
    if text in ("確認儲值", "确认储值", "儲值確認", "储值确认"):
        cmd_confirm_deposit(user_id, token, member); return
    if text in ("綁定帳號", "绑定帐号", "綁定"):
        cmd_bind_gw_start(user_id, token, member); return
    if text in ("更換帳號", "更换帐号"):
        cmd_bind_gw_rebind(user_id, token, member); return
    if text in ("審核狀態", "审核状态", "審核"):
        cmd_gw_status(user_id, token, member); return
    if text in ("聊天室", "群組", "社群"):
        if CHAT_URL:
            reply_text(token, f"💬 加入{BRAND_NAME}聊天室\n\n👉 {CHAT_URL}")
        else:
            reply_text(token, f"💬 {BRAND_NAME}聊天室尚未開放")
        return
    if text in ("說明", "说明", "help", "指令", "Help", "HELP"):
        plat_now = get_user_platform(member)
        plat_info = "DG 14 桌" if plat_now == "DG" else "MT 13 廳"
        follow_hint = f"{CMD_FOLLOW} 01~07" if plat_now == "DG" else f"{CMD_FOLLOW} X廳（1~13）"
        reply_text(token,
            f"🃏 {BRAND_NAME} 指令說明\n"
            f"📡 目前場館：{plat_info}（輸入「切換」可切換）\n"
            "━━━━━━━━━━━━━━\n\n"
            f"📡 {CMD_AIRDROP} X\n"
            "→ 開啟全桌掃描 X 小時（1~3），\n"
            "　偵測到優勢選項立即通知\n\n"
            f"🔗 {follow_hint}\n"
            "→ 鎖定某張桌即時跟蹤，\n"
            "　每局推送牌面+預期收益\n\n"
            f"🧙 {CMD_GUIDE}\n"
            "→ 一鍵查詢最佳桌台\n\n"
            "🛑 停止\n"
            f"→ 停止{CMD_FOLLOW}/{CMD_AIRDROP}\n\n"
            "🔄 切換\n"
            "→ 切換 MT / DG 場館\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📋 我的推薦碼 → 查推薦碼與期限\n"
            "🎁 好友推薦碼 REF-XXXX → 輸入推薦碼\n"
            f"🔗 綁定帳號 → 綁定{GW_NAME}帳號\n"
            "💰 確認儲值 → 儲值後通知客服確認\n"
            "📊 審核狀態 → 查詢帳號審核進度\n"
            "📊 EV介紹 → 預期收益是什麼？\n"
            "🃏 算牌介紹 → 我們怎麼計算？\n"
            "📖 介紹 → 帳號狀態與說明\n"
            "💬 聊天室 → 進群交流\n\n"
            "💡 所有指令送出後，請稍等 5 秒再進行操作")
        return

    # ── 需要 CD 的指令（查全廳 / 寫入狀態）──
    if not check_cooldown(user_id):
        return

    if any(text.startswith(k) for k in (CMD_FOLLOW, "跟隨","跟随","追隨","追蹤","監控")):
        body = re.sub(r'^(' + '|'.join([CMD_FOLLOW, "跟隨","跟随","追隨","追蹤","監控"]) + ')', '', text).strip()
        cmd_follow(user_id, token, "跟隨" + body, member)
    elif text.startswith(CMD_AIRDROP) or text.startswith("空投") or text.startswith("開始空投") or text.startswith("全局監控") or text.startswith("掃描桌檯") or text.startswith("掃描桌台"):
        cmd_airdrop(user_id, token, text, member)
    elif text in ("停止", "結束", "stop", "Stop", "STOP"):
        cmd_stop(user_id, token)
    elif CMD_GUIDE in text or "仙人指路" in text or "最佳推薦" in text or "開始報牌" in text:
        cmd_guide(user_id, token, member)
    elif any(text.startswith(k) for k in ("好友推薦碼","好友推荐码","推薦碼","推荐码","推廣碼","推广码","輸入推薦碼","输入推荐码","我的推薦碼是")):
        cmd_enter_code(user_id, token, text, member)
    elif re.match(r'^[A-Za-z0-9\-]{3,20}$', text) and text.lower() not in ("stop","help","test","menu"):
        # 3~20 碼英數字（含 REF-XXXX、代理碼），自動當推薦碼處理
        cmd_enter_code(user_id, token, text, member)

# ── 背景輪詢 ──────────────────────────────────────────────
def _check_data_freshness(latest_hands: dict):
    """Render 端獨立監控：數據超過 3 分鐘未更新就發 TG 警報"""
    global _stale_alert_active, _last_stale_check
    now_ts = time.time()
    if now_ts - _last_stale_check < STALE_CHECK_INTERVAL:
        return
    _last_stale_check = now_ts
    if not WATCHDOG_TG_TOKEN or not ADMIN_TG_CHAT_ID:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        freshest_age = None
        for row in latest_hands.values():
            ca = row.get("updated_at", "") or row.get("created_at", "")
            if not ca:
                continue
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            age = (now_utc - dt).total_seconds()
            if freshest_age is None or age < freshest_age:
                freshest_age = age
        if freshest_age is None:
            freshest_age = 9999
        if freshest_age > STALE_THRESHOLD and not _stale_alert_active:
            _stale_alert_active = True
            msg = (f"\U0001f6a8 [Render] \u6570\u636e\u5df2 {int(freshest_age)} \u79d2\u672a\u66f4\u65b0\uff01\n"
                   f"ev_monitor \u53ef\u80fd\u65b7\u7dda\u6216\u5d29\u6f70\n"
                   f"\u8acb\u6aa2\u67e5\u672c\u6a5f ev_monitor \u72c0\u614b")
            _httpx.post(
                f"https://api.telegram.org/bot{WATCHDOG_TG_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_TG_CHAT_ID, "text": msg}, timeout=10)
            print(f"[Freshness] ALERT sent: {int(freshest_age)}s stale", flush=True)
        elif freshest_age <= STALE_THRESHOLD and _stale_alert_active:
            _stale_alert_active = False
            msg = f"\u2705 [Render] \u6578\u64da\u5df2\u6062\u5fa9\u6b63\u5e38\uff08{int(freshest_age)}\u79d2\u524d\u66f4\u65b0\uff09"
            _httpx.post(
                f"https://api.telegram.org/bot{WATCHDOG_TG_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_TG_CHAT_ID, "text": msg}, timeout=10)
            print(f"[Freshness] RECOVERED: {int(freshest_age)}s", flush=True)
    except Exception as e:
        print(f"[Freshness Error] {e}", flush=True)


def poll_loop():
    global _last_trial_check
    print(f"[poll_loop] 啟動 pid={os.getpid()}", flush=True)
    while True:
        time.sleep(5)  # 5秒一次（從2秒改，減輕負載）
        try:
            # 一次 query 取得所有桌台最新手，供 following & airdrop 共用
            latest_hands = get_all_latest_hands()
        except Exception as e:
            print(f"[Poll] 取手牌失敗: {e}", flush=True)
            continue

        try:
            _poll_following(latest_hands)
        except Exception as e:
            print(f"[Poll] _poll_following 崩潰: {e}", flush=True)
        try:
            _poll_airdrop(latest_hands)
        except Exception as e:
            print(f"[Poll] _poll_airdrop 崩潰: {e}", flush=True)

        # 數據新鮮度監控
        _check_data_freshness(latest_hands)

        # 試用到期警告 + 記憶體清理：每 60 秒
        now_ts = time.time()
        if now_ts - _last_trial_check >= 60:
            _last_trial_check = now_ts
            _poll_trial_warnings()
            # 清理過期的記憶體狀態
            for d in (_cooldown, _pending_extend, _pending_bind, _pending_follow):
                stale = [k for k, v in d.items()
                         if isinstance(v, (int, float)) and now_ts - v > 300
                         or isinstance(v, dict) and now_ts > v.get("expire_ts", now_ts + 1)]
                for k in stale:
                    d.pop(k, None)

def _poll_following(latest_hands: dict):
    _test_mode = is_test_mode()
    with follow_lock:
        users = dict(following)
    for user_id, state in users.items():
        # 測試模式：跳過非管理員
        if _test_mode and not is_admin(user_id):
            continue
        tid       = state["table_id"]
        last_shoe = state["last_shoe"]
        last_hand = state["last_hand"]
        try:
            row = latest_hands.get(tid)
            if not row:
                # 超過 60 秒還查不到資料 → 通知用戶
                if last_shoe is None and time.time() - state.get("started_at", time.time()) > 60:
                    push_text(user_id, f"⚠️ 第{tnum(tid)}廳目前查無資料\n請確認監控腳本是否正在運行中")
                    with follow_lock:
                        following.pop(user_id, None)
                print(f"[Follow] {tid} 查無資料", flush=True); continue
            cur_shoe, cur_hand = row["shoe"], row["hand_num"]

            if last_shoe is not None and cur_shoe != last_shoe:
                push_text(user_id, f"🔄 第{tnum(tid)}廳 換靴，跟隨已停止")
                with follow_lock:
                    following.pop(user_id, None)
                continue

            if last_shoe is None:
                # 等 EV 回填完再推
                if row.get("ev_banker") is None:
                    continue
                with follow_lock:
                    if user_id in following:
                        following[user_id]["last_shoe"] = cur_shoe
                        following[user_id]["last_hand"] = cur_hand
                print(f"[Follow] 首次連線，push 確認給 {user_id}", flush=True)
                push_text(user_id, f"✅ 已開始跟隨第{tnum(tid)}廳\n每局新牌即時推送，換靴自動停止\n再次輸入「跟隨」可手動停止")
                push_text(user_id, format_hand(row))
                print(f"[Follow] push 完成", flush=True)
                continue

            if cur_hand > last_hand:
                # live_tables 永遠只有最新一局，直接推
                if row.get("ev_banker") is None:
                    continue
                push_text(user_id, format_hand(row))
                with follow_lock:
                    if user_id in following:
                        following[user_id]["last_shoe"] = cur_shoe
                        following[user_id]["last_hand"] = cur_hand
        except Exception as e:
            print(f"[Follow Error] {user_id}: {e}", flush=True)

def _poll_airdrop(latest_hands: dict):
    _test_mode = is_test_mode()
    now = datetime.now(timezone.utc)
    with airdrop_lock:
        users = dict(airdrop)
    if not users:
        return
    # poll 計數 + 定期日誌
    _poll_stats["count"] += 1
    if _poll_stats["count"] % 100 == 0:
        last = _poll_stats["last_trigger"] or "從未觸發"
        print(f"[Airdrop Poll] {_poll_stats['count']} 次查詢, "
              f"{_poll_stats['airdrop_triggers']} 次觸發, "
              f"監控用戶: {len(users)}, 上次觸發: {last}", flush=True)
    # 查 positive_ev View，取全部正 EV 桌（按平台分組給各用戶）
    try:
        pos_rows = sb().table("positive_ev").select("*").execute().data
        pos_hands_mt = {row["table_id"]: row for row in pos_rows
                        if row.get("platform") == "MT" and row["table_id"] != "TEST01"}
        pos_hands_dg = {row["table_id"]: row for row in pos_rows
                        if row.get("platform") == "DG" and not _hide_sexy(row["table_id"])}
    except Exception as e:
        print(f"[Airdrop] 查 positive_ev 失敗: {e}", flush=True)
        pos_hands_mt, pos_hands_dg = {}, {}
    active_tables = len(latest_hands)
    # 預載用戶平台偏好（批次查一次，避免每用戶都查 DB）
    _user_plats = {}
    try:
        uids = list(users.keys())
        mr = sb().table("members").select("user_id,game").in_("user_id", uids).eq("tenant_id", TENANT_ID).execute()
        _user_plats = {m["user_id"]: (m.get("game") or DEFAULT_PLATFORM) for m in (mr.data or [])}
    except Exception:
        pass
    for user_id, state in users.items():
        # 測試模式：跳過非管理員
        if _test_mode and not is_admin(user_id):
            continue
        try:
            if now > state["expire_at"]:
                cnt = state.get("push_count", 0)
                if cnt > 0:
                    end_msg = f"🪂 {CMD_AIRDROP}監控已結束\n本次共捕獲 {cnt} 次優勢訊號\n\n輸入「{CMD_AIRDROP}」可再次啟動"
                else:
                    end_msg = f"🪂 {CMD_AIRDROP}監控已結束\n本次監控期間未偵測到優勢訊號\n\n輸入「{CMD_AIRDROP}」可再次啟動"
                push_text(user_id, end_msg)
                with airdrop_lock:
                    airdrop.pop(user_id, None)
                continue

            user_plat = _user_plats.get(user_id, "MT")
            pos_hands = pos_hands_dg if user_plat == "DG" else pos_hands_mt
            for tid, row in pos_hands.items():
                cur_hand = row["hand_num"]
                if cur_hand <= state["notified"].get(tid, 0):
                    continue
                with airdrop_lock:
                    if user_id in airdrop:
                        airdrop[user_id]["notified"][tid] = cur_hand
                pos = [(EV_LABELS[f], row[f]) for f in EV_FIELDS if row.get(f) and row[f] > 0]
                if pos:
                    _poll_stats["airdrop_triggers"] += 1
                    _poll_stats["last_trigger"] = datetime.now(timezone.utc).strftime("%m/%d %H:%M")
                    with airdrop_lock:
                        if user_id in airdrop:
                            airdrop[user_id]["push_count"] = airdrop[user_id].get("push_count", 0) + 1
                    dealer = row.get("dealer") or ""
                    d_str = f" 荷官：{dealer}" if dealer and dealer != "未知" else ""
                    next_hand = cur_hand + 1
                    air_plat = row.get("platform", "MT")
                    lines = [f"🪂 優勢選項 [{air_plat}] 第{tnum(tid)}廳{d_str}", f"第{next_hand}局"]
                    for label, val in sorted(pos, key=lambda x: -x[1]):
                        lines.append(f"{label}預期收益：{val:+.4f} ✅")
                    push_text(user_id, "\n".join(lines))
        except Exception as e:
            print(f"[Airdrop Error] {user_id}: {e}", flush=True)

def _poll_trial_warnings():
    now = datetime.now(timezone.utc)
    warn_threshold = now + timedelta(minutes=WARN_MINUTES)
    try:
        r = (sb().table("members").select("user_id,expire_at,referral_code")
               .eq("is_member", False).eq("warned_15min", False)
               .eq("tenant_id", TENANT_ID)
               .gte("expire_at", now.isoformat()).lte("expire_at", warn_threshold.isoformat())
               .execute())
        for m in (r.data or []):
            exp = m.get("expire_at")
            if not exp: continue
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if now < exp_dt <= warn_threshold:
                code = m.get("referral_code", "N/A")
                try:
                    push_text(m["user_id"],
                        f"⏰ 使用期限還有 15 分鐘到期\n\n"
                        f"想繼續使用嗎？\n"
                        f"回覆「繼續」即可了解儲值方案\n\n"
                        f"📋 你的推薦碼：{code}\n"
                        f"推薦好友：雙方各 +6 小時")
                    sb().table("members").update({"warned_15min": True}).eq("user_id", m["user_id"]).eq("tenant_id", TENANT_ID).execute()
                except Exception as e:
                    print(f"[Trial Warn Error] {m['user_id']}: {e}", flush=True)
    except Exception as e:
        print(f"[Trial Poll Error] {e}", flush=True)

print(f"[app] loaded pid={os.getpid()}", flush=True)

if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
