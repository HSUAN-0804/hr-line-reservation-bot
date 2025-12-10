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
GAS_LINE_LOG_URL = os.environ.get(
    "GAS_LINE_LOG_URL",
    "https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec"
)

logging.basicConfig(level=logging.INFO)


# ================== 共用：查詢 LineUsers 的 bot_mode / last_mode_at_ms ==================

def get_line_user_routing(line_user_id: str):
    """
    從 GAS 取得這個 line_user_id 的 routing 設定：
      bot_mode: auto_ai / owner_manual / staff_manual
      owner_agent_id: OWNER / XMING / ''
      last_mode_at_ms: 毫秒數或 None

    回傳：(bot_mode, owner_agent_id, last_mode_at_ms)
    """
    default = ("auto_ai", "", None)

    if not GAS_LINE_LOG_URL or not line_user_id:
        return default

    try:
        resp = requests.get(
            GAS_LINE_LOG_URL,
            params={
                "action": "getLineUserRouting",
                "line_user_id": line_user_id,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return default
        if data.get("ok") is False:
            return default

        mode = data.get("bot_mode") or "auto_ai"
        owner = data.get("owner_agent_id") or ""
        last_ms = data.get("last_mode_at_ms", None)

        if isinstance(last_ms, (int, float)):
            last_ms = int(last_ms)
        else:
            last_ms = None

        if mode not in ("auto_ai", "owner_manual", "staff_manual"):
            mode = "auto_ai"

        logging.info(
            "routing for %s: mode=%s owner=%s last_mode_at_ms=%s",
            line_user_id, mode, owner, last_ms
        )
        return mode, owner, last_ms

    except Exception as e:
        logging.error("get_line_user_routing error: %s", e)
        return default


def should_auto_reply_text(bot_mode: str, event_timestamp_ms, last_mode_at_ms) -> bool:
    """
    決定這一則文字事件，是否要由小潔自動回覆。

    條件：
      1) bot_mode == auto_ai
      2) 事件時間 >= last_mode_at_ms（如果有）
      3) 事件與現在時間差 <= 120 秒（避免處理太舊的重送事件）
    """
    if bot_mode != "auto_ai":
        return False

    if not isinstance(event_timestamp_ms, (int, float)):
        # 理論上 LINE 都會給 timestamp，如果沒有，就保守一點：不自動回覆
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    delta_ms = now_ms - int(event_timestamp_ms)

    # 📌 如果事件發生時間距今超過 120 秒，就當成舊事件，不自動回覆
    if delta_ms > 10 * 1000:
        logging.info(
            "event too old to auto-reply: delta_ms=%s (mode=%s)", delta_ms, bot_mode
        )
        return False

    # 如果有 last_mode_at_ms，要求事件時間要晚於最後一次模式切換時間
    if isinstance(last_mode_at_ms, (int, float)):
        if int(event_timestamp_ms) < int(last_mode_at_ms):
            logging.info(
                "event earlier than last_mode_at_ms, skip auto reply: event_ms=%s last_ms=%s",
                event_timestamp_ms, last_mode_at_ms
            )
            return False

    return True


# ================== 共用：把訊息記錄到 GAS（line_messages） ==================

def log_to_gas(body: dict):
    """
    直接把 body 當 JSON POST 給 GAS，
    GAS 那邊的 doPost 應該要做類似：
      const data = JSON.parse(e.postData.contents); appLineLog(data)
    """
    if not GAS_LINE_LOG_URL:
        logging.warning("GAS_LINE_LOG_URL 未設定，略過記錄 log")
        return

    try:
        resp = requests.post(GAS_LINE_LOG_URL, json=body, timeout=5)
        logging.info("log_to_gas resp: %s", resp.text[:200])
    except Exception as e:
        # 只記 log，不影響主流程
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
    統一把 LINE 的事件轉成 appLineLog 需要的 JSON 格式：
    {
      "event_id": "...",          # ✅ 用來做去重複
      "line_user_id": "...",
      "type": "text" 或 "sticker",
      "text": "...",
      "sticker_package_id": "...",
      "sticker_id": "...",
      "sender": "user" / "agent" / "bot",
      "timestamp": "ISO8601"
    }
    """
    # user id
    try:
        user_id = event.source.user_id
    except Exception:
        user_id = ""

    # LINE 的 message.id：同一則訊息固定不變
    try:
        message_id = getattr(event.message, "id", "")
    except Exception:
        message_id = ""

    # 🎯 同一個事件：user 跟 bot 用不同後綴
    #   例如 "123456:user" / "123456:bot"
    event_id = f"{message_id}:{sender}" if message_id else ""

    # timestamp（LINE 給的是毫秒）
    try:
        ts_iso = datetime.fromtimestamp(
            event.timestamp / 1000, tz=timezone.utc
        ).isoformat()
    except Exception:
        ts_iso = datetime.now(timezone.utc).isoformat()

    body = {
        "event_id": event_id,
        "line_user_id": user_id,
        "type": msg_type,  # 'text' or 'sticker'
        "text": text,
        "sticker_package_id": str(sticker_package_id) if sticker_package_id else "",
        "sticker_id": str(sticker_id) if sticker_id else "",
        "sender": sender,  # 'user' / 'bot' / 'agent' 等
        "timestamp": ts_iso,
    }

    log_to_gas(body)


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

    # 0) 先記錄「使用者這句話」
    log_from_event(
        event,
        msg_type="text",
        text=user_text,
        sender="user",
    )

    # 1) 查 routing
    bot_mode, owner_agent_id, last_mode_at_ms = get_line_user_routing(user_id)
    event_ms = getattr(event, "timestamp", None)

    # 2) 決定是否自動回覆
    should_reply = should_auto_reply_text(bot_mode, event_ms, last_mode_at_ms)

    reply_text = None
    if should_reply:
        reply_text = generate_reply_from_openai(user_text, user_id=user_id)

    reply_token = event.reply_token
    invalid_tokens = {
        "00000000000000000000000000000000",
        "ffffffffffffffffffffffffffffffff",
    }

    if reply_text and reply_token not in invalid_tokens:
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            logging.error("回覆文字訊息失敗: %s", e)
    else:
        if reply_token in invalid_tokens:
            logging.info("跳過假 reply_token，不回覆文字訊息。")
        else:
            logging.info(
                "text: bot_mode=%s last_mode_at_ms=%s event_ms=%s should_reply=%s",
                bot_mode, last_mode_at_ms, event_ms, should_reply
            )

    if reply_text:
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
    user_id = event.source.user_id

    bot_mode, owner_agent_id, last_mode_at_ms = get_line_user_routing(user_id)
    event_ms = getattr(event, "timestamp", None)

    should_reply = should_auto_reply_text(bot_mode, event_ms, last_mode_at_ms)

    reply_text = None
    if should_reply:
        reply_text = "收到你的貼圖～如果方便的話，也可以再打一點文字，讓小潔更好幫你喔！"

    reply_token = event.reply_token
    invalid_tokens = {
        "00000000000000000000000000000000",
        "ffffffffffffffffffffffffffffffff",
    }

    if reply_text and reply_token not in invalid_tokens:
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            logging.error("回覆貼圖訊息失敗: %s", e)
    else:
        if reply_token in invalid_tokens:
            logging.info("跳過假 reply_token，不回覆貼圖訊息。")
        else:
            logging.info(
                "sticker: bot_mode=%s last_mode_at_ms=%s event_ms=%s should_reply=%s",
                bot_mode, last_mode_at_ms, event_ms, should_reply
            )

    log_from_event(
        event,
        msg_type="sticker",
        text="",
        sticker_package_id=package_id,
        sticker_id=sticker_id,
        sender="user",
    )

    if reply_text:
        log_from_event(
            event,
            msg_type="text",
            text=reply_text,
            sender="bot",
        )


# ================== 主程式啟動 ==================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

