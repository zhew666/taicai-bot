# -*- coding: utf-8 -*-
"""
百家之眼 LINE Bot Server
"""
import os
import threading
import time
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
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
config  = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
sb      = create_client(
    os.environ.get('SUPABASE_URL'),
    os.environ.get('SUPABASE_KEY')
)

# ── 跟隨狀態 ──────────────────────────────────────────────
following   = {}   # user_id → {table_id, last_shoe, last_hand}
follow_lock = threading.Lock()

# ── 工具函式 ──────────────────────────────────────────────
def normalize_table(text: str):
    text = text.strip().replace("廳", "").replace("第", "")
    if text.isdigit():
        return f"BAG{int(text):02d}"
    text = text.upper()
    if text.startswith("BAG"):
        return text
    return None

def format_hand(row: dict) -> str:
    table  = row.get("table_id", "")
    shoe   = row.get("shoe", "")
    hand   = row.get("hand_num", "")
    dealer = row.get("dealer", "")
    p1, p2, p3 = row.get("p1","-"), row.get("p2","-"), row.get("p3","-")
    b1, b2, b3 = row.get("b1","-"), row.get("b2","-"), row.get("b3","-")
    player_cards = " ".join(c for c in [p1, p2, p3] if c != "-")
    banker_cards = " ".join(c for c in [b1, b2, b3] if c != "-")

    def ev_line(label, val):
        val  = val or 0
        sign = "+" if val > 0 else ""
        star = " ✅" if val > 0 else ""
        return f"  {label}：{sign}{val:.4f}{star}"

    table_num = table.replace("BAG","").lstrip("0") or table
    return "\n".join([
        f"━━━━━━━━━━━━━━━━",
        f"第{table_num}廳｜靴{shoe} 第{hand}手",
        f"荷官：{dealer}",
        f"閒牌：{player_cards}",
        f"莊牌：{banker_cards}",
        f"── EV ──",
        ev_line("莊",  row.get("ev_banker")),
        ev_line("閒",  row.get("ev_player")),
        ev_line("和",  row.get("ev_tie")),
        ev_line("超六", row.get("ev_super6")),
        ev_line("閒對", row.get("ev_pair_p")),
        ev_line("莊對", row.get("ev_pair_b")),
    ])

def push_text(user_id: str, text: str):
    with ApiClient(config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text)]))

# ── 背景輪詢 ──────────────────────────────────────────────
def poll_loop():
    while True:
        time.sleep(5)
        with follow_lock:
            users = dict(following)

        for user_id, state in users.items():
            table_id  = state["table_id"]
            last_shoe = state["last_shoe"]
            last_hand = state["last_hand"]
            try:
                latest = (sb.table("baccarat_hands")
                            .select("*")
                            .eq("table_id", table_id)
                            .order("shoe",     desc=True)
                            .order("hand_num", desc=True)
                            .limit(1).execute()).data
                if not latest:
                    print(f"[Poll] {table_id} 查無資料，跳過")
                    continue
                row      = latest[0]
                cur_shoe = row["shoe"]
                cur_hand = row["hand_num"]
                print(f"[Poll] {table_id} shoe={cur_shoe} hand={cur_hand} last_shoe={last_shoe} last_hand={last_hand}")

                # 新靴偵測
                if last_shoe is not None and cur_shoe != last_shoe:
                    table_num = table_id.replace("BAG","").lstrip("0")
                    push_text(user_id,
                        f"🔄 第{table_num}廳 新靴開始\n跟隨已停止，輸入「跟隨 X廳」重新開始")
                    with follow_lock:
                        following.pop(user_id, None)
                    continue

                # 首次連線
                if last_shoe is None:
                    with follow_lock:
                        following[user_id]["last_shoe"] = cur_shoe
                        following[user_id]["last_hand"] = cur_hand
                    table_num = table_id.replace("BAG","").lstrip("0")
                    print(f"[Poll] 首次連線 {table_id}，push 確認訊息給 {user_id}")
                    push_text(user_id,
                        f"✅ 已開始跟隨第{table_num}廳\n"
                        f"目前靴{cur_shoe} 第{cur_hand}手，等待下一手...")
                    print(f"[Poll] push 完成")
                    continue

                # 有新手
                if cur_hand > last_hand:
                    new_rows = (sb.table("baccarat_hands")
                                  .select("*")
                                  .eq("table_id", table_id)
                                  .eq("shoe", cur_shoe)
                                  .gt("hand_num", last_hand)
                                  .order("hand_num").execute()).data
                    for r in new_rows:
                        push_text(user_id, format_hand(r))
                    with follow_lock:
                        if user_id in following:
                            following[user_id]["last_shoe"] = cur_shoe
                            following[user_id]["last_hand"] = cur_hand

            except Exception as e:
                print(f"[Poll Error] {user_id}: {e}")

import sys
print(f"[app] module loaded, pid={__import__('os').getpid()}", flush=True)

# ── LINE Webhook ───────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id   = event.source.user_id
    user_text = event.message.text.strip()

    with ApiClient(config) as api_client:
        line_api = MessagingApi(api_client)

        # 跟隨
        if user_text.startswith("跟隨"):
            table_id = normalize_table(user_text[2:].strip())
            if not table_id:
                reply = "格式錯誤，請輸入：跟隨 3廳"
            else:
                with follow_lock:
                    following[user_id] = {"table_id": table_id, "last_shoe": None, "last_hand": 0}
                table_num = table_id.replace("BAG","").lstrip("0")
                reply = f"⏳ 正在連線第{table_num}廳，稍等..."
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token, messages=[TextMessage(text=reply)]))
            return

        # 停止
        if user_text in ("停止", "stop"):
            with follow_lock:
                removed = following.pop(user_id, None)
            reply = "已停止跟隨" if removed else "目前沒有跟隨任何廳"
            line_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token, messages=[TextMessage(text=reply)]))
            return

        # 預設
        reply = (
            "🃏 百家之眼\n"
            "━━━━━━━━━━━━\n"
            "跟隨 X廳  →  開始即時牌面推送\n"
            "停止      →  停止跟隨"
        )
        line_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
