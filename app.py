# -*- coding: utf-8 -*-
"""
百家之眼 LINE Bot Server
功能：跟隨系統 / 空投系統 / 仙人指路 / 會員系統（試用+推薦碼）
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
_sb_url = os.environ["SUPABASE_URL"]
_sb_key = os.environ["SUPABASE_KEY"]
_thread_local = threading.local()

def sb():
    """每個 thread 使用獨立的 Supabase client，避免多 thread 共用同一 httpx socket"""
    if not hasattr(_thread_local, 'client'):
        _thread_local.client = create_client(_sb_url, _sb_key)
    return _thread_local.client

import httpx as _httpx

REGISTER_URL    = os.environ.get("REGISTER_URL", "gw55.GW1688.NET")
ADMIN_USER_ID   = os.environ.get("ADMIN_USER_ID", "")
ADMIN_REF_CODE  = os.environ.get("ADMIN_REF_CODE", "")
TG_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_GW_CHAT_IDS  = [x.strip() for x in os.environ.get("TELEGRAM_GW_CHAT_IDS", "").split(",") if x.strip()]
TRIAL_HOURS    = 1
WARN_MINUTES   = 15
GW_TIERS       = {5000: 7, 10000: 31}  # 儲值金額 → 天數
ALL_TABLES   = [f"BAG{i:02d}" for i in range(1, 14)] + ["BAG03A", "TEST01"]
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
_poll_stats  = {"count": 0, "airdrop_triggers": 0, "last_trigger": None}  # poll 健康監控
_push_lock   = threading.Lock()
_last_push   = 0  # 上次 push 的時間戳
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

def is_maintenance() -> bool:
    """從 DB 讀取維護模式狀態"""
    try:
        r = sb().table("system_config").select("value").eq("key", "maintenance_mode").execute()
        return r.data[0]["value"] == "true" if r.data else False
    except Exception:
        return False

def set_maintenance(on: bool):
    """寫入維護模式狀態到 DB"""
    sb().table("system_config").upsert({"key": "maintenance_mode", "value": "true" if on else "false"}).execute()

def is_test_mode() -> bool:
    """從 DB 讀取測試模式狀態"""
    try:
        r = sb().table("system_config").select("value").eq("key", "test_mode").execute()
        return r.data[0]["value"] == "true" if r.data else False
    except Exception:
        return False

def set_test_mode(on: bool):
    """寫入測試模式狀態到 DB"""
    sb().table("system_config").upsert({"key": "test_mode", "value": "true" if on else "false"}).execute()
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
    return table_id.replace("BAG", "").lstrip("0") or table_id

_CN_NUM = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,
            "八":8,"九":9,"十":10,"十一":11,"十二":12,"十三":13}

def normalize_table(text: str):
    t = text.strip()
    # 去掉常見多餘字
    for c in ("廳","號","桌","台","第","厅"):
        t = t.replace(c, "")
    t = t.strip()
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
    global _last_push
    for attempt in range(3):
        # 全局限速：每次 push 間隔至少 0.15 秒
        with _push_lock:
            now = time.time()
            gap = now - _last_push
            if gap < 0.15:
                time.sleep(0.15 - gap)
            _last_push = time.time()
        try:
            with ApiClient(config) as api:
                MessagingApi(api).push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=text)]))
            return
        except Exception as e:
            err_str = str(e)
            if "429" in err_str:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                print(f"[push_text] 429 限速，等待 {wait}s 重試", flush=True)
                time.sleep(wait)
            elif attempt == 2:
                print(f"[push_text] 失敗 3 次放棄: {e}", flush=True)
            else:
                time.sleep(1)

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
            else:
                time.sleep(1)

def get_latest_hand(table_id: str):
    r = (sb().table("baccarat_hands").select("*")
           .eq("table_id", table_id)
           .order("shoe", desc=True).order("hand_num", desc=True)
           .limit(1).execute())
    return r.data[0] if r.data else None

def get_all_latest_hands() -> dict:
    """查 latest_hands View 取得每桌最新一手，回傳 {table_id: row}"""
    rows = sb().table("latest_hands").select("*").execute().data
    return {row["table_id"]: row for row in rows}

def format_hand(row: dict) -> str:
    """回傳單則訊息：EV在前（置頂通知可見莊閒），牌面結果在後"""
    p = " ".join(str(row.get(f"p{i}","-")) for i in range(1,4) if row.get(f"p{i}"))
    b = " ".join(str(row.get(f"b{i}","-")) for i in range(1,4) if row.get(f"b{i}"))
    tid    = tnum(row['table_id'])
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

    return "\n".join([
        f"第{tid}廳{dealer_str} | 下一手EV",
        f"  莊：{ev_str(row.get('ev_banker'))}  閒：{ev_str(row.get('ev_player'))}",
        f"  超六：{ev_str(row.get('ev_super6'))}  對子：{ev_str(pair_ev)}",
        f"  和：{ev_str(row.get('ev_tie'))}",
        f"──────────",
        f"靴{shoe} | 第{hand}手結果",
        f"閒牌：{p}",
        f"莊牌：{b}",
    ])

# ── 會員系統 ──────────────────────────────────────────────
def gen_referral_code() -> str:
    chars = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "REF-" + "".join(random.choices(chars, k=4))
        if not sb().table("members").select("user_id").eq("referral_code", code).execute().data:
            return code
    return "REF-" + "".join(random.choices(chars, k=6))

def get_or_create_member(user_id: str) -> dict:
    r = sb().table("members").select("*").eq("user_id", user_id).execute()
    if r.data:
        return r.data[0]
    now = datetime.now(timezone.utc)
    member = {
        "user_id":       user_id,
        "trial_start":   None,
        "expire_at":     None,
        "is_member":     False,
        "referral_code": gen_referral_code(),
        "referred_by":   None,
        "warned_15min":  False,
    }
    sb().table("members").insert(member).execute()
    return member

def activate_trial(user_id: str, member: dict) -> dict:
    """首次使用功能時才啟動試用倒數"""
    if member.get("trial_start") or member.get("is_member"):
        return member
    now = datetime.now(timezone.utc)
    updates = {
        "trial_start": now.isoformat(),
        "expire_at": (now + timedelta(hours=TRIAL_HOURS)).isoformat(),
    }
    sb().table("members").update(updates).eq("user_id", user_id).execute()
    member.update(updates)
    return member

def is_allowed(member: dict) -> bool:
    if member.get("is_member"):
        return True
    if not member.get("trial_start"):
        return True  # 尚未啟動試用，允許使用（會在功能內啟動）
    exp = member.get("expire_at")
    if not exp:
        return False
    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) < exp_dt

def expired_reply(token: str, member: dict):
    code = member.get("referral_code", "N/A")
    reply_text(token,
        f"⏰ 試用已結束\n\n"
        f"想繼續使用百家之眼嗎？\n"
        f"回覆「繼續」即可了解方案\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📋 你的推薦碼：{code}\n"
        f"分享給好友，每人可獲得額外試用時間")

def cmd_continue_info(user_id, token, member):
    """用戶回覆「繼續」，推送 GW 儲值引導"""
    has_account = member.get("gw_account")
    if has_account:
        reply_text(token,
            f"前往金盈匯儲值點數\n"
            f"👉 gw55.GW1688.NET\n\n"
            f"💰 點數可直接用來玩金盈匯平台上的遊戲\n"
            f"儲值後即可同時開通百家之眼使用權\n\n"
                        f"💡 儲值 5,000 點 → 7 天\n"
            f"💡 儲值 10,000 點 → 31 天\n\n"
            f"儲值完成後回來輸入「確認儲值」")
    else:
        reply_text(token,
            f"前往金盈匯註冊並儲值點數\n"
            f"👉 gw55.GW1688.NET\n\n"
            f"💰 點數可直接用來玩金盈匯平台上的遊戲\n"
            f"儲值後即可同時開通百家之眼使用權\n\n"
                        f"💡 儲值 5,000 點 → 7 天\n"
            f"💡 儲值 10,000 點 → 31 天\n\n"
            f"註冊完成後，輸入「綁定帳號」綁定\n"
            f"儲值完成後輸入「確認儲值」")

def cmd_confirm_deposit(user_id, token, member):
    """用戶輸入「確認儲值」，通知 GW 客服"""
    account = member.get("gw_account")
    if not account:
        reply_text(token,
            "尚未綁定金盈匯帳號\n\n"
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
    sb().table("members").update({"gw_status": "pending"}).eq("user_id", user_id).execute()
    reply_text(token,
        f"📋 帳號：{account}\n"
        f"已通知客服確認您的最新儲值\n"
        f"確認後將自動延長使用期限\n\n"
                f"💡 儲值 5,000 點 → 7 天\n"
        f"💡 儲值 10,000 點 → 31 天")
    # 通知 GW 客服（Telegram）
    tg_notify_gw(
        f"📋 儲值待確認\n"
        f"━━━━━━━━━━━━━━\n"
        f"金盈匯帳號：{account}\n\n"
        f"請確認儲值後回覆：\n"
        f"  確認 {account} <金額>\n\n"
        f"未通過請回覆：\n"
        f"  未通過 {account}")

# ── 指令處理 ──────────────────────────────────────────────
def cmd_follow(user_id, token, text, member):
    member = activate_trial(user_id, member)
    if not is_allowed(member):
        expired_reply(token, member); return
    tid = normalize_table(text[2:].strip())

    # 沒帶廳號：如果正在跟隨就關閉，否則顯示引導
    if not tid or tid not in ALL_TABLES:
        with follow_lock:
            if user_id in following:
                old_tid = following.pop(user_id)["table_id"]
                reply_text(token, f"👁 已停止跟隨第{tnum(old_tid)}廳"); return
        reply_text(token,
            "👁 請選擇要跟隨的廳號\n"
            "━━━━━━━━━━━━━━\n"
            "輸入：跟隨 X廳（1~13）\n\n"
            "例如：\n"
            "  跟隨 3廳 → 鎖定第3廳\n"
            "  跟隨 7廳 → 鎖定第7廳\n\n"
            "📡 支援場館：MT 13 廳"); return

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
    member = activate_trial(user_id, member)
    if not is_allowed(member):
        expired_reply(token, member); return

    # 開關機制：已開啟就關閉
    with airdrop_lock:
        if user_id in airdrop:
            airdrop.pop(user_id)
            reply_text(token, "🪂 空投監控已關閉"); return

    import re
    m = re.search(r'(\d+)', text)
    hours = max(1, min(3, int(m.group(1)))) if m else 1
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    with airdrop_lock:
        airdrop[user_id] = {"expire_at": exp, "notified": {}, "push_count": 0}

    # 即時狀態快照
    exp_tw = exp.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
    try:
        fresh = sb().table("latest_hands").select("table_id," + ",".join(EV_FIELDS)).execute().data or []
    except Exception:
        fresh = []
    real = [r for r in fresh if r["table_id"] in ALL_TABLES and r["table_id"] != "TEST01"]
    active = len(real)
    pos = sum(1 for r in real if any(r.get(f) and r[f] > 0 for f in EV_FIELDS))

    lines = [
        "🪂 空投監控已開啟",
        "━━━━━━━━━━━━━━",
        "",
        f"監控廳數：{active} 廳",
        f"目前正EV：{pos} 廳",
        f"到期時間：{exp_tw}",
        "",
        "偵測到正EV時立即推播通知",
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
    member = activate_trial(user_id, member)
    if not is_allowed(member):
        expired_reply(token, member); return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
    try:
        fresh_rows = sb().table("latest_hands").select("*").gte("created_at", cutoff).execute().data
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
    if best_val > 0:
        msg = (f"🧙 仙人指路 第{t}廳{d_str}\n"
               f"第{next_hand}手 {label} EV={best_val:+.4f} ✅\n"
               f"正EV機會，可考慮出手")
    else:
        msg = (f"🧙 仙人指路 第{t}廳{d_str}\n"
               f"第{next_hand}手\n"
               f"目前最佳選項：{label} EV={best_val:+.4f}\n"
               f"靴牌進行中，持續監控")
    reply_text(token, msg)

def cmd_my_code(user_id, token, member):
    code = member.get("referral_code", "N/A")
    exp  = member.get("expire_at", "")
    if exp:
        exp_dt  = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        exp_str = exp_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    else:
        exp_str = "永久"
    reply_text(token,
        f"📋 你的推薦碼：{code}\n"
        f"使用期限：{exp_str}\n\n"
        f"推薦好友使用推薦碼：每人 +1 天\n"
        f"好友完成正式註冊：+7 天")

def cmd_intro(user_id, token, member):
    code = member.get("referral_code", "N/A")
    exp  = member.get("expire_at", "")
    is_m = member.get("is_member", False)

    if is_m:
        status = "✅ 正式會員（永久使用）"
    elif exp:
        exp_dt  = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        exp_str = exp_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
        remaining = exp_dt - datetime.now(timezone.utc)
        mins = int(remaining.total_seconds() / 60)
        days = mins // 1440
        hours = (mins % 1440) // 60
        if mins <= 0:
            status = "⏰ 試用已結束"
        elif days >= 7:
            status = f"✅ 使用期限至 {exp_str}（剩餘約 {days} 天）"
        elif days >= 1:
            status = f"⏳ 剩餘約 {days} 天（到期：{exp_str}）"
        elif hours >= 1:
            status = f"⏳ 試用中，剩餘約 {hours} 小時（到期：{exp_str}）"
        else:
            status = f"⏳ 試用中，剩餘約 {mins} 分鐘（到期：{exp_str}）"
    else:
        status = "⏰ 試用已結束"

    reply_text(token,
        f"📋 帳號狀態：\n"
        f"{status}\n"
        f"📡 支援場館：MT 13 廳\n\n"
        f"🔗 你的專屬推薦碼：{code}\n"
        f"・每邀請 1 人試用 → +1 天\n"
        f"・好友完成正式註冊 → +7 天\n\n"
        f"正式註冊：{REGISTER_URL}\n\n"
        f"💡 輸入「指令」查詢更多功能")

def get_agent(user_id: str):
    """查詢 agents 表，回傳 agent row 或 None"""
    try:
        r = sb().table("agents").select("*").eq("agent_id", user_id).eq("is_active", True).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None

def is_admin(user_id: str) -> bool:
    if ADMIN_USER_ID and user_id == ADMIN_USER_ID:
        return True
    if ADMIN_REF_CODE:
        codes = [c.strip().upper() for c in ADMIN_REF_CODE.split(",") if c.strip()]
        r = sb().table("members").select("user_id").in_("referral_code", codes).execute()
        if any(row["user_id"] == user_id for row in (r.data or [])):
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
        r = sb().table("members").select("*").eq("referral_code", target.upper()).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).execute()

    if not r.data:
        reply_text(token, f"找不到成員：{target}"); return

    target_member = r.data[0]
    target_uid    = target_member["user_id"]

    if target_member.get("is_member"):
        reply_text(token, f"⚠️ {target_uid} 已是正式會員"); return

    # 開通正式會員
    sb().table("members").update({"is_member": True, "expire_at": None}).eq("user_id", target_uid).execute()
    # 記錄轉換事件
    try:
        sb().table("referral_events").insert({
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

    # 推薦人 +7 天
    referrer_uid = target_member.get("referred_by")
    if referrer_uid:
        try:
            rr = sb().table("members").select("expire_at").eq("user_id", referrer_uid).execute()
            if rr.data:
                exp = rr.data[0].get("expire_at")
                base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
                new_exp = (base + timedelta(days=7)).isoformat()
                sb().table("members").update({"expire_at": new_exp}).eq("user_id", referrer_uid).execute()
                # 記錄推薦人獎勵事件
                try:
                    sb().table("referral_events").insert({
                        "referrer_id": referrer_uid,
                        "referee_id": target_uid,
                        "code_used": target_member.get("referral_code", ""),
                        "code_type": "referral",
                        "event_type": "referral_bonus",
                        "bonus_given_hours": 168,
                    }).execute()
                except Exception as e:
                    print(f"[Admin] 記錄推薦獎勵事件失敗: {e}", flush=True)
                push_text(referrer_uid, "🎉 你推薦的好友完成正式註冊，使用期限 +7 天！")
                reply_text(token, f"✅ 推薦人 {referrer_uid} 已 +7 天")
        except Exception as e:
            print(f"[Admin] 推薦人加天失敗: {e}", flush=True)

def cmd_admin_extend(user_id, token, text):
    """延長 <user_id或REF碼> <天數>"""
    if not is_admin(user_id):
        reply_text(token, "❌ 無權限"); return
    parts = text.strip().split()
    if len(parts) < 2:
        reply_text(token, "格式：延長 <user_id或REF碼> <天數>"); return
    target = parts[1]
    try:
        days = int(parts[2]) if len(parts) >= 3 else 7
    except ValueError:
        reply_text(token, "天數請輸入數字"); return

    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).execute()

    if not r.data:
        reply_text(token, f"找不到成員：{target}"); return

    m = r.data[0]
    uid = m["user_id"]
    exp = m.get("expire_at")
    base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + timedelta(days=days)).isoformat()
    sb().table("members").update({"expire_at": new_exp}).eq("user_id", uid).execute()

    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    reply_text(token, f"✅ {uid} 延長 {days} 天\n新到期：{new_exp_tw}")
    try:
        push_text(uid, f"🎉 你的使用期限已延長 {days} 天！新到期時間：{new_exp_tw}")
    except: pass

def cmd_admin_query(user_id, token, text):
    """查詢 REF-XXXX → 顯示該用戶的下線裂變數據"""
    target = text[2:].strip()
    if not target:
        reply_text(token, "格式：查詢 REF-XXXX"); return

    # 找目標用戶
    if target.upper().startswith("REF-"):
        r = sb().table("members").select("*").eq("referral_code", target.upper()).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).execute()
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
            children = sb().table("members").select("user_id,is_member,expire_at,referral_code").eq("referred_by", uid).execute().data or []
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
    direct = sb().table("members").select("user_id").eq("referred_by", target_uid).execute().data or []
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
        r = sb().table("members").select("*").eq("referral_code", target.upper()).execute()
    else:
        r = sb().table("members").select("*").eq("user_id", target).execute()
    if not r.data:
        reply_text(token, f"找不到：{target}"); return

    m = r.data[0]
    target_uid = m["user_id"]
    target_code = m.get("referral_code", "?")

    # 設為正式會員 + 永久（expire_at=null）
    sb().table("members").update({
        "is_member": True,
        "expire_at": None,
    }).eq("user_id", target_uid).execute()

    # 同步到 agents 表
    try:
        sb().table("agents").upsert({
            "agent_id": target_uid,
            "is_active": True,
            "max_extend_days": 31,
        }).execute()
    except Exception as e:
        print(f"[SetAgent] agents 表寫入失敗: {e}", flush=True)

    reply_text(token, f"✅ {target_code} 已設為代理（永久使用）")
    try:
        push_text(target_uid, "🎉 你的帳號已升級為代理，永久使用所有功能！")
    except: pass

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
        r = sb().table("members").select("*").eq("referral_code", target_ref).execute()
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
    r = sb().table("members").select("*").eq("user_id", target_uid).execute()
    if not r.data:
        reply_text(token, f"❌ 找不到用戶 {target_ref}"); return True

    m = r.data[0]
    exp = m.get("expire_at")
    base = max(datetime.fromisoformat(exp.replace("Z", "+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + timedelta(days=days)).isoformat()
    sb().table("members").update({"expire_at": new_exp}).eq("user_id", target_uid).execute()

    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    reply_text(token, f"✅ 已延長 {target_ref} {days} 天\n新到期：{new_exp_tw}")
    try:
        push_text(target_uid, f"🎉 你的使用期限已延長 {days} 天！新到期時間：{new_exp_tw}")
    except: pass
    return True

# ── GW 金盈匯相關 ──────────────────────────────────────────
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
                f"如需再次延長，請在金盈匯儲值點數後\n"
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
        "請輸入您在金盈匯的帳號：\n"
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
        f"{msg}請輸入新的金盈匯帳號：\n"
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
    existing = sb().table("members").select("user_id").eq("gw_account", account).execute()
    if existing.data and existing.data[0]["user_id"] != user_id:
        reply_text(token, "⚠️ 此帳號已被其他用戶綁定，請確認後再試")
        return True
    sb().table("members").update({
        "gw_account": account,
        "gw_status": "pending"
    }).eq("user_id", user_id).execute()
    reply_text(token,
        f"✅ 已記錄您的金盈匯帳號：{account}\n"
        f"━━━━━━━━━━━━━━\n"
        f"已通知客服進行審核\n"
        f"審核通過後將自動延長使用期限\n\n"
                f"💡 儲值 5,000 點 → 7 天\n"
        f"💡 儲值 10,000 點 → 31 天\n\n"
        f"請耐心等候審核結果\n"
        f"填錯了？→ 輸入「更換帳號」")
    # 通知 GW 客服審核（Telegram）
    tg_notify_gw(
        f"📋 新帳號待審核\n"
        f"━━━━━━━━━━━━━━\n"
        f"金盈匯帳號：{account}\n\n"
        f"請確認註冊及儲值後回覆：\n"
        f"  確認 {account} <金額>\n\n"
        f"未通過請回覆：\n"
        f"  未通過 {account}")
    return True

def cmd_gw_status(user_id, token, member):
    """用戶查詢審核狀態"""
    account = member.get("gw_account")
    if not account:
        reply_text(token,
            "尚未綁定金盈匯帳號\n\n"
            "請先前往註冊：gw55.GW1688.NET\n"
            "註冊後輸入「綁定帳號」綁定")
        return
    status = member.get("gw_status", "none")
    status_label = {"none": "未提交", "pending": "審核中", "verified": "已通過", "rejected": "未通過"}.get(status, status)
    reply_text(token,
        f"📋 金盈匯帳號：{account}\n"
        f"審核狀態：{status_label}\n"
        f"━━━━━━━━━━━━━━\n"
                f"💡 儲值 5,000 點 → 7 天\n"
        f"💡 儲值 10,000 點 → 31 天")

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
    days = GW_TIERS.get(amount)
    if not days:
        tiers_str = "、".join(f"{k}→{v}天" for k, v in sorted(GW_TIERS.items()))
        return f"金額不在方案內\n可用方案：{tiers_str}"
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    m = r.data[0]
    target_uid = m["user_id"]
    exp = m.get("expire_at")
    base = max(datetime.fromisoformat(exp.replace("Z", "+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + timedelta(days=days)).isoformat()
    sb().table("members").update({
        "expire_at": new_exp,
        "gw_status": "verified"
    }).eq("user_id", target_uid).execute()
    try:
        sb().table("gw_deposits").insert({
            "user_id": target_uid,
            "gw_account": account,
            "amount": amount,
            "days_granted": days,
            "verified_by": verified_by,
        }).execute()
    except Exception as e:
        print(f"[GW] 記錄儲值失敗: {e}", flush=True)
    new_exp_tw = datetime.fromisoformat(new_exp).astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    try:
        push_text(target_uid,
            f"🎉 帳號驗證通過！\n"
            f"━━━━━━━━━━━━━━\n"
            f"儲值點數：{amount:,}\n"
            f"使用期限延長 {days} 天\n"
            f"新到期時間：{new_exp_tw}\n\n"
            f"感謝使用百家之眼！")
    except: pass
    return f"✅ 已確認 {account}\n儲值 {amount} 點 → 延長 {days} 天\n新到期：{new_exp_tw}"

def _do_gw_reject(text: str) -> str:
    """純邏輯：拒絕，回傳結果文字。"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：未通過 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    sb().table("members").update({"gw_status": "rejected"}).eq("user_id", target_uid).execute()
    try:
        push_text(target_uid,
            "❌ 帳號驗證未通過\n"
            "━━━━━━━━━━━━━━\n"
            "請確認是否已完成註冊及儲值\n\n"
            "註冊網址：gw55.GW1688.NET\n"
            "完成後請重新輸入「確認儲值」")
    except: pass
    return f"✅ 已標記 {account} 未通過"

