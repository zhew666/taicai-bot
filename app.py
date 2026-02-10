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
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Groq 模組
from groq import Groq

app = Flask(__name__)

# --- 設定區 (環境變數) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
client = Groq(api_key=GROQ_API_KEY)

# --- 1. 核心計算邏輯 ---

def calculate_single_digit(n):
    """將數字加總至個位數 (保留 11, 22, 33)"""
    # 這裡確保 11, 22, 33 不會被縮減
    while n > 9 and n not in [11, 22, 33]:
        n = sum(int(d) for d in str(n))
    return n

def calculate_lp(year, month, day):
    """計算生命靈數"""
    # 先個別加總年、月、日，再縮減
    total = sum(int(d) for d in str(year)) + sum(int(d) for d in str(month)) + sum(int(d) for d in str(day))
    return calculate_single_digit(total)

def calculate_pd(month, day):
    """計算個人日數"""
    # 取得台灣時間
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    
    total = sum(int(d) for d in str(month)) + sum(int(d) for d in str(day)) + \
            sum(int(d) for d in str(now.year)) + sum(int(d) for d in str(now.month)) + sum(int(d) for d in str(now.day))
    return calculate_single_digit(total)

def get_lucky_numbers(lp, pd, day):
    """生成3組雙碼"""
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    
    # 遇到大師數計算種子時，先縮減為單數 (11->2) 以利公式計算
    lp_single = lp if lp < 10 else sum(int(d) for d in str(lp))
    pd_single = pd if pd < 10 else sum(int(d) for d in str(pd))
    
    seed = (lp_single * pd_single * (day + now.day)) % 100
    
    # 生成邏輯
    n1 = (seed % 50) 
    n2 = (seed + 15) % 50
    n3 = (seed + 33) % 50
    
    raw_list = [n1, n2, n3]
    final_list = []
    
    for num in raw_list:
        if num == 0: num = 1 # 避免00
        final_list.append(f"{num:02d}")
        
    return final_list

# --- 2. AI 生成與 Flex Message 設計 ---

