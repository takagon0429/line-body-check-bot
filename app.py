import os
from flask import Flask, request, abort
from linebot.v3.webhook import WebhookHandler            # singular
from linebot.v3.webhooks import MessageEvent, TextMessageContent  # plural
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

app = Flask(__name__)

# 環境変数から取得（Renderの環境変数設定で登録）
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("環境変数 LINE_CHANNEL_ACCESS_TOKEN と LINE_CHANNEL_SECRET を設定してください")

handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/", methods=["GET"])
def index():
    return "LINE Bot is running!", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """テキストメッセージを受信して返信する"""
    user_text = event.message.text.strip() if event.message and hasattr(event.message, "text") else ""
    reply_token = event.reply_token

    cfg = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
    with ApiClient(cfg) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[
                    TextMessage(text=f"受け取りました：{user_text}")
                ]
            )
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
