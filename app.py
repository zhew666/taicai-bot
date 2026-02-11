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
    TextMessage,
    ImageMessage
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

# --- 核心邏輯 (計算與 AI) ---
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
    lp_s = lp if lp < 10 else sum(int(d) for d in str(lp))
    pd_s = pd if pd < 10 else sum(int(d) for d in str(pd))
    seed = (lp_s * pd_s * (day + now.day)) % 100
    return [f"{max(1, (seed % 50)):02d}", f"{max(1, ((seed + 15) % 50)):02d}", f"{max(1, ((seed + 33) % 50)):02d}"]

def generate_short_analysis(lp, lucky_numbers):
    nums_str = ", ".join(lucky_numbers)
    system_prompt = f"你是一位精簡的運勢分析師。使用者：生命靈數 {lp}，今日幸運尾號 {nums_str}。請給出50-60字短評。嚴禁Markdown。"
    try:
        completion = client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": "請指引"}],
            model="llama-3.1-8b-instant",
        )
        return completion.choices[0].message.content.strip()
    except: return "今日能量流動順暢，直覺將是你最好的指引。"

# --- 復原漂亮的 Flex Message ---
def create_flex_bubble(lp, lucky_numbers, ai_text):
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y / %m / %d")
    is_master = lp in [11, 22, 33]
    ball_color = "#6610f2" if is_master else "#28a745"
    
    bubble_json = {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": "🔮 今日幸運靈數報告", "weight": "bold", "color": "#FFFFFF", "size": "lg"},
                {"type": "text", "text": today_str, "color": "#FFFBE6", "size": "sm", "margin": "sm"}
            ], "backgroundColor": "#FFD700", "paddingAll": "20px"
        },
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "您的生命靈數", "size": "md", "color": "#aaaaaa", "gravity": "center"},
                        {
                            "type": "box", "layout": "vertical", "contents": [{"type": "text", "text": str(lp), "color": "#ffffff", "weight": "bold", "size": "xl", "align": "center"}],
                            "backgroundColor": ball_color, "cornerRadius": "50px", "width": "60px", "height": "60px", "justifyContent": "center"
                        }
                    ], "justifyContent": "space-between"
                },
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "✨ 推薦今日尾號", "weight": "bold", "margin": "lg"},
                {
                    "type": "box", "layout": "horizontal", "margin": "md", "spacing": "md", "contents": [
                        {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": n, "color": "#ffffff", "weight": "bold", "align": "center"}], "backgroundColor": "#FF4B4B", "cornerRadius": "50px", "paddingAll": "10px", "flex": 1} for n in lucky_numbers
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "margin": "xl", "paddingAll": "12px", "backgroundColor": "#f0f2f5", "cornerRadius": "10px",
                    "contents": [{"type": "text", "text": ai_text, "wrap": True, "size": "sm", "color": "#555555"}]
                }
            ]
        },
        "footer": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "僅供娛樂參考", "size": "xs", "color": "#bbbbbb", "align": "center"}]}
    }
    return FlexMessage(alt_text="今日幸運報告", contents=FlexContainer.from_json(json.dumps(bubble_json)))

# --- Webhook 處理 ---
@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    # 1. 直接發送圖片訊息 (解決點進去是網頁的問題)
    if any(k in user_text for k in ["攻略", "刮刮樂", "2026"]):
        base_url = request.host_url.rstrip('/')
        img1 = f"{base_url}/static/price100.png"
        img2 = f"{base_url}/static/price200.png"
        img3 = f"{base_url}/static/price300.png"
        
        # 直接回傳三張 ImageMessage
        images = [
            ImageMessage(original_content_url=img1, preview_image_url=img1),
            ImageMessage(original_content_url=img2, preview_image_url=img2),
            ImageMessage(original_content_url=img3, preview_image_url=img3)
        ]
        
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=images))
        return

    # 2. 漂亮版生日靈數
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', user_text)
    if match:
        lp = calculate_lp(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        lucky_nums = get_lucky_numbers(lp, calculate_pd(int(match.group(2)), int(match.group(3))), int(match.group(3)))
        ai_text = generate_short_analysis(lp, lucky_nums)
        flex = create_flex_bubble(lp, lucky_nums, ai_text)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[flex]))
        return

    # 3. 導引
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔮 輸入生日(如 1990-01-01)獲取幸運靈數，或輸入「攻略」查看刮刮樂分析。")]))

if __name__ == "__main__":
    app.run()