def generate_short_analysis(lp, lucky_numbers):
    nums_str = ", ".join(lucky_numbers)
    
    # 大師數特殊提示
    master_note = ""
    if lp in [11, 22, 33]:
        master_note = f"注意：此人是稀有的「大師數 {lp}」，請強調其天賦、直覺與使命感。"

    system_prompt = f"""
    你是一位精簡的運勢分析師。
    使用者資料：生命靈數 {lp}，今日幸運尾號 {nums_str}。
    {master_note}
    
    請給出一段約 50-60 字的短評。
    重點放在：今日的能量關鍵字、財運指引。
    風格：正向、神秘、果斷。
    
    嚴格禁止：
    1. 不要重複列出數字。
    2. 不要自我介紹。
    3. 不要任何格式符號。
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
    except Exception:
        return "今日能量流動順暢，直覺將是你最好的指引。財運潛藏在日常細節中，保持專注即可看見機會。"

def create_flex_bubble(lp, lucky_numbers, ai_text):
    """
    製作 LINE Flex Message (卡片) 的 JSON 結構
    """
    
    # 1. 取得今日日期字串 (台灣時間)
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y / %m / %d")

    # 2. 判斷是否為大師數，設定顏色與文字
    is_master = False
    rarity_component = None
    
    if lp in [11, 22, 33]:
        is_master = True
        ball_color = "#6610f2" # 紫色 (大師)
        
        # 設定稀有度文字
        if lp == 11:
            rarity_title = "🌟 大師數 (稀有度約 6%)"
            rarity_desc = "直覺與靈性的先驅"
        elif lp == 22:
            rarity_title = "🌟 大師數 (稀有度約 2%)"
            rarity_desc = "夢想的實踐大師"
        else: # 33
            rarity_title = "🌟 大師數 (稀有度 < 1%)"
            rarity_desc = "無私的療癒導師"
            
        rarity_component = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": rarity_title, "size": "xs", "color": "#6610f2", "weight": "bold", "align": "center"},
                {"type": "text", "text": rarity_desc, "size": "xxs", "color": "#999999", "align": "center", "margin": "xs"}
            ],
            "margin": "md",
            "backgroundColor": "#f3e5f5",
            "cornerRadius": "8px",
            "paddingAll": "8px"
        }
    else:
        ball_color = "#28a745" # 綠色 (一般)

    # 3. 建立 Body 內容列表
    body_contents = []

    # A. 生命靈數區塊 (包含球與標題)
    body_contents.append({
        "type": "box",
        "layout": "horizontal",
        "alignItems": "center",
        "contents": [
            {"type": "text", "text": "生命靈數", "size": "md", "color": "#aaaaaa", "flex": 1},
            {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": str(lp), "color": "#ffffff", "weight": "bold", "align": "center", "gravity": "center", "size": "xl"}],
                "backgroundColor": ball_color, # 使用上面判斷過的顏色
                "cornerRadius": "50px",
                "width": "70px",
                "height": "70px",
                "justifyContent": "center",
                "alignItems": "center",
                "flex": 0
            }
        ],
        "margin": "md"
    })

    # B. 如果是大師數，插入稀有度區塊
    if is_master and rarity_component:
        body_contents.append(rarity_component)

    # C. 分隔線與推薦尾號
    body_contents.append({"type": "separator", "margin": "lg"})
    body_contents.append({
        "type": "text",
        "text": "✨ 推薦尾號",
        "weight": "bold",
        "size": "md",
        "margin": "lg",
        "color": "#333333"
    })

    # D. 紅色球體區塊
    body_contents.append({
        "type": "box",
        "layout": "horizontal",
        "margin": "md",
        "contents": [
            {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": lucky_numbers[0], "color": "#ffffff", "weight": "bold", "align": "center", "gravity": "center", "size": "lg"}],
                "backgroundColor": "#FF4B4B",
                "cornerRadius": "50px",
                "width": "60px",
                "height": "60px",
                "justifyContent": "center",
                "alignItems": "center"
            },
            {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": lucky_numbers[1], "color": "#ffffff", "weight": "bold", "align": "center", "gravity": "center", "size": "lg"}],
                "backgroundColor": "#FF4B4B",
                "cornerRadius": "50px",
                "width": "60px",
                "height": "60px",
                "justifyContent": "center",
                "alignItems": "center",
                "offsetStart": "10px"
            },
            {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": lucky_numbers[2], "color": "#ffffff", "weight": "bold", "align": "center", "gravity": "center", "size": "lg"}],
                "backgroundColor": "#FF4B4B",
                "cornerRadius": "50px",
                "width": "60px",
                "height": "60px",
                "justifyContent": "center",
                "alignItems": "center",
                "offsetStart": "20px"
            }
        ],
        "justifyContent": "center" 
    })

    # E. AI 文字區塊
    body_contents.append({
        "type": "box",
        "layout": "vertical",
        "margin": "xl",
        "contents": [
            {
                "type": "text",
                "text": ai_text,
                "wrap": True,
                "size": "sm",
                "color": "#555555",
                "lineSpacing": "5px"
            }
        ],
        "backgroundColor": "#f0f2f5",
        "cornerRadius": "10px",
        "paddingAll": "12px"
    })

    # 4. 組裝完整 JSON
    bubble_json = {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "🔮 今日幸運靈數",
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "lg"
                },
                # 【新增】日期顯示
                {
                    "type": "text",
                    "text": today_str,
                    "weight": "regular",
                    "color": "#FFFBE6", # 微微淡黃白，增加層次
                    "size": "sm",
                    "margin": "sm"
                }
            ],
            "backgroundColor": "#FFD700",
            "paddingAll": "20px"
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "僅供娛樂參考，不保證中獎",
                    "size": "xs",
                    "color": "#bbbbbb",
                    "align": "center"
                }
            ]
        }
    }
    return FlexMessage(alt_text="您的今日幸運靈數報告", contents=FlexContainer.from_json(json.dumps(bubble_json)))

# --- 3. Webhook 處理 ---

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    # 驗證生日格式
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', user_text)
    
    if match:
        try:
            year = int(match.group(1))
            month = int(match.group(2))
            day = int(match.group(3))
            
            # 1. 計算
            lp = calculate_lp(year, month, day)
            pd = calculate_pd(month, day)
            lucky_numbers = get_lucky_numbers(lp, pd, day)
            
            # 2. AI 生成文字
            ai_text = generate_short_analysis(lp, lucky_numbers)
            
            # 3. 製作 Flex Message
            flex_message = create_flex_bubble(lp, lucky_numbers, ai_text)
            
            # 4. 回覆
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[flex_message]
                    )
                )
        except ValueError:
             with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="日期無效，請檢查月份或日期。")]
                    )
                )
    else:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="請輸入生日格式：YYYY-MM-DD\n例如：1990-05-20")]
                )
            )

if __name__ == "__main__":
    app.run()
