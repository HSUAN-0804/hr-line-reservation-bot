# main.py - 簡化乾淨版：文字 + 貼圖 + 記錄到 GAS

import os
import json
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    StickerMessage,
    TextSendMessage,
)

# -------- OpenAI (新版 SDK) --------
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception as e:
    openai_client = None
    print("OpenAI 初始化失敗，請確認 openai 套件與 OPENAI_API_KEY：", e)

# -------- 基本設定 --------
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise Exception("請設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN 環境變數")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 統一給 GAS 用的 Web App URL（exec）
GAS_LINE_LOG_URL = os.environ.get(
    "GAS_LINE_LOG_URL",
    # ✅ 請把下面這行改成你的 Script exec URL（不想用環境變數就直接寫死在這行）
    # "https://script.google.com/macros/s/XXXXXXXXXXXXXXXXXXXX/exec",
    ""
)

logging.basicConfig(level=logging.INFO)


# ================== 共用：把訊息記錄到 GAS（line_messages） ==================

def log_to_gas_from_event(
    event,
    msg_type: str,
    text: str = "",
    sticker_package_id: str = "",
    sticker_id: str = "",
    sender: str = "user",
):
    """
    把 LINE 的訊息（文字 / 貼圖）丟給 GAS 的 appLineLog
    GAS 那邊的 doPost 會呼叫 appLineLog(body)，寫入 line_messages
    """
    if not GAS_LINE_LOG_URL:
        logging.warning("GAS_LINE_LOG_URL 未設定，略過記錄 log")
        return

    try:
        user_id = event.source.user_id
    except Exception:
        user_id = ""

    # LINE 的 timestamp 是毫秒
    try:
        ts_iso = datetime.fromtimestamp(
            event.timestamp / 1000, tz=timezone.utc
        ).isoformat()
    except Exception:
        ts_iso = datetime.now(timezone.utc).isoformat()

    body = {
        "line_user_id": user_id,
        "type": msg_type,  # 'text' or 'sticker'
        "text": text,
        "sticker_package_id": str(sticker_package_id) if sticker_package_id else "",
        "sticker_id": str(sticker_id) if sticker_id else "",
        "sender": sender,  # user / agent / bot
        "timestamp": ts_iso,
    }

    try:
        resp = requests.post(GAS_LINE_LOG_URL, json=body, timeout=2)
        logging.info("log_to_gas_from_event resp: %s", resp.text[:200])
    except Exception as e:
        logging.error("log_to_gas_from_event error: %s", e)


# ================== OpenAI：產生小潔回覆 ==================

def generate_reply_from_openai(user_text: str, user_id: str = "") -> str:
    """
    呼叫 OpenAI，產生 H.R 燈藝小潔的回覆
    （簡化版，可之後再加店家資料 / Google Sheet 等）
    """
    if not openai_client:
        return "目前暫時無法連線到 AI 伺服器，不好意思 >_<"

    system_prompt = (
        "你是機車精品改裝店「H.R 燈藝」的線上客服「小潔」，"
        "使用者多半是來詢問尾燈、方向燈、排氣管、烤漆、安裝預約等問題。\n"
        "請用「活潑親切但專業」的口吻回覆，使用繁體中文，不要使用 emoji。\n"
        "如果對方問到價格或施工時間，可以先提供大概區間，"
        "並主動詢問車種與想要改裝的項目，讓你再幫忙抓比較準的估價。"
    )

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_text
                },
            ],
            temperature=0.6,
        )
        reply = resp.choices[0].message.content.strip()
        return reply or "這邊暫時想不到怎麼回，可以再多跟我描述一點嗎？"
    except Exception as e:
        logging.error("OpenAI 回覆失敗: %s", e)
        return "目前系統有點忙不過來，我可能晚一點才有辦法幫你詳細回覆 QQ"


# ================== LINE Webhook 入口 ==================

@app.route("/callback", methods=["POST"])
def callback():
    # 取得 X-Line-Signature header
    signature = request.headers.get("X-Line-Signature", "")

    # 取得請求 body
    body = request.get_data(as_text=True)
    logging.info("Request body: " + body)

    # 驗證與處理
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.error("Invalid signature. Check channel access token/secret.")
        abort(400)

    return "OK"


# ================== 事件處理：文字訊息 ==================

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    user_text = event.message.text

    # 1) 呼叫 OpenAI 產生小潔回覆
    reply_text = generate_reply_from_openai(user_text, user_id=user_id)

    # 2) 回覆給使用者（只有文字，不發圖片 / 貼圖）
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        logging.error("回覆文字訊息失敗: %s", e)

    # 3) 把「使用者這句話」記錄到 GAS / line_messages
    log_to_gas_from_event(
        event,
        msg_type="text",
        text=user_text,
        sender="user",
    )

    # （如果你也想記錄小潔的回覆，可再呼叫一次 log_to_gas_from_event，sender="bot"）
    # log_to_gas_from_event(
    #     event,
    #     msg_type="text",
    #     text=reply_text,
    #     sender="bot",
    # )


# ================== 事件處理：貼圖訊息 ==================

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    user_id = event.source.user_id
    package_id = event.message.package_id
    sticker_id = event.message.sticker_id

    # 1) 回覆客人一段文字（不主動發貼圖，以免 400）
    reply_text = "收到你的貼圖～如果方便的話，也可以再打一點文字，讓小潔更好幫你喔！"
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        logging.error("回覆貼圖訊息失敗: %s", e)

    # 2) 把貼圖記錄到 GAS / line_messages
    log_to_gas_from_event(
        event,
        msg_type="sticker",
        text="",
        sticker_package_id=package_id,
        sticker_id=sticker_id,
        sender="user",
    )


# ================== 主程式啟動 ==================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Render / Railway 等都用 0.0.0.0
    app.run(host="0.0.0.0", port=port)
