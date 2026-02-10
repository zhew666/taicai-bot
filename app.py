import os
import datetime
import traceback
from flask import Flask, request, abort

# LINE Bot SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

# Groq
from groq import Groq

app = Flask(__name__)

# ==========================================
# 設定區 (已填入你的 Key)
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

# 初始化 LINE Configuration
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 初始化 Groq Client
groq_client = Groq(api_key=GROQ_API_KEY)

# ==========================================
# 核心計算邏輯 (與測試版相同)
# ==========================================

def sum_digits(n, keep_master=True):
    """將數字逐位加總直到變成個位數 (保留大師數)"""
    while n > 9:
        if keep_master and n in [11, 22, 33]:
            return n
        n = sum(int(digit) for digit in str(n))
    return n

def get_single_digit(n):
    """強制縮減為單數字 (用於種子計算)"""
    while n > 9:
        n = sum(int(digit) for digit in str(n))
    return n

def calculate_luck_numbers(birth_str):
    """
    輸入: "YYYY-MM-DD" 字串
    輸出: 字典 (成功) 或 字串 (錯誤訊息)
    """
    # 1. 輸入驗證
    try:
        birth_date = datetime.datetime.strptime(birth_str, "%Y-%m-%d")
        current_year = datetime.datetime.now().year
        if not (1900 <= birth_date.year <= current_year):
            return "請輸入正確年份範圍：1900~現在"
    except ValueError:
        return "請輸入正確格式：YYYY-MM-DD"

    # 取得今天日期 (設定為台灣時間 GMT+8，避免伺服器時區問題)
    tz_offset = datetime.timedelta(hours=8)
    today = datetime.datetime.now(datetime.timezone.utc) + tz_offset

    # 2. 計算 LP
    lp_raw = sum(int(d) for d in birth_str if d.isdigit())
    lp = sum_digits(lp_raw, keep_master=True)
    lp_single = get_single_digit(lp)

    # 3. 計算 PD
    pd_string = f"{birth_date.month}{birth_date.day}{today.year}{today.month}{today.day}"
    pd_raw = sum(int(d) for d in pd_string if d.isdigit())
    pd = sum_digits(pd_raw, keep_master=True)
    pd_single = get_single_digit(pd)

    # 4. 計算種子
    seed = (lp_single * pd_single * (birth_date.day + today.day)) % 100

    # 5. 計算兩碼 3 組
    nums = []
    nums.append(seed % 50)
    nums.append((seed + 10 + lp_single) % 50)
    nums.append((seed + 20 + pd_single) % 50)

    # 碰撞檢查 (差 < 10 則 +10)
    for _ in range(3):
        changed = False
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if abs(nums[i] - nums[j]) < 10:
                    nums[j] = (nums[j] + 10) % 50
                    changed = True
        if not changed:
            break
    
    formatted_two_digits = [f"{n:02d}" for n in nums]

    return {
        "lp": lp,
        "pd": pd,
        "two_digits": formatted_two_digits,
        "single_digit": pd_single
    }

def get_ai_explanation(data):
    """呼叫 Groq API 生成解釋"""
    prompt = f"""你是一位數理顧問，用生命靈數分析數字。回應用繁體中文，總長60-90字，嚴格固定結構：

1. "你的生命靈數是 {data['lp']}，代表 [單詞或短語特質，例如創意、內省]。"
2. "今日個人日數是 {data['pd']}，影響 [單詞或短語能量，例如和諧、行動]。"
3. "推薦今日幸運尾號：兩碼 {', '.join(data['two_digits'])}，單碼 {data['single_digit']}。"
4. "這些尾號對應今日能量強度最高的三組組合。"

嚴格禁止：額外說明、負面、保證中獎、免責、過長、超出結構。"""

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq API Error: {e}")
        return None

# ==========================================
# Flask 路由與 LINE Webhook 處理
# ==========================================

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    calc_result = calculate_luck_numbers(user_msg)
    
    reply_text = ""

    if isinstance(calc_result, str):
        reply_text = calc_result
    else:
        ai_response = get_ai_explanation(calc_result)
        
        if ai_response:
            reply_text = ai_response
        else:
            reply_text = "系統忙碌，請稍後再試 (AI Error)"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
