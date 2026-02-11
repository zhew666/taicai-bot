import os
import re
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort

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
from groq import Groq

app = Flask(__name__)

# --- 設定區 ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
client = Groq(api_key=GROQ_API_KEY)

# --- 核心邏輯 ---
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
    seed = (lp * pd * day) % 100
    return [f"{max(1, (seed % 50)):02d}", f"{max(1, ((seed + 15) % 50)):02d}", f"{max(1, ((seed + 33) % 50)):02d}"]

def generate_short_analysis(lp, lucky_numbers):
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": "你是一位運勢分析師。"}, {"role": "user", "content": f"靈數{lp}，幸運尾號{lucky_numbers}，給50字指引。"}],
            model="llama-3.1-8b-instant",
        )
        return completion.choices[0].message.content.strip()
    except: return "今日能量穩定，適合小試身手。"

# --- 修正後的 Flex Message 函式 ---

def create_flex_bubble(lp, lucky_numbers, ai_text):
    """生日靈數卡片"""
    bubble_dict = {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "🔮 今日幸運靈數", "weight": "bold", "color": "#FFFFFF"}], "backgroundColor": "#FFD700"},
        "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"您的靈數：{lp}", "size": "xl", "weight": "bold"}, {"type": "text", "text": f"推薦尾號：{', '.join(lucky_numbers)}", "margin": "md"}, {"type": "text", "text": ai_text, "wrap": True, "margin": "lg", "size": "sm"}]}
    }
    return FlexMessage(alt_text="今日幸運報告", contents=FlexContainer.from_json(json.dumps(bubble_dict)))

def create_scratch_off_carousel():
    """【徹底修正】手動建構 Carousel 結構"""
    base_url = request.host_url.rstrip('/')
    # 確保圖片檔名與你上傳的一致
    img_urls = [f"{base_url}/static/price100.png", f"{base_url}/static/price200.png", f"{base_url}/static/price300.png"]
    
    bubbles = []
    for url in img_urls:
        bubbles.append({
            "type": "bubble",
            "hero": {
                "type": "image",
                "url": url,
                "size": "full",
                "aspectRatio": "20:31",
                "aspectMode": "cover",
                "action": {"type": "uri", "uri": url}
            }
        })
    
    # 直接回傳最外層格式
    carousel_obj = {
        "type": "carousel",
        "contents": bubbles
    }
    
    # 這裡使用 FlexContainer 包裝 carousel_obj
    return FlexMessage(
        alt_text="2026刮刮樂全攻略",
        contents=FlexContainer.from_json(json.dumps(carousel_obj))
    )

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
    
    # 關鍵字判斷
    if any(k in user_text for k in ["攻略", "刮刮樂", "2026"]):
        flex_carousel = create_scratch_off_carousel()
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[flex_carousel])
            )
        return

    # 生日格式判斷
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', user_text)
    if match:
        lp = calculate_lp(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        lucky_nums = get_lucky_numbers(lp, calculate_pd(int(match.group(2)), int(match.group(3))), int(match.group(3)))
        flex = create_flex_bubble(lp, lucky_nums, generate_short_analysis(lp, lucky_nums))
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[flex]))
        return

    # 預設導引
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 歡迎！輸入生日(如 1990-01-01)或點選攻略")]))

if __name__ == "__main__":
    app.run()
