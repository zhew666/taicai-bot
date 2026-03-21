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

REGISTER_URL    = os.environ.get("REGISTER_URL", "（請洽管理員）")
ADMIN_USER_ID   = os.environ.get("ADMIN_USER_ID", "")
ADMIN_REF_CODE  = os.environ.get("ADMIN_REF_CODE", "")
TRIAL_HOURS    = 1
WARN_MINUTES   = 15
ALL_TABLES   = [f"BAG{i:02d}" for i in range(1, 14)] + ["BAG03A"]
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
_poll_stats  = {"count": 0, "airdrop_triggers": 0, "last_trigger": None}  # poll 健康監控
_push_lock   = threading.Lock()
_last_push   = 0  # 上次 push 的時間戳

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
        f"分享你的推薦碼給好友，每人試用 +1 天：\n"
        f"📋 {code}\n\n"
        f"正式註冊：{REGISTER_URL}")

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
                              "started_at": time.time(), "remaining": 10}
    reply_text(token, f"⏳ 正在連線第{tnum(tid)}廳（接下來 10 手）")

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
        airdrop[user_id] = {"expire_at": exp, "notified": {}, "last_status": None}

    # 即時狀態快照
    exp_tw = exp.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
    try:
        fresh = sb().table("latest_hands").select("table_id," + ",".join(EV_FIELDS)).execute().data or []
    except Exception:
        fresh = []
    active = len(fresh)
    pos = sum(1 for r in fresh if any(r.get(f) and r[f] > 0 for f in EV_FIELDS))

    lines = [
        "🪂 空投監控已開啟",
        "━━━━━━━━━━━━━━",
        "",
        f"監控廳數：{active} 廳",
        f"目前正EV：{pos} 廳",
        f"到期時間：{exp_tw}",
        "",
        "偵測到正EV時立即推播通知",
        "每 30 分鐘自動回報監控狀態",
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

def cmd_enter_code(user_id, token, text, member):
    import re
    # 清理輸入：去掉前綴、空格、冒號，取得純碼
    raw = text.replace("好友推薦碼", "").replace("好友推荐码", "").replace("推薦碼", "").replace("推荐码", "").strip().strip(":").strip()
    # 去掉 REF- 前綴後查活動碼
    code_clean = re.sub(r'^REF-', '', raw, flags=re.IGNORECASE).strip()
    promo = get_promo_code(code_clean)
    if promo:
        if member.get("referred_by"):
            reply_text(token, "你已經使用過推薦碼了"); return
        bonus_hours = promo["bonus_hours"]
        exp = member.get("expire_at")
        base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
        new_exp = (base + timedelta(hours=bonus_hours)).isoformat()
        sb().table("members").update({
            "referred_by": f"PROMO_{code_clean.upper()}",
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
            "code_type": promo.get("type", "promo"),
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
    if member.get("referred_by"):
        reply_text(token, "你已經輸入過推薦碼了"); return
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
            "→ 開關維護模式"
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

    # 舊系統轉移（偵測特徵格式）
    if "效期" in text and "推薦碼" in text:
        cmd_migrate(user_id, token, text, member); return

    # 維護模式：非管理員直接不回應（LINE 自動回覆處理）
    if is_maintenance() and not is_admin(user_id):
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

        # 試用到期警告：每 60 秒檢查一次
        now_ts = time.time()
        if now_ts - _last_trial_check >= 60:
            _last_trial_check = now_ts
            _poll_trial_warnings()

def _poll_following(latest_hands: dict):
    with follow_lock:
        users = dict(following)
    for user_id, state in users.items():
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
                push_text(user_id, f"✅ 已開始跟隨第{tnum(tid)}廳（接下來 10 手）")
                push_text(user_id, format_hand(row))
                # 首手算 1 手
                with follow_lock:
                    if user_id in following:
                        following[user_id]["remaining"] = following[user_id].get("remaining", 10) - 1
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
                    # 扣手數
                    with follow_lock:
                        if user_id in following:
                            following[user_id]["remaining"] = following[user_id].get("remaining", 10) - 1
                            if following[user_id]["remaining"] <= 0:
                                following.pop(user_id)
                                push_text(user_id, f"👁 第{tnum(tid)}廳 10 手已結束\n再次輸入「跟隨 {tnum(tid)}廳」繼續")
                                break
                with follow_lock:
                    if user_id in following:
                        following[user_id]["last_shoe"] = cur_shoe
                        following[user_id]["last_hand"] = cur_hand
        except Exception as e:
            print(f"[Follow Error] {user_id}: {e}", flush=True)

def _poll_airdrop(latest_hands: dict):
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
        pos_hands = {row["table_id"]: row for row in pos_rows}
    except Exception as e:
        print(f"[Airdrop] 查 positive_ev_now 失敗: {e}", flush=True)
        pos_hands = {}
    active_tables = len(latest_hands)
    for user_id, state in users.items():
        try:
            if now > state["expire_at"]:
                push_text(user_id, "🪂 空投監控時間已結束")
                with airdrop_lock:
                    airdrop.pop(user_id, None)
                continue

            # 30 分鐘定期狀態回報
            last_status = state.get("last_status")
            if last_status is None or (now - last_status).total_seconds() >= 1800:
                with airdrop_lock:
                    if user_id in airdrop:
                        airdrop[user_id]["last_status"] = now
                remain = state["expire_at"] - now
                remain_min = max(0, int(remain.total_seconds() // 60))
                pos_count = len(pos_hands)
                exp_tw = state["expire_at"].astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
                status_lines = [
                    "🪂 空投狀態回報",
                    "━━━━━━━━━━━━━━",
                    f"監控廳數：{active_tables} 廳",
                    f"目前正EV：{pos_count} 廳",
                    f"剩餘時間：{remain_min} 分鐘（{exp_tw} 到期）",
                ]
                if pos_count > 0:
                    status_lines.append("")
                    for tid, row in pos_hands.items():
                        pf = [(EV_LABELS[f], row[f]) for f in EV_FIELDS if row.get(f) and row[f] > 0]
                        if pf:
                            best = max(pf, key=lambda x: x[1])
                            status_lines.append(f"  🟢 第{tnum(tid)}廳 {best[0]} {best[1]:+.4f}")
                else:
                    status_lines.append("\n暫無正EV，持續監控中")
                push_text(user_id, "\n".join(status_lines))

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
                        f"⏰ 試用剩餘 15 分鐘\n\n"
                        f"分享推薦碼給好友，每人使用 +1 天：\n"
                        f"📋 {code}\n\n"
                        f"正式註冊：{REGISTER_URL}")
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