def _do_gw_not_deposited(text: str) -> str:
    """客服回報：有帳號但未儲值"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：未儲值 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            "📋 帳號已確認，但尚未儲值\n"
            "━━━━━━━━━━━━━━\n"
            "請先前往金盈匯儲值點數\n"
            "👉 gw55.GW1688.NET\n\n"
            "💡 儲值 5,000 點 → 7 天\n"
            "💡 儲值 10,000 點 → 31 天\n\n"
            "儲值完成後回來輸入「確認儲值」")
    except: pass
    return f"✅ 已通知 {account} 用戶尚未儲值"

def _do_gw_not_found(text: str) -> str:
    """客服回報：查無此帳號"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：查無 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    sb().table("members").update({"gw_status": "rejected"}).eq("user_id", target_uid).execute()
    try:
        push_text(target_uid,
            "❌ 查無此帳號\n"
            "━━━━━━━━━━━━━━\n"
            "金盈匯查無您綁定的帳號\n"
            "請確認帳號是否正確\n\n"
            "如需重新綁定，請輸入「綁定帳號」")
    except: pass
    return f"✅ 已通知 {account} 用戶查無此帳號"

def _do_gw_ask_cs(text: str) -> str:
    """客服回報：請用戶聯繫金盈匯客服"""
    parts = text.strip().split()
    if len(parts) < 2:
        return "格式：請詢問 <帳號>"
    account = parts[1]
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            "📋 請聯繫金盈匯客服\n"
            "━━━━━━━━━━━━━━\n"
            "您的帳號需要由金盈匯客服協助處理\n"
            "請直接聯繫金盈匯線上客服\n\n"
            "👉 gw55.GW1688.NET")
    except: pass
    return f"✅ 已通知 {account} 用戶聯繫金盈匯客服"

