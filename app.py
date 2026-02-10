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
# 設定區 (讀環境變數，安全，不寫死)
# ==========================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

groq_client = Groq(api_key=GROQ_API_KEY)

# ==========================================
# 核心計算邏輯 (已避免00)
# ==========================================

def sum_digits(n, keep_master=True):
    while n > 9:
        if keep_master and n in [11, 22, 33]:
            return n
        n = sum(int(digit) for digit in str(n))
    return n

def get_single_digit(n):
    while n > 9:
        n = sum(int(digit) for digit in str(n))
    return n

def calculate_luck_numbers(birth_str):
    try:
        birth_date = datetime.datetime.strptime(birth_str, "%Y-%m-%d")
        current_year = datetime.datetime.now().year
        if not (1900 <= birth_date.year <= current_year):
            return "請輸入正確年份範圍：1900~現在"
    except ValueError:
        return "請輸入正確格式：YYYY-MM-DD"

    tz_offset = datetime.timedelta(hours=8)
    today = datetime.datetime.now(datetime.timezone.utc) + tz_offset

    lp_raw = sum(int(d) for d in birth_str if d.isdigit())
    lp = sum_digits(lp_raw, keep_master=True)
    lp_single = get_single_digit(lp)

    pd_string = f"{birth_date.month}{birth_date.day}{today.year}{today.month}{today.day}"
    pd_raw = sum(int(d) for d in pd_string if d.isdigit())
    pd = sum_digits(pd_raw, keep_master=True)
    pd_single = get_single_digit(pd)

    seed = (lp_single * pd_single * (birth_date.day + today.day)) % 100

    nums = []
    nums.append(seed % 50)
    nums.append((seed + 10 + lp_single) % 50)
    nums.append((seed + 20 + pd_single) % 50)

    for _ in range(3):
        changed = False
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if abs(nums[i] - nums[j]) < 10:
                    nums[j] = (nums[j] + 10) % 50
                    changed = True
        if not changed:
            break
    
    formatted_two_digits = []
    for n in nums:
        if n == 0:
            n = 1  # 避免00，改成01
        formatted_two_digits.append(f"{n:02d}")

    single_digit = pd_single
    
    return {
        "lp": lp,
        "pd": pd,
        "two_digits": formatted_two_digits,
        "single_digit": single_digit
    }

def get_ai_explanation(data):
    client = Groq(api_key=GROQ_API_KEY)
    
    prompt = f"""你是一位數理顧問，用生命靈數分析數字。回應用繁體中文，只輸出以下固定格式，不加任何其他文字或解釋：
你的生命靈數是 {data['lp']} 代表 [最多3個形容詞，與個性、人格、人格魅力相關，例如領導魅力、獨立個性、吸引力]
你的今日幸運數字是 {data['two_digits'][0]} 代表 [一個形容詞，與財富、運氣、機運相關，例如財運、好運、機遇]
你的今日幸運數字是 {data['two_digits'][1]} 代表 [一個形容詞，與財富、運氣、機運相關，例如財運、好運、機遇]
你的今日幸運數字是 {data['two_digits'][2]} 代表 [一個形容詞，與財富、運氣、機運相關，例如財運、好運、機遇]

嚴格禁止：任何額外文字、單碼顯示、數字編號、負面、保證中獎、免責、過長、超出格式、形容詞超過指定數量。使用中性、多樣詞彙，避免重複。"""
    
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=100
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"AI 生成失敗: {str(e)}"

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
