# app.py
import os
from flask import Flask, request, jsonify

# LINE v3 SDK
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,          # ← これを使う
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)

# ===== 環境変数 =====
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

# ===== Flask =====
app = Flask(__name__)

# ===== LINE SDK 準備 =====
handler = WebhookHandler(CHANNEL_SECRET)

# ApiClient を 1回だけ作って使い回す
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api = MessagingApi(api_client)

# ===== ヘルスチェック =====
@app.get("/")
def index():
    return jsonify({"status": "ok", "service": "line-body-check-bot"})

# ===== Webhook 入口 =====
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        return "Missing signature", 400

    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.exception(f"/callback handle error: {e}")
        # LINE 側へは 200 を返して再試行を避ける
        return "OK", 200

    return "OK", 200

# ===== メッセージハンドラ =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="受け取りました ✅")]
            )
        )
    except Exception as e:
        app.logger.exception(f"reply text error: {e}")

@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event: MessageEvent):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="画像を受け取りました。正面と横の2枚を続けて送ってください。")]
            )
        )
    except Exception as e:
        app.logger.exception(f"reply image error: {e}")

# ===== エントリーポイント =====
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