def _do_gw_reply(text: str) -> str:
    """客服自訂回覆"""
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return "格式：回覆 <帳號> <訊息內容>"
    account = parts[1]
    message = parts[2]
    r = sb().table("members").select("*").eq("gw_account", account).execute()
    if not r.data:
        return f"找不到綁定帳號 {account} 的用戶"
    target_uid = r.data[0]["user_id"]
    try:
        push_text(target_uid,
            f"📋 金盈匯客服回覆\n"
            f"━━━━━━━━━━━━━━\n"
            f"{message}")
    except: pass
    return f"✅ 已傳送自訂訊息給 {account}"

def get_promo_code(code_str: str):
    """從 DB 查詢活動碼，回傳 row dict 或 None"""
    r = (sb().table("promo_codes").select("*")
           .eq("code", code_str.lower())
           .eq("is_active", True)
           .execute())
    if not r.data:
        return None
    row = r.data[0]
    if row.get("max_uses") and row.get("used_count", 0) >= row["max_uses"]:
        return None
    return row

def _has_used(user_id: str, code_type: str) -> bool:
    """檢查用戶是否已使用過某類型的碼（promo 或 referral）"""
    r = (sb().table("referral_events").select("id")
           .eq("referee_id", user_id)
           .eq("code_type", code_type)
           .limit(1).execute())
    return bool(r.data)

