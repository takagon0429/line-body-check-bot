# app.py — LINE Bot (v3 SDK) 完全版
import os
import logging
from flask import Flask, request

# LINE v3 SDK
from linebot.v3.webhook import WebhookHandler, MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
)
from linebot.v3.messaging.models import (
    ReplyMessageRequest, TextMessage,
)

# --- 基本設定 ---
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = app.logger

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN をRenderの環境変数に設定してください。")

# LINE クライアント
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
messaging_api = MessagingApi(api_client)

# --- ヘルスチェック ---
@app.get("/")
def index():
    return "OK", 200

# --- Webhook 受信口 ---
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)  # ← bytes ではなく str で取得
    try:
        handler.handle(body, signature)
        return "OK", 200
    except Exception as e:
        logger.error(f"callback handle error: {e!r}")
        return "NG", 400

# --- イベントハンドラ（テキスト受信）---
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    user_text = event.message.text or ""
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=f"受け取りました！: {user_text}")
                ],
            )
        )
    except Exception as e:
        logger.error("reply text error: %s", e)

# --- ローカル起動用（Renderでは不要）---
if __name__ == "__main__":
    # Render は PORT 環境変数を渡してくることが多いので対応
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
