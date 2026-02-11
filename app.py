import os
import re
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort

# LINE SDK v3 模組
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    FlexMessage,
    FlexContainer,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Groq 模組
from groq import Groq

app = Flask(__name__)

# --- 設定區 ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
client = Groq(api_key=GROQ_API_KEY)

# --- 核心邏輯 (靈數計算等) ---
def calculate_single_digit(n):
    while n > 9 and n not in [11, 22, 33]:
        n = sum(int(d) for d in str(n))
    return n

def calculate_lp(year, month, day):
    total = sum(int(d) for d in str(year)) + sum(int(d) for d in str(month)) + sum(int(d) for d in str(day))
    return calculate_single_digit(total)

def calculate_pd(month, day):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    total = sum(int(d) for d in str(month)) + sum(int(d) for d in str(day)) + \
            sum(int(d) for d in str(now.year)) + sum(int(d) for d in str(now.month)) + sum(int(d) for d in str(now.day))
    return calculate_single_digit(total)

def get_lucky_numbers(lp, pd, day):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    lp_single = lp if lp < 10 else sum(int(d) for d in str(lp))
    pd_single = pd if pd < 10 else sum(int(d) for d in str(pd))
    seed = (lp_single * pd_single * (day + now.day)) % 100
    n1, n2, n3 = (seed % 50), (seed + 15) % 50, (seed + 33) % 50
    return [f"{max(1, n1):02d}", f"{max(1, n2):02d}", f"{max(1, n3):02d}"]

def generate_short_analysis(lp, lucky_numbers):
    nums_str = ", ".join(lucky_numbers)
    system_prompt = f"你是一位精簡的運勢分析師。使用者：生命靈數 {lp}，今日幸運尾號 {nums_str}。請給出約50-60字指引。嚴禁Markdown。"
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": "請指引"}],
            model="llama-3.1-8b-instant",
            temperature=0.7, max_tokens=300,
        )
        return completion.choices[0].message.content.strip()
    except: return "今日能量流動順暢，直覺將是你最好的指引。"

# --- Flex Message 設計 ---

def create_flex_bubble(lp, lucky_numbers, ai_text):
    # 此部分結構維持不變
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y / %m / %d")
    ball_color = "#6610f2" if lp in [11, 22, 33] else "#28a745"
    
    bubble_json = {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🔮 今日幸運靈數", "weight": "bold", "color": "#FFFFFF", "size": "lg"}, {"type": "text", "text": today_str, "color": "#FFFBE6", "size": "sm", "margin": "sm"}], "backgroundColor": "#FFD700", "paddingAll": "20px"},
        "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": str(lp), "weight": "bold", "size": "xl", "align": "center"}]}, # 簡化版
        "footer": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "僅供娛樂參考", "size": "xs", "color": "#bbbbbb", "align": "center"}]}
    }
    return FlexMessage(alt_text="今日幸運報告", contents=FlexContainer.from_json(json.dumps(bubble_json)))

def create_scratch_off_carousel():
    """修正後的輪播圖結構"""
    base_url = request.host_url.rstrip('/')
    img_urls = [f"{base_url}/static/price100.png", f"{base_url}/static/price200.png", f"{base_url}/static/price300.png"]
    
    bubbles = []
    for url in img_urls:
        bubbles.append({
            "type": "bubble",
            "size": "giga",
            "hero": {
                "type": "image",
                "url": url,
                "size": "full",
                "aspectRatio": "20:31",
                "aspectMode": "cover",
                "action": {"type": "uri", "uri": url}
            }
        })
    
    # 這裡必須回傳完整的 carousel 結構
    carousel_json = {
        "type": "carousel",
        "contents": bubbles
    }
    return FlexMessage(alt_text="2026刮刮樂全攻略", contents=FlexContainer.from_json(json.dumps(carousel_json)))

# --- Webhook 處理 ---

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    # 1. 攻略關鍵字
    if any(k in user_text for k in ["攻略", "刮刮樂", "2026"]):
        try:
            reply_msg = create_scratch_off_carousel()
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg]))
        except Exception as e:
            print(f"Error sending carousel: {e}")
        return

    # 2. 生日計算
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', user_text)
    if match:
        lp = calculate_lp(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        lucky_nums = get_lucky_numbers(lp, calculate_pd(int(match.group(2)), int(match.group(3))), int(match.group(3)))
        flex = create_flex_bubble(lp, lucky_nums, generate_short_analysis(lp, lucky_nums))
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[flex]))
        return

    # 3. 預設回覆
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 歡迎！請輸入生日 (如 1990-01-01) 或點選「刮刮樂攻略」")]))

if __name__ == "__main__":
    app.run()