def cmd_enter_code(user_id, token, text, member):
    import re
    # 清理輸入：去掉前綴、空格、冒號，取得純碼
    raw = text.replace("好友推薦碼", "").replace("好友推荐码", "").replace("推薦碼", "").replace("推荐码", "").strip().strip(":").strip()
    # 去掉 REF- 前綴後查活動碼
    code_clean = re.sub(r'^REF-', '', raw, flags=re.IGNORECASE).strip()
    promo = get_promo_code(code_clean)
    if promo:
        # 每人限用一次活動碼（查 referral_events）
        if _has_used(user_id, "promo"):
            reply_text(token, "⚠️ 每人限用一次活動碼"); return
        bonus_hours = promo["bonus_hours"]
        exp = member.get("expire_at")
        base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
        new_exp = (base + timedelta(hours=bonus_hours)).isoformat()
        sb().table("members").update({
            "expire_at": new_exp
        }).eq("user_id", user_id).execute()
        # 更新使用次數
        sb().table("promo_codes").update({
            "used_count": promo.get("used_count", 0) + 1
        }).eq("code", code_clean.lower()).execute()
        # 記錄推薦事件
        sb().table("referral_events").insert({
            "referee_id": user_id,
            "code_used": code_clean.lower(),
            "code_type": "promo",
            "event_type": "used_promo",
            "bonus_given_hours": bonus_hours,
        }).execute()
        days = bonus_hours // 24
        label = f"+{days} 天" if bonus_hours >= 24 else f"+{bonus_hours} 小時"
        reply_text(token, f"✅ 活動碼兌換成功！使用期限 {label} 🎁"); return

    # 查推薦碼：自動補 REF- 前綴
    code_upper = code_clean.upper()
    if not code_upper.startswith("REF-"):
        code_upper = "REF-" + code_upper
    r_check = sb().table("members").select("user_id,expire_at").eq("referral_code", code_upper).execute()
    if not r_check.data:
        reply_text(token, "找不到這個碼，請確認後再試"); return
    code = code_upper
    # 每人限用一次推薦碼（查 referral_events）
    if _has_used(user_id, "referral"):
        reply_text(token, "⚠️ 每人限用一次推薦碼"); return
    referrer = r_check.data[0]
    if referrer["user_id"] == user_id:
        reply_text(token, "不能輸入自己的推薦碼"); return
    # 更新被推薦人 + 試用延長到 6 小時
    trial_start = member.get("trial_start")
    if trial_start:
        ts = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
        new_user_exp = (ts + timedelta(hours=6)).isoformat()
    else:
        new_user_exp = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    sb().table("members").update({
        "referred_by": referrer["user_id"],
        "expire_at": new_user_exp
    }).eq("user_id", user_id).execute()
    # 推薦人 +1 天
    exp = referrer["expire_at"]
    base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
    new_exp = (base + timedelta(days=1)).isoformat()
    sb().table("members").update({"expire_at": new_exp}).eq("user_id", referrer["user_id"]).execute()
    # 記錄推薦事件
    sb().table("referral_events").insert({
        "referrer_id": referrer["user_id"],
        "referee_id": user_id,
        "code_used": code,
        "code_type": "referral",
        "event_type": "trial_started",
        "bonus_given_hours": 24,
    }).execute()
    reply_text(token, "✅ 推薦碼輸入成功！試用時間已延長至 6 小時 🎁")
    try:
        push_text(referrer["user_id"], "🎉 有好友使用你的推薦碼，使用期限 +1 天！")
    except: pass

