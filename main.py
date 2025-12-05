import os
import json
import logging
from datetime import datetime

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from openai import OpenAI

# ===== 環境變數 =====
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
# 建議在 Render 的環境變數加一個 GAS_BASE_URL
# 值填： https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec
GAS_BASE_URL        = os.getenv("GAS_BASE_URL") or "https://script.google.com/macros/s/AKfycbyQKpoVWZXTwksDyV5qIso1yMKEz1yQrQhuIfMfunNsgo7rtfN2eWWW_7YKV6rbl4Y8iw/exec"
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
client       = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ===== 呼叫 GAS WebApp 共用函式 =====
def gas_get(action, params=None):
    if not GAS_BASE_URL:
        raise RuntimeError("GAS_BASE_URL not set")
    params = params or {}
    params["action"] = action
    r = requests.get(GAS_BASE_URL, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def gas_post(action, payload):
    if not GAS_BASE_URL:
        raise RuntimeError("GAS_BASE_URL not set")
    data = {"action": action}
    data.update(payload)
    r = requests.post(GAS_BASE_URL, json=data, timeout=10)
    r.raise_for_status()
    return r.json()

# ===== 預約流程簡單狀態（Demo：存在記憶體） =====
USER_STATE = {}
# state: idle / waiting_date / waiting_time / waiting_info / confirming

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

# ===== 主事件處理（唯一的 handle_message） =====
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    text    = event.message.text.strip()

    # ① 一進來就丟一份 log 給 GAS（寫進原本那本 SHEET 的 line_messages）
    try:
        gas_post("lineLog", {
            "line_user_id": user_id,
            "text": text,
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        logging.warning(f"log to GAS error: {e}")

    # ② 簡單指令：預約 / 查預約
    if text in ("預約", "我要預約"):
        start_reservation_flow(user_id, event.reply_token)
        return
    if text in ("查預約", "我的預約"):
        show_my_reservations(user_id, event.reply_token)
        return

    # ③ 若在預約流程中，優先處理預約
    state = USER_STATE.get(user_id)
    if state and state.get("step") != "idle":
        handle_reservation_flow(user_id, text, event.reply_token)
        return

    # ④ 其他丟給 OpenAI 智慧客服
    reply_ai_chat(user_id, text, event.reply_token)

# ===== 智慧客服：OpenAI =====
def reply_ai_chat(user_id, user_text, reply_token):
    system_prompt = """你是 H.R 燈藝機車精品改裝店的 LINE 智慧客服「小潔」。
說話口吻：活潑、有溫度、專業，全部使用繁體中文，不要使用表情符號。
你可以回答關於 H.R 燈藝的營業時間、預約注意事項、改裝相關的常見問題。
如果使用者想預約，請引導他輸入「預約」，啟動預約流程。
"""

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_text},
        ],
    )
    reply_text = completion.choices[0].message.content.strip()
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

# ===== 預約流程：啟動 =====
def start_reservation_flow(user_id, reply_token):
    USER_STATE[user_id] = {
        "step": "waiting_date",
        "reservation": {}
    }
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="好的～來幫你安排預約時間。\n請先輸入日期，例如：2025-12-03")
    )

