import os
import logging
from datetime import datetime, timezone
import json
import urllib.parse

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    StickerMessage,
    PostbackEvent,
    TextSendMessage,
    FlexSendMessage,
    Sender,
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

# ✅ 既有：給 GAS 用的 Web App URL（exec）— routing / line log 都用這個
GAS_LINE_LOG_URL = os.environ.get(
    "GAS_LINE_LOG_URL",
    "https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec",
)

# ✅ 新增：booking 確認（更新 Reservations 狀態）用的 GAS URL
# 若你 bookingConfirmByReservationId 也寫在同一支 GAS，就不用另外設 GAS_BOOKING_URL
GAS_BOOKING_URL = os.environ.get("GAS_BOOKING_URL", GAS_LINE_LOG_URL)

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
    """
    if bot_mode != "auto_ai":
        return False

    if not isinstance(event_timestamp_ms, (int, float)):
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    delta_ms = now_ms - int(event_timestamp_ms)

    # 超過 10 秒就視為舊事件，不自動回覆
    if delta_ms > 10 * 1000:
        logging.info(
            "event too old to auto-reply: delta_ms=%s (mode=%s)", delta_ms, bot_mode
        )
        return False

    # 如果有 last_mode_at_ms，事件時間要晚於最後一次切換模式時間
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
    把 body 打給 GAS 的 doPost。
    使用 { action: 'lineLog', body: {...} } 格式，
    對應 Code.gs 裡的 appLineLog。
    """
    if not GAS_LINE_LOG_URL:
        logging.warning("GAS_LINE_LOG_URL 未設定，略過記錄 log")
        return

    try:
        payload = {
            "action": "lineLog",
            "body": body,
        }
        resp = requests.post(GAS_LINE_LOG_URL, json=payload, timeout=5)
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
    display_persona=None,
    sent_by_agent_id=None,
):
    """
    統一把 LINE 的事件轉成 appLineLog 需要的 JSON 格式。
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

    # 同一個事件：user / bot / agent 用不同後綴，避免重複
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
        "sender": sender,   # 'user' / 'bot' / 'agent'
        "timestamp": ts_iso,
    }

    # 只有需要時才加這兩欄
    if display_persona:
        body["display_persona"] = display_persona
    if sent_by_agent_id:
        body["sent_by_agent_id"] = sent_by_agent_id

    log_to_gas(body)


def log_postback_event(line_user_id: str, data: str, sender: str = "user"):
    """
    記錄 postback（不一定每個專案都要，但建議留一筆可追查）
    """
    try:
        ts_iso = datetime.now(timezone.utc).isoformat()
        body = {
            "event_id": f"postback:{int(datetime.now(timezone.utc).timestamp()*1000)}:{sender}",
            "line_user_id": line_user_id or "",
            "type": "postback",
            "text": data or "",
            "sender": sender,
            "timestamp": ts_iso,
        }
        log_to_gas(body)
    except Exception as e:
        logging.error("log_postback_event error: %s", e)


# ================== OpenAI：產生小潔回覆 ==================

def generate_reply_from_openai(user_text: str, user_id: str = "") -> str:
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


# ================== Booking 確認：postback → GAS 更新 Reservations.status ==================

def parse_confirm_reservation_id(data: str) -> str:
    """
    支援格式：
      1) CONFIRM|R-xxxx
      2) action=confirm&rid=R-xxxx
      3) JSON: {"action":"confirm","reservation_id":"R-xxxx"}
    """
    if not data:
        return ""

    s = data.strip()

    if s.startswith("CONFIRM|"):
        return s.split("|", 1)[1].strip()

    # querystring style
    if "rid=" in s or "reservation_id=" in s:
        try:
            qs = urllib.parse.parse_qs(s, keep_blank_values=True)
            rid = (qs.get("rid") or qs.get("reservation_id") or [""])[0]
            return (rid or "").strip()
        except Exception:
            pass

    # json
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            rid = obj.get("rid") or obj.get("reservation_id") or ""
            return str(rid).strip()
    except Exception:
        pass

    return ""


def confirm_booking_in_gas(reservation_id: str, line_user_id: str):
    """
    呼叫 GAS：bookingConfirmByReservationId
    回傳 dict（盡量解析 json）
    """
    if not GAS_BOOKING_URL:
        return {"ok": False, "error": "GAS_BOOKING_URL_MISSING"}

    payload = {
        "action": "bookingConfirmByReservationId",
        "body": {
            "reservation_id": reservation_id,
            "line_user_id": line_user_id or "",
        },
    }

    try:
        resp = requests.post(GAS_BOOKING_URL, json=payload, timeout=8)
        text = resp.text or ""
        logging.info("confirm_booking_in_gas status=%s body=%s", resp.status_code, text[:200])
        try:
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"ok": False, "error": "GAS_NON_JSON_RESPONSE", "raw": text[:500]}
    except Exception as e:
        return {"ok": False, "error": "GAS_REQUEST_FAILED", "message": str(e)}


def make_confirmed_flex(reservation_id: str, already: bool = False):
    title = "已確認到店" if not already else "已確認過了"
    body_lines = [
        f"預約編號：{reservation_id}",
        "收到～若需要改期或取消，直接跟我們說一聲就好。",
    ]
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "lg"},
                {"type": "text", "text": "\n".join(body_lines), "margin": "md", "wrap": True},
            ],
        },
    }


# ================== LINE Webhook 入口 ==================

@app.route("/api/ping", methods=["GET"])
def api_ping():
    return {
        "ok": True,
        "has_line_secret": bool(CHANNEL_SECRET),
        "has_line_token": bool(CHANNEL_ACCESS_TOKEN),
        "has_gas_line_log_url": bool(GAS_LINE_LOG_URL),
        "has_gas_booking_url": bool(GAS_BOOKING_URL),
    }


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logging.info("Request body (head200): %s", body[:200])

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.error("Invalid signature. Check channel access token/secret.")
        abort(400)

    return "OK"


# ================== 事件處理：Postback（確認到店） ==================

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = ""
    try:
        user_id = event.source.user_id
    except Exception:
        user_id = ""

    data = ""
    try:
        data = event.postback.data or ""
    except Exception:
        data = ""

    logging.info("postback from %s data=%s", user_id, data)
    log_postback_event(user_id, data, sender="user")

    rid = parse_confirm_reservation_id(data)
    if not rid:
        # 不是我們要的 postback，就略過（避免影響你其他功能）
        return

    # ✅ 呼叫 GAS 更新 Reservations
    res = confirm_booking_in_gas(rid, user_id)

    if res.get("ok") is True:
        already = bool(res.get("alreadyConfirmed"))

        # ✅ 第二次（含之後）完全不回覆（靜默）
        if already:
            return

        flex = FlexSendMessage(
            alt_text="已確認到店",
            contents=make_confirmed_flex(rid, already=False)
        )

        try:
            line_bot_api.reply_message(
                event.reply_token,
                [
                    TextSendMessage(
                        text="收到，我已幫您把這筆預約標記為「已確認到店」。",
                        sender=Sender(name="小潔 H.R 燈藝客服"),
                    ),
                    flex,
                ],
            )
        except Exception as e:
            logging.error("reply postback success failed: %s", e)
        return

    # ✅ 失敗也要回覆（讓你知道 webhook 有進來）
    logging.error("confirm_booking_in_gas failed: %s", res)
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="我有收到您的確認，但系統更新狀態時出了點狀況。麻煩您直接回覆我們『已確認到店』，我會請客服幫您處理。",
                sender=Sender(name="小潔 H.R 燈藝客服"),
            ),
        )
    except Exception as e:
        logging.error("reply postback failed failed: %s", e)


# ================== 事件處理：文字訊息 ==================

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_text = event.message.text
    user_id = event.source.user_id

    # 0) 先記錄「使用者這句話」（不管是不是自動小潔）
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
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(
                    text=reply_text,
                    sender=Sender(
                        name="小潔 H.R 燈藝客服",
                    ),
                ),
            )
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

    # 3) 如果真的有產生小潔回覆，再額外記錄一筆 bot 訊息（帶 persona）
    if reply_text:
        log_from_event(
            event,
            msg_type="text",
            text=reply_text,
            sender="bot",
            display_persona="xiaojie",
        )


# ================== 事件處理：貼圖訊息 ==================

@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker_message(event):
    package_id = event.message.package_id
    sticker_id = event.message.sticker_id
    user_id = event.source.user_id

    # 先查 routing
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
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(
                    text=reply_text,
                    sender=Sender(
                        name="小潔 H.R 燈藝客服",
                    ),
                ),
            )
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

    # 記錄使用者這張貼圖
    log_from_event(
        event,
        msg_type="sticker",
        text="",
        sticker_package_id=package_id,
        sticker_id=sticker_id,
        sender="user",
    )

    # 如果有回覆文字，再記錄一筆 bot 訊息（帶 persona）
    if reply_text:
        log_from_event(
            event,
            msg_type="text",
            text=reply_text,
            sender="bot",
            display_persona="xiaojie",
        )


# ================== 主程式啟動 ==================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info("Booting... port=%s has_gas_booking_url=%s", port, bool(GAS_BOOKING_URL))
    app.run(host="0.0.0.0", port=port)