def cmd_migrate(user_id, token, text, member):
    """解析舊系統轉移訊息，自動設定用戶效期"""
    import re
    TW = timezone(timedelta(hours=8))
    MIGRATE_MAX = datetime(2026, 6, 1, tzinfo=TW)

    # 已轉移過
    if member.get("referred_by") and str(member["referred_by"]).startswith("MIGRATE_"):
        exp = member.get("expire_at")
        if exp:
            exp_str = datetime.fromisoformat(exp.replace("Z","+00:00")).astimezone(TW).strftime("%Y-%m-%d %H:%M")
        else:
            exp_str = "永久"
        reply_text(token, f"你已完成轉移，目前效期：{exp_str}\n如有問題請聯繫管理員。")
        return

    # 解析效期
    m_exp = re.search(r'效期\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', text)
    if not m_exp:
        reply_text(token, "無法解析效期，請確認格式正確後重新貼上。")
        return
    try:
        exp_tw = datetime.strptime(m_exp.group(1).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW)
        exp_utc = exp_tw.astimezone(timezone.utc)
    except ValueError:
        reply_text(token, "日期格式錯誤，請確認後重新貼上。")
        return

    now = datetime.now(timezone.utc)
    if exp_utc <= now:
        reply_text(token, "此效期已過期，無法轉移。如有疑問請聯繫管理員。")
        return
    if exp_tw > MIGRATE_MAX:
        reply_text(token,
            "⚠️ 效期資料與舊系統紀錄不符\n\n"
            "系統已記錄此次異常操作。\n"
            "若持續提交不實資料，將取消使用資格。\n\n"
            "如有疑問請聯繫管理員。")
        return

    # 解析推薦碼（舊渠道碼）
    m_ref = re.search(r'推薦碼\s*(\S+)', text)
    old_channel = m_ref.group(1).strip() if m_ref else "unknown"

    # 寫入 DB
    sb().table("members").update({
        "expire_at": exp_utc.isoformat(),
        "referred_by": f"MIGRATE_{old_channel}",
    }).eq("user_id", user_id).execute()

    # 記錄轉移事件
    try:
        sb().table("referral_events").insert({
            "referee_id": user_id,
            "code_used": old_channel,
            "code_type": "migration",
            "event_type": "system_migrate",
            "bonus_given_hours": 0,
        }).execute()
    except Exception as e:
        print(f"[Migrate] 記錄事件失敗: {e}", flush=True)

    exp_display = exp_tw.strftime("%Y-%m-%d %H:%M")
    my_code = member.get("referral_code", "N/A")
    reply_text(token,
        f"✅ 轉移成功！\n"
        f"━━━━━━━━━━━━━━\n"
        f"舊系統渠道：{old_channel}\n"
        f"使用效期：{exp_display}\n"
        f"你的新推薦碼：{my_code}\n"
        f"━━━━━━━━━━━━━━\n"
        f"所有功能已開放，輸入「說明」查看指令。")

