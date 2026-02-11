import os
import re
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort

# LINE SDK v3
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

# Groq
from groq import Groq

app = Flask(__name__)

# --- 設定區 ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
client = Groq(api_key=GROQ_API_KEY)

# --- 1. 計算邏輯 ---
def calculate_single_digit(n):
    # 保留 11, 22, 33 不加總
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
    # 計算亂數種子
    lp_s = lp if lp < 10 else sum(int(d) for d in str(lp))
    pd_s = pd if pd < 10 else sum(int(d) for d in str(pd))
    seed = (lp_s * pd_s * (day + now.day)) % 100
    
    n1, n2, n3 = (seed % 50), (seed + 15) % 50, (seed + 33) % 50
    return [f"{max(1, n1):02d}", f"{max(1, n2):02d}", f"{max(1, n3):02d}"]

# --- 2. AI 短評 ---
def generate_short_analysis(lp, lucky_numbers):
    nums_str = ", ".join(lucky_numbers)
    # 若是大師數，加入提示
    master_note = ""
    if lp in [11, 22, 33]:
        master_note = f"此人為大師數 {lp}，請強調天賦與直覺。"

    system_prompt = f"""
    你是一位精簡的運勢分析師。使用者資料：生命靈數 {lp}，今日幸運尾號 {nums_str}。
    {master_note}
    請給出一段約 50-60 字的短評。重點：今日能量關鍵字、財運指引。
    風格：正向、神秘、果斷。嚴禁Markdown格式。
    """
    try:
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "請給出今日指引"}
            ],
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=300,
        )
        return completion.choices[0].message.content.strip()
    except:
        return "今日能量流動順暢，直覺將是你最好的指引，請相信自己的判斷。"

# --- 3. 豪華版 Flex Message (復原你的 image_98973e 設計) ---
def create_luxury_flex(lp, lucky_numbers, ai_text):
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y / %m / %d")
    
    # 判斷大師數顏色與標籤
    is_master = False
    rarity_box = None
    ball_color = "#28a745" # 預設綠色

    if lp in [11, 22, 33]:
        is_master = True
        ball_color = "#6610f2" # 大師數紫色
        
        # 設定稀有度文字
        if lp == 11:
            r_title, r_desc = "🌟 大師數 (稀有度約 6%)", "直覺與靈性的先驅"
        elif lp == 22:
            r_title, r_desc = "🌟 大師數 (稀有度約 2%)", "夢想的實踐大師"
        else:
            r_title, r_desc = "🌟 大師數 (稀有度 < 1%)", "無私的療癒導師"
            
        rarity_box = {
            "type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#f3e5f5", "cornerRadius": "8px", "paddingAll": "8px",
            "contents": [
                {"type": "text", "text": r_title, "size": "xs", "color": "#6610f2", "weight": "bold", "align": "center"},
                {"type": "text", "text": r_desc, "size": "xxs", "color": "#999999", "align": "center", "margin": "xs"}
            ]
        }

    # 建立主要內容
    body_contents = []
    
    # 1. 靈數大球
    body_contents.append({
        "type": "box", "layout": "horizontal", "alignItems": "center", "margin": "md",
        "contents": [
            {"type": "text", "text": "生命靈數", "size": "md", "color": "#aaaaaa", "flex": 1},
            {
                "type": "box", "layout": "vertical", "width": "70px", "height": "70px", "backgroundColor": ball_color, "cornerRadius": "35px", "justifyContent": "center", "alignItems": "center", "flex": 0,
                "contents": [{"type": "text", "text": str(lp), "color": "#ffffff", "weight": "bold", "size": "xl"}]
            }
        ]
    })

    # 2. 如果是大師數，加入稀有度方塊
    if is_master and rarity_box:
        body_contents.append(rarity_box)

    # 3. 分隔線與標題
    body_contents.extend([
        {"type": "separator", "margin": "lg"},
        {"type": "text", "text": "✨ 推薦尾號", "weight": "bold", "size": "md", "margin": "lg", "color": "#333333"}
    ])

    # 4. 紅色幸運球 (水平排列)
    lucky_balls = []
    for num in lucky_numbers:
        lucky_balls.append({
            "type": "box", "layout": "vertical", "width": "50px", "height": "50px", "backgroundColor": "#FF4B4B", "cornerRadius": "25px", "justifyContent": "center", "alignItems": "center", "margin": "md",
            "contents": [{"type": "text", "text": num, "color": "#ffffff", "weight": "bold", "size": "lg"}]
        })
    
    body_contents.append({
        "type": "box", "layout": "horizontal", "justifyContent": "center", "margin": "md",
        "contents": lucky_balls
    })

    # 5. AI 分析灰框
    body_contents.append({
        "type": "box", "layout": "vertical", "margin": "xl", "backgroundColor": "#f0f2f5", "cornerRadius": "10px", "paddingAll": "12px",
        "contents": [{"type": "text", "text": ai_text, "wrap": True, "size": "sm", "color": "#555555", "lineSpacing": "5px"}]
    })

    # 組裝最終 JSON
    bubble = {
        "type": "bubble", "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFD700", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": "🔮 今日幸運靈數", "weight": "bold", "color": "#FFFFFF", "size": "lg"},
                {"type": "text", "text": today_str, "color": "#FFFBE6", "size": "sm", "margin": "sm"}
            ]
        },
        "body": {"type": "box", "layout": "vertical", "contents": body_contents},
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [{"type": "text", "text": "僅供娛樂參考，不保證中獎", "size": "xs", "color": "#bbbbbb", "align": "center"}]
        }
    }
    return FlexMessage(alt_text="今日幸運報告", contents=FlexContainer.from_json(json.dumps(bubble)))

# --- 4. Webhook 處理 ---

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
    
    # --- 功能 1: 刮刮樂攻略 (發送 3 張大圖) ---
    if any(k in user_text for k in ["攻略", "刮刮樂", "2026"]):
        base_url = request.host_url.rstrip('/')
        # 確保這些檔名在你的 static 資料夾裡
        img1 = f"{base_url}/static/price100.png"
        img2 = f"{base_url}/static/price200.png"
        img3 = f"{base_url}/static/price300.png"
        
        # 建立 3 張圖片訊息
        image_messages = [
            ImageMessage(original_content_url=img1, preview_image_url=img1),
            ImageMessage(original_content_url=img2, preview_image_url=img2),
            ImageMessage(original_content_url=img3, preview_image_url=img3)
        ]
        
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=image_messages)
            )
        return

    # --- 功能 2: 豪華版生命靈數 ---
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', user_text)
    if match:
        lp = calculate_lp(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        lucky_nums = get_lucky_numbers(lp, calculate_pd(int(match.group(2)), int(match.group(3))), int(match.group(3)))
        ai_text = generate_short_analysis(lp, lucky_nums)
        
        # 呼叫豪華版函式
        flex_msg = create_luxury_flex(lp, lucky_nums, ai_text)
        
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[flex_msg])
            )
        return

    # --- 預設引導 ---
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token, 
                messages=[TextMessage(text="🔮 歡迎使用台彩助手！\n\n輸入生日 (如 1993-01-01) 查看靈數報告。\n輸入「攻略」查看刮刮樂推薦圖片。")]
            )
        )

if __name__ == "__main__":
    app.run()
