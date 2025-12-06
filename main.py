# main.py - 文字 + 貼圖 + 對 GAS 發 {"action":"lineLog","body":...}

import os
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

# ✅ 給 GAS 用的 Web App URL（exec）
# 建議在環境變數 GAS_LINE_LOG_URL 設定，如果懶得設也可以直接寫在預設值
GAS_LINE_LOG_URL = os.environ.get(
    "GAS_LINE_LOG_URL",
    ""  # 例如："https://script.google.com/macros/s/xxxxxxxxxxxxxxxx/exec"
)

logging.basicConfig(level=logging.INFO)


# ================== 共用：把訊息記錄到 GAS（line_messages） ==================

def log_to_gas(body: dict):
    """
    用「你之前有成功寫入的格式」丟給 GAS：
    {
      "action": "lineLog",
      "body": {...}
    }
    GAS 的 doPost 裡應該有類似：
      const data = JSON.parse(e.postData.contents);
      if (data.action === 'lineLog') appLineLog(data.body);
    """
    if not GAS_LINE_LOG_URL:
        logging.warning("GAS_LINE_LOG_URL 未設定，略過記錄 log")
        return

    payload = {
        "action": "lineLog",
        "body": body,
    }

    try:
        resp = requests.post(GAS_LINE_LOG_URL, json=payload, timeout=2)
        logging.info("log_to_gas resp: %s", resp.text[:200])
    except Exception as e:
        logging.error("log_to_gas error: %s", e)


def log_from_event(
    event,
    msg_type: str,
    text: str = "",
    sticker_package_id: str = "",
    sticker_id: str = "",
    sender: str = "user",
):
    """
    把 LINE 事件轉成 appLineLog 需要的 body：
    {
      "line_user_id": "...",
      "type": "text" / "sticker",
      "text": "...",
      "sticker_package_id": "...",
      "sticker_id": "...",
      "sender": "user" / "agent" / "bot",
      "timestamp": "ISO8601",
      "event_id": "xxxxxxxxxxxxxxxx"
    }
    """
    try:
        user_id = event.source.user_id
    except Exception:
        user_id = ""

    # 事件 ID，給 GAS 做防重複
    try:
        event_id = event.id
    except Exception:
        event_id = None

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
        "sender": sender,
        "timestamp": ts_iso,
        "event_id": event_id,
    }

    log_to_gas(body)


# ================== OpenAI：產生小潔回覆 ==================

def generate_reply_from_openai(user_text: str, user_id: str = "") -> str:
    """
    呼叫 OpenAI，產生 H.R 燈藝小潔的回覆
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
                {"role": "user", "content": user_text},
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
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logging.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.error("Invalid signature. Check channel access token/secret.")
        abort(400)

    return "OK"


# ================== 事件處理：文字訊息 ==================

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_text = event.message.text
    user_id = event.source.user_id

    # 1) 呼叫 OpenAI 產生小潔回覆
    reply_text = generate_reply_from_openai(user_text, user_id=user_id)

    # 2) 回覆給使用者
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        logging.error("回覆文字訊息失敗: %s", e)

    # 3) 記錄使用者這句
    log_from_event(
        event,
        msg_type="text",
        text=user_text,
        sender="user",
    )

    # 4) 記錄小潔的回覆
    log_from_event(
        event,
        msg_type="text",
        text=reply_text,
        sender="bot",
    )


# ================== 事件處理：貼圖訊息 ==================

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    package_id = event.message.package_id
    sticker_id = event.message.sticker_id

    # 1) 用文字回覆客人（不主動發貼圖，避免 400）
    reply_text = "收到你的貼圖～如果方便的話，也可以再打一點文字，讓小潔更好幫你喔！"
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        logging.error("回覆貼圖訊息失敗: %s", e)

    # 2) 記錄使用者貼圖
    log_from_event(
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
    app.run(host="0.0.0.0", port=port)