def cmd_ev_intro(user_id, token):
    reply_text(token,
        "📊 什麼是 EV（期望值）？\n"
        "━━━━━━━━━━━━━━\n\n"
        "EV = Expected Value，\n"
        "代表每一注的長期平均報酬。\n\n"
        "EV > 0 → 這注長期有利可圖\n"
        "EV < 0 → 這注長期會虧\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂通常莊閒 EV 都是負的，\n"
        "但隨著牌靴消耗，偶爾會出現\n"
        "EV 翻正的瞬間 — 這就是出手時機。\n\n"
        "百家之眼替你即時計算每張桌的 EV，\n"
        "在正EV出現時第一時間通知你。\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "如果還是難以理解，直接輸入「仙人指路」\n"
        "系統會從十幾個遊戲廳中，\n"
        "即時選出當下最佳的投注選項給你。\n"
        "這是目前全網最強的百家樂輔助功能。")

def cmd_card_intro(user_id, token):
    reply_text(token,
        "🃏 百家之眼怎麼算？\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂用 8 副牌（416 張），\n"
        "每發一手牌，剩餘牌組就會改變。\n\n"
        "我們的系統：\n"
        "1️⃣ 即時記錄已出的每一張牌\n"
        "2️⃣ 根據剩餘牌組，窮舉所有可能\n"
        "3️⃣ 計算莊/閒/和/超六/對子的EV\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "跟 21 點算牌同理：\n"
        "已出的牌會影響後續的機率分佈。\n\n"
        "差別是我們用電腦完整計算，\n"
        "不是靠人腦估算。\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "如果還是難以理解，直接輸入「仙人指路」\n"
        "系統會從十幾個遊戲廳中，\n"
        "即時選出當下最佳的投注選項給你。\n"
        "這是目前全網最強的百家樂輔助功能。")