# ===== 預約流程：每一步處理 =====
def handle_reservation_flow(user_id, text, reply_token):
    state = USER_STATE.get(user_id) or {"step": "idle", "reservation": {}}
    step  = state.get("step")
    data  = state.get("reservation", {})

    # 輸入日期
    if step == "waiting_date":
        date_str = text.strip()
        data["date"] = date_str

        USER_STATE[user_id] = {"step": "waiting_time", "reservation": data}
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=f"預計日期：{date_str}\n請輸入預計時間，例如：14:00")
        )
        return

    # 輸入時間
    if step == "waiting_time":
        time_str = text.strip()
        data["time"] = time_str

        USER_STATE[user_id] = {"step": "waiting_info", "reservation": data}
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text="好的～再請你輸入：\n1. 姓名\n2. 連絡電話\n3. 車種（例如：JETSL）\n可以「姓名/電話/車種」這樣輸入。")
        )
        return

    # 輸入基本資料：姓名/電話/車種
    if step == "waiting_info":
        parts = text.replace("／", "/").split("/")
        if len(parts) < 3:
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="格式不太對～請用「姓名/電話/車種」這樣輸入喔！")
            )
            return
        name  = parts[0].strip()
        phone = parts[1].strip()
        model = parts[2].strip()

        data["name"]  = name
        data["phone"] = phone
        data["model"] = model

        USER_STATE[user_id] = {"step": "confirming", "reservation": data}
        msg = (
          "請幫我確認一下預約資料：\n"
          f"日期：{data['date']}\n"
          f"時間：{data['time']}\n"
          f"姓名：{name}\n"
          f"電話：{phone}\n"
          f"車種：{model}\n\n"
          "如果都沒問題，請回覆「確認預約」。"
        )
        line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
        return

    # 確認送出
    if step == "confirming":
        if text.strip() not in ("確認", "確認預約", "ok", "OK"):
            line_bot_api.reply_message(
                reply_token,
                TextSendMessage(text="如果要送出預約，請回覆「確認預約」。\n若要修改日期或時間，直接輸入新的日期或時間即可。")
            )
            return

        data = state["reservation"]
        try:
            # 先綁定 LINE userId 到顧客（用電話當 key）
            try:
                gas_post("bindLineCustomer", {
                    "line_user_id": user_id,
                    "display_name": data["name"],
                    "key": data["phone"]
                })
            except Exception as bind_err:
                logging.warning(f"bindLineCustomer error: {bind_err}")

            # 真正建立預約
            payload = {
                "line_user_id": user_id,
                "name":   data["name"],
                "phone":  data["phone"],
                "plate":  "",
                "model":  data["model"],
                "date":   data["date"],
                "start_time": data["time"],
                "end_time":   "",
                "service_type": "LINE 預約",
                "technician":   "",
                "remark":       "LINEBOT 自動預約",
                "source":       "LINE"
            }
            res = gas_post("reservationsCreate", payload)
            if res.get("ok"):
                msg = "預約已送出，感謝你～\n如果之後需要調整時間，也可以再傳訊息給我。"
            else:
                msg = "預約送出時遇到問題 QQ\n可以稍後再試一次，或直接留言給我們人工處理。"
        except Exception as e:
            logging.exception("create reservation error")
            msg = "預約送出時遇到問題 QQ\n可以先把資料截圖給我們，或稍後再試一次。"

        USER_STATE[user_id] = {"step": "idle", "reservation": {}}
        line_bot_api.reply_message(reply_token, TextSendMessage(text=msg))
        return

    # 其他狀況：重置流程
    USER_STATE[user_id] = {"step": "idle", "reservation": {}}
    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="我們重新來一次：如果要預約，請輸入「預約」。")
    )

# ===== 查詢我的預約（目前先簡單確認有綁定） =====
def show_my_reservations(user_id, reply_token):
    try:
        res = gas_get("resolveLineCustomer", {"line_user_id": user_id})
        if not res.get("ok"):
            line_bot_api.reply_message(
              reply_token,
              TextSendMessage(text="目前還沒有綁定你的資料喔！\n第一次預約時會幫你自動建立。")
            )
            return

        line_bot_api.reply_message(
          reply_token,
          TextSendMessage(text="已經幫你綁定顧客資料，之後我可以幫你查詢預約與消費紀錄～\n（詳細查詢功能我們下一步再加）")
        )
    except Exception as e:
        logging.exception("show reservations error")
        line_bot_api.reply_message(
          reply_token,
          TextSendMessage(text="查詢時遇到一點問題，等一下再試試看～")
        )

# ===== Flask 啟動 =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000)) or 5000
    app.run(host="0.0.0.0", port=port)
