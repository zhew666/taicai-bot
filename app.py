# -*- coding: utf-8 -*-
"""
百家之眼 LINE Bot Server
功能：跟隨系統 / 空投系統 / 仙人指路 / 會員系統（試用+推薦碼）
"""
import os, threading, time, random, string
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

def normalize_table(text: str):
    t = text.strip().replace("廳", "").replace("第", "")
    if t.upper() in ("3A", "03A"):
        return "BAG03A"
    if t.isdigit():
        return f"BAG{int(t):02d}"
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
            if attempt == 2:
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
    """一次 query 取得所有桌台最新一手，回傳 {table_id: row}"""
    rows = (sb().table("baccarat_hands").select("*")
              .in_("table_id", ALL_TABLES)
              .order("shoe", desc=True)
              .order("hand_num", desc=True)
              .limit(100).execute()).data
    latest = {}
    for row in rows:
        tid = row["table_id"]
        if tid not in latest:
            latest[tid] = row
    return latest

def format_hand(row: dict) -> str:
    """回傳單則訊息：EV在前（置頂通知可見莊閒），牌面結果在後"""
    p = " ".join(str(row.get(f"p{i}","-")) for i in range(1,4) if row.get(f"p{i}"))
    b = " ".join(str(row.get(f"b{i}","-")) for i in range(1,4) if row.get(f"b{i}"))
    tid    = tnum(row['table_id'])
    shoe   = row['shoe']
    hand   = row['hand_num']
    dealer = row.get("dealer", "")

    def ev_str(val):
        if val is None: return "N/A"
        star = " ✅" if val > 0 else ""
        return f"{val:+.4f}{star}"

    pair_ev = max(v for v in [row.get("ev_pair_p"), row.get("ev_pair_b")] if v is not None) \
              if any(row.get(f) is not None for f in ["ev_pair_p","ev_pair_b"]) else None

    return "\n".join([
        f"第{tid}廳｜靴{shoe} | 下一手EV",
        f"  莊：{ev_str(row.get('ev_banker'))}  閒：{ev_str(row.get('ev_player'))}",
        f"  超六：{ev_str(row.get('ev_super6'))}",
        f"  對子：{ev_str(pair_ev)}",
        f"  和：{ev_str(row.get('ev_tie'))}",
        f"──────────",
        f"第{hand}手結果｜荷官：{dealer}",
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
        "trial_start":   now.isoformat(),
        "expire_at":     (now + timedelta(hours=TRIAL_HOURS)).isoformat(),
        "is_member":     False,
        "referral_code": gen_referral_code(),
        "referred_by":   None,
        "warned_15min":  False,
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

def expired_reply(token: str, member: dict):
    code = member.get("referral_code", "N/A")
    reply_text(token,
        f"⏰ 試用已結束\n\n"
        f"分享你的推薦碼給好友，每人試用 +1 天：\n"
        f"📋 {code}\n\n"
        f"正式註冊：{REGISTER_URL}")

# ── 指令處理 ──────────────────────────────────────────────
def cmd_follow(user_id, token, text, member):
    if not is_allowed(member):
        expired_reply(token, member); return
    tid = normalize_table(text[2:].strip())
    if not tid or tid not in ALL_TABLES:
        reply_text(token, "格式：跟隨 X廳（1~13）"); return
    with follow_lock:
        following[user_id] = {"table_id": tid, "last_shoe": None, "last_hand": 0, "started_at": time.time()}
    reply_text(token, f"⏳ 正在連線第{tnum(tid)}廳，稍等...")

def cmd_airdrop(user_id, token, text, member):
    if not is_allowed(member):
        expired_reply(token, member); return
    import re
    m = re.search(r'(\d+)', text)
    hours = max(1, min(3, int(m.group(1)))) if m else 1
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    with airdrop_lock:
        airdrop[user_id] = {"expire_at": exp, "notified": {}}
    reply_text(token, f"🪂 空投監控已開啟（{hours} 小時）\n偵測到任一廳正EV時立即通知")

def cmd_stop(user_id, token):
    removed = []
    with follow_lock:
        if following.pop(user_id, None): removed.append("跟隨")
    with airdrop_lock:
        if airdrop.pop(user_id, None): removed.append("空投")
    reply_text(token, f"已停止：{'、'.join(removed)}" if removed else "目前沒有進行中的監控")

def cmd_guide(user_id, token, member):
    if not is_allowed(member):
        expired_reply(token, member); return
    best_row, best_field, best_val = None, None, -999
    for tid, row in get_all_latest_hands().items():
        for f in EV_FIELDS:
            v = row.get(f)
            if v is not None and v > best_val:
                best_val, best_field, best_row = v, f, row
    if not best_row:
        reply_text(token, "目前無法取得數據，請稍後再試"); return
    label = EV_LABELS.get(best_field, best_field)
    t     = tnum(best_row["table_id"])
    hand  = best_row["hand_num"]
    if best_val > 0:
        msg = f"🧙 仙人指路\n第{t}廳 第{hand}手 ✅\n{label} EV={best_val:+.4f}"
    else:
        msg = f"🧙 仙人指路\n目前無正EV選項，最接近：\n第{t}廳 第{hand}手\n{label} EV={best_val:+.4f}"
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
        if mins > 0:
            status = f"⏳ 試用中，剩餘約 {mins} 分鐘（到期：{exp_str}）"
        else:
            status = f"⏰ 試用已結束"
    else:
        status = "⏰ 試用已結束"

    reply_text(token,
        f"📋 帳號狀態：\n"
        f"{status}\n\n"
        f"🔗 你的專屬推薦碼：{code}\n"
        f"・每邀請 1 人試用 → +1 天\n"
        f"・好友完成正式註冊 → +7 天\n\n"
        f"正式註冊：{REGISTER_URL}")

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

PROMO_CODES = {
    "evpro":   timedelta(days=1),
    "chen972": timedelta(days=1),
}

def cmd_enter_code(user_id, token, text, member):
    import re
    # 特殊活動碼（不限格式，直接比對）
    raw = text.replace("好友推薦碼", "").replace("推薦碼", "").strip().lower().strip(":")
    if raw in PROMO_CODES:
        if member.get("referred_by"):
            reply_text(token, "你已經使用過推薦碼了"); return
        bonus = PROMO_CODES[raw]
        exp = member.get("expire_at")
        base = max(datetime.fromisoformat(exp.replace("Z","+00:00")), datetime.now(timezone.utc)) if exp else datetime.now(timezone.utc)
        new_exp = (base + bonus).isoformat()
        sb().table("members").update({
            "referred_by": f"PROMO_{raw.upper()}",
            "expire_at": new_exp
        }).eq("user_id", user_id).execute()
        reply_text(token, f"✅ 活動碼兌換成功！使用期限 +{bonus.days} 天 🎁"); return

    m = re.search(r'(REF-[A-Z0-9]{4,6})', text.upper())
    if not m:
        reply_text(token, "格式：好友推薦碼 REF-XXXX"); return
    code = m.group(1)
    if member.get("referred_by"):
        reply_text(token, "你已經輸入過推薦碼了"); return
    r = sb().table("members").select("user_id,expire_at").eq("referral_code", code).execute()
    if not r.data:
        reply_text(token, "推薦碼無效，請確認後再試"); return
    referrer = r.data[0]
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
    reply_text(token, "✅ 推薦碼輸入成功！試用時間已延長至 6 小時 🎁")
    try:
        push_text(referrer["user_id"], "🎉 有好友使用你的推薦碼，使用期限 +1 天！")
    except: pass

# ── Webhook ──────────────────────────────────────────────
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
            "→ 顯示本列表"
        ); return
    if text.startswith("開通"):
        cmd_admin_activate(user_id, token, text); return
    if text.startswith("延長"):
        cmd_admin_extend(user_id, token, text); return

    if not check_cooldown(user_id):
        return  # 冷卻中，直接忽略

    if text == "介紹" or "全廳掃描" in text:
        cmd_intro(user_id, token, member)
    elif text.startswith("跟隨"):
        cmd_follow(user_id, token, text, member)
    elif text.startswith("空投") or text.startswith("開始空投"):
        cmd_airdrop(user_id, token, text, member)
    elif text in ("停止", "結束", "stop"):
        cmd_stop(user_id, token)
    elif text == "仙人指路":
        cmd_guide(user_id, token, member)
    elif text in ("我的推薦碼", "推薦碼"):
        cmd_my_code(user_id, token, member)
    elif text.startswith("好友推薦碼"):
        cmd_enter_code(user_id, token, text, member)
    elif text in ("說明", "help", "指令", "Help", "HELP"):
        reply_text(token,
            "🃏 百家之眼 指令\n"
            "━━━━━━━━━━━━━━\n"
            "跟隨 X廳　→ 即時牌面推送\n"
            "空投 X　　→ X小時正EV通知(1~3)\n"
            "仙人指路　→ 查詢當前最高EV\n"
            "停止　　　→ 停止監控\n"
            "我的推薦碼 → 查詢推薦碼與期限\n"
            "好友推薦碼 : REF-XXXX → 輸入好友推薦碼")

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
                push_text(user_id, f"✅ 已開始跟隨第{tnum(tid)}廳｜荷官：{row.get('dealer','')}")
                push_text(user_id, format_hand(row))
                print(f"[Follow] push 完成", flush=True)
                continue

            if cur_hand > last_hand:
                new_rows = (sb().table("baccarat_hands").select("*")
                              .eq("table_id", tid).eq("shoe", cur_shoe)
                              .gt("hand_num", last_hand).order("hand_num").execute()).data
                for r in new_rows:
                    push_text(user_id, format_hand(r))
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
    for user_id, state in users.items():
        try:
            if now > state["expire_at"]:
                push_text(user_id, "🪂 空投監控時間已結束")
                with airdrop_lock:
                    airdrop.pop(user_id, None)
                continue
            for tid, row in latest_hands.items():
                cur_hand = row["hand_num"]
                if cur_hand <= state["notified"].get(tid, 0):
                    continue
                with airdrop_lock:
                    if user_id in airdrop:
                        airdrop[user_id]["notified"][tid] = cur_hand
                pos = [(EV_LABELS[f], row[f]) for f in EV_FIELDS if row.get(f) and row[f] > 0]
                if pos:
                    dealer = row.get("dealer", "")
                    dealer_str = f" 荷官：{dealer} |" if dealer else ""
                    lines = [f"🪂 +EV空投", f"第{tnum(tid)}廳{dealer_str} 第{cur_hand}手"]
                    for label, val in sorted(pos, key=lambda x: -x[1]):
                        lines.append(f"{label}：{val:+.4f} ✅")
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