def cmd_feature_intro(user_id, token):
    # 第一則：功能與使用方式
    reply_text(token,
        "百家之眼 ── 功能介紹\n"
        "━━━━━━━━━━━━━━\n\n"
        "即時監控 MT 全 13 廳百家樂，\n"
        "8 副牌完整追蹤，計算 6 種注區 EV：\n"
        "莊 / 閒 / 和 / 超級六 / 閒對 / 莊對\n\n"
        "▸ 仙人指路\n"
        "  一鍵掃描全廳，列出目前最值得關注的投注。\n"
        "  → 點選單「仙人指路」\n\n"
        "▸ 空投掃描\n"
        "  開啟後，正 EV 出現立刻推播通知你。\n"
        "  → 點選單「空投掃描」\n\n"
        "▸ 跟隨桌台\n"
        "  鎖定單一廳口，即時推送每手牌面與 EV。\n"
        "  → 輸入「跟隨 3廳」（1~13廳）\n\n"
        "▸ EV 與算牌原理\n"
        "  → 輸入「EV介紹」或「算牌介紹」\n\n"
        "━━━━━━━━━━━━━━\n"
        "💡 不知道 EV 是什麼也沒關係，\n"
        "先試「仙人指路」，看到正 EV 就是出手訊號。")
    # 第二則：關於我們 + 推薦機制
    push_text(user_id,
        "關於百家之眼\n"
        "━━━━━━━━━━━━━━\n\n"
        "百家樂不是純運氣的遊戲。\n"
        "8 副牌、416 張牌，每發一張牌，\n"
        "剩餘牌組的機率結構就在改變。\n\n"
        "百家之眼做的事很單純：\n"
        "把這些數學算好，即時告訴你。\n\n"
        "我們不賣牌路、不帶單、不保證贏，\n"
        "只提供透明的數據，讓你自己判斷。\n\n"
        "━━━━━━━━━━━━━━\n"
        "🎁 推薦好友計畫\n\n"
        "把你的專屬推薦碼分享給朋友：\n"
        "  ✦ 每推薦 1 人加入 → 你得 1 天使用權\n"
        "  ✦ 好友正式註冊 → 你再得 7 天\n\n"
        "→ 輸入「我的推薦碼」查看你的推薦碼\n"
        "→ 輸入「指令」查看所有指令")

