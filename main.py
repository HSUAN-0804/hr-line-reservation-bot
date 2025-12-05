import os
import logging
from datetime import datetime

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ===== 環境變數 =====
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# 建議在 Render 的環境變數加一個 GAS_BASE_URL
# 值填： https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec
GAS_BASE_URL        = os.getenv("GAS_BASE_URL") or "https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec"

line_bot_api = LineBotApi(LINE_CHANNEL_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ===== 呼叫 GAS WebApp =====
def gas_post(action, payload):
    if not GAS_BASE_URL:
        raise RuntimeError("GAS_BASE_URL not set")
    params = {"action": action}  # ✅ 用 query string 傳給 GAS 的 doPost(e).parameter.action
    r = requests.post(GAS_BASE_URL, params=params, json=payload, timeout=10)
    app.logger.info(f"POST to GAS: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

# ===== Webhook 入口 =====
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ===== 主事件處理（測試版，只做 log + 回覆一句話） =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    text    = event.message.text.strip()

    # ① 丟一筆 log 給 GAS：lineLog
    try:
        payload = {
            "line_user_id": user_id,
            "text": text,
            "timestamp": datetime.utcnow().isoformat()
        }
        res = gas_post("lineLog", payload)
        app.logger.info(f"lineLog result: {res}")
    except Exception as e:
        app.logger.warning(f"log to GAS error: {e}")

    # ② 回一段簡單訊息給使用者
    reply_text = f"測試版：我有收到你的訊息喔～\n\n你說的是：{text}"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# ===== 啟動 Flask =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000)) or 5000
    app.run(host="0.0.0.0", port=port)