# ── Webhook ──────────────────────────────────────────────
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
        tg_send(chat_id, f"歡迎使用百家之眼 GW 客服系統\n\n你的 Chat ID：{chat_id}\n\n請將此 ID 提供給管理員完成設定")
        return "OK"
    # 驗證是否為 GW 客服
    if chat_id not in TG_GW_CHAT_IDS:
        tg_send(chat_id, f"⚠️ 無權限\n你的 Chat ID：{chat_id}\n請聯繫管理員開通")
        return "OK"
    # 處理指令
    CMD_HELP = ("📋 百家之眼 GW 客服指令\n"
                "━━━━━━━━━━━━━━\n\n"
                "確認 <帳號> <金額>\n→ 儲值確認，自動延長期限\n\n"
                "未儲值 <帳號>\n→ 帳號存在但尚未儲值\n\n"
                "查無 <帳號>\n→ 查無此帳號\n\n"
                "未通過 <帳號>\n→ 驗證未通過\n\n"
                "請詢問 <帳號>\n→ 請用戶聯繫金盈匯客服\n\n"
                "回覆 <帳號> <訊息>\n→ 自訂回覆內容\n\n"
                "範例：確認 abc123 5000")
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
            "延長 <user_id或REF碼> <天數>\n"
            "→ 延長使用期限\n\n"
            "管理員指令\n"
            "→ 顯示本列表\n\n"
            "維護開 / 維護關\n"
            "→ 開關維護模式\n\n"
            "測試開 / 測試關\n"
            "→ 測試模式（只有管理員能操作和收到推送）\n\n"
            "查詢 REF-XXXX\n"
            "→ 查看該用戶下線裂變數據\n\n"
            "設代理 REF-XXXX\n"
            "→ 設為代理（永久使用）"
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
    if text.startswith("查詢") or text.startswith("查询"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_query(user_id, token, text); return
    if text.startswith("設代理") or text.startswith("设代理"):
        if not is_admin(user_id):
            reply_text(token, "❌ 無權限"); return
        cmd_admin_set_agent(user_id, token, text); return
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

    # 二段式 GW 帳號綁定（捕捉用戶回覆）
    if user_id in _pending_bind:
        if cmd_bind_gw_capture(user_id, token, text): return

    # 舊系統轉移（偵測特徵格式）
    if "效期" in text and "推薦碼" in text:
        cmd_migrate(user_id, token, text, member); return

    # 維護模式：非管理員直接不回應（LINE 自動回覆處理）
    if is_maintenance() and not is_admin(user_id):
        return

    # 測試模式：只有管理員能操作
    if is_test_mode() and not is_admin(user_id):
        return

    # 新用戶歡迎訊息（不吃 CD）
    if not member.get("welcomed"):
        try:
            sb().table("members").update({"welcomed": True}).eq("user_id", user_id).execute()
            reply_text(token,
                "歡迎加入【百家之眼】\n"
                "━━━━━━━━━━━━━━\n\n"
                "我們是一群用數據打牌的人。\n\n"
                "百家之眼即時追蹤 8 副牌、計算每一手的\n"
                "期望值（EV），在機率站到你這邊時通知你。\n\n"
                "不靠感覺，靠數學。\n\n"
                "━━━━━━━━━━━━━━\n"
                "🎯 馬上試試 → 點選單「仙人指路」\n"
                "📖 想了解更多 → 輸入「功能介紹」\n"
                "🎁 有推薦碼 → 輸入「好友推薦碼 REF-XXXX」")
            return
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
        reply_text(token, "💬 加入百家之眼聊天室\n\n👉 https://line.me/ti/g/ddjjpjznQL"); return
    if text in ("說明", "说明", "help", "指令", "Help", "HELP"):
        reply_text(token,
            "🃏 百家之眼 指令說明\n"
            "📡 支援場館：MT 13 廳\n"
            "━━━━━━━━━━━━━━\n\n"
            "🪂 空投 X\n"
            "→ 開啟全廳掃描 X 小時（1~3），\n"
            "　任一桌出現正EV立刻通知\n\n"
            "👁 跟隨 X廳\n"
            "→ 鎖定某張桌即時跟蹤，\n"
            "　每手推送牌面+EV\n\n"
            "🧙 仙人指路\n"
            "→ 一鍵查詢全廳最高EV桌台\n\n"
            "🛑 停止\n"
            "→ 停止跟隨/空投\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📋 我的推薦碼 → 查推薦碼與期限\n"
            "🎁 好友推薦碼 REF-XXXX → 輸入推薦碼\n"
            "🔗 綁定帳號 → 綁定金盈匯帳號\n"
            "💰 確認儲值 → 儲值後通知客服確認\n"
            "📊 審核狀態 → 查詢帳號審核進度\n"
            "📊 EV介紹 → EV期望值是什麼？\n"
            "🃏 算牌介紹 → 我們怎麼計算？\n"
            "📖 介紹 → 帳號狀態與說明\n"
            "💬 聊天室 → 進群交流\n\n"
            "💡 所有指令送出後，請稍等 5 秒再進行操作")
        return

    # ── 需要 CD 的指令（查全廳 / 寫入狀態）──
    if not check_cooldown(user_id):
        return

    if any(text.startswith(k) for k in ("跟隨","跟随","追隨","追蹤","監控")):
        body = text[2:].strip()
        cmd_follow(user_id, token, "跟隨" + body, member)
    elif text.startswith("空投") or text.startswith("開始空投"):
        cmd_airdrop(user_id, token, text, member)
    elif text in ("停止", "結束", "stop", "Stop", "STOP"):
        cmd_stop(user_id, token)
    elif "仙人指路" in text:
        cmd_guide(user_id, token, member)
    elif text.startswith("好友推薦碼") or text.startswith("好友推荐码"):
        cmd_enter_code(user_id, token, text, member)
    elif re.match(r'^[A-Za-z0-9\-]{4,10}$', text):
        # 4~10 碼英數字（含 REF-XXXX），自動當推薦碼/活動碼處理
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
            ca = row.get("created_at", "")
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
        time.sleep(5)
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

        # 試用到期警告：每 60 秒檢查一次
        now_ts = time.time()
        if now_ts - _last_trial_check >= 60:
            _last_trial_check = now_ts
            _poll_trial_warnings()

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
            print(f"[Follow] {tid} shoe={cur_shoe} hand={cur_hand} last={last_shoe}/{last_hand}", flush=True)

            if last_shoe is not None and cur_shoe != last_shoe:
                push_text(user_id, f"🔄 第{tnum(tid)}廳 換靴，跟隨已停止")
                with follow_lock:
                    following.pop(user_id, None)
                continue

            if last_shoe is None:
                with follow_lock:
                    if user_id in following:
                        following[user_id]["last_shoe"] = cur_shoe
                        following[user_id]["last_hand"] = cur_hand
                print(f"[Follow] 首次連線，push 確認給 {user_id}", flush=True)
                push_text(user_id, f"✅ 已開始跟隨第{tnum(tid)}廳\n每手新牌即時推送，換靴自動停止\n再次輸入「跟隨」可手動停止")
                push_text(user_id, format_hand(row))
                print(f"[Follow] push 完成", flush=True)
                continue

            if cur_hand > last_hand:
                new_rows = (sb().table("baccarat_hands").select("*")
                              .eq("table_id", tid).eq("shoe", cur_shoe)
                              .gt("hand_num", last_hand).order("hand_num").execute()).data
                for i, r in enumerate(new_rows):
                    if i > 0:
                        time.sleep(0.3)
                    push_text(user_id, format_hand(r))
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
    # 獨立查 positive_ev_now View，只取有正 EV 的桌
    try:
        pos_rows = sb().table("positive_ev_now").select("*").execute().data
        pos_hands = {row["table_id"]: row for row in pos_rows
                     if row["table_id"] in ALL_TABLES}
    except Exception as e:
        print(f"[Airdrop] 查 positive_ev_now 失敗: {e}", flush=True)
        pos_hands = {}
    active_tables = len(latest_hands)
    for user_id, state in users.items():
        # 測試模式：跳過非管理員
        if _test_mode and not is_admin(user_id):
            continue
        try:
            if now > state["expire_at"]:
                cnt = state.get("push_count", 0)
                if cnt > 0:
                    end_msg = f"🪂 空投監控已結束\n本次共捕獲 {cnt} 次 +EV 空投\n\n輸入「空投」可再次啟動"
                else:
                    end_msg = "🪂 空投監控已結束\n本次監控期間未偵測到 +EV\n\n輸入「空投」可再次啟動"
                push_text(user_id, end_msg)
                with airdrop_lock:
                    airdrop.pop(user_id, None)
                continue

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
                    lines = [f"🪂 +EV空投 第{tnum(tid)}廳{d_str}", f"第{next_hand}手"]
                    for label, val in sorted(pos, key=lambda x: -x[1]):
                        lines.append(f"{label}EV：{val:+.4f} ✅")
                    push_text(user_id, "\n".join(lines))
        except Exception as e:
            print(f"[Airdrop Error] {user_id}: {e}", flush=True)

def _poll_trial_warnings():
    now = datetime.now(timezone.utc)
    warn_threshold = now + timedelta(minutes=WARN_MINUTES)
    try:
        r = (sb().table("members").select("user_id,expire_at,referral_code")
               .eq("is_member", False).eq("warned_15min", False).execute())
        for m in (r.data or []):
            exp = m.get("expire_at")
            if not exp: continue
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if now < exp_dt <= warn_threshold:
                code = m.get("referral_code", "N/A")
                try:
                    push_text(m["user_id"],
                        f"⏰ 試用即將結束（剩餘 15 分鐘）\n\n"
                        f"想繼續使用百家之眼嗎？\n"
                        f"回覆「繼續」即可了解方案\n\n"
                        f"📋 你的推薦碼：{code}\n"
                        f"分享給好友也能獲得額外時間")
                    sb().table("members").update({"warned_15min": True}).eq("user_id", m["user_id"]).execute()
                except Exception as e:
                    print(f"[Trial Warn Error] {m['user_id']}: {e}", flush=True)
    except Exception as e:
        print(f"[Trial Poll Error] {e}", flush=True)

print(f"[app] loaded pid={os.getpid()}", flush=True)

if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
