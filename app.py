import os
import tempfile
import requests
import logging
from flask import Flask, request, abort, jsonify
from linebot.v3.webhooks import WebhookParser
from linebot.v3.messaging import MessagingApiBlob, Configuration, ApiClient
from linebot.v3.webhooks.models import MessageEvent, ImageMessageContent, TextMessageContent

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("line-bot")

# LINE設定
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ANALYZER_URL = os.getenv("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api_blob = MessagingApiBlob(api_client)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info(f"Request body: {body}")

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        abort(400)

    for event in events:
        if isinstance(event, MessageEvent):
            if isinstance(event.message, TextMessageContent):
                handle_text_message(event)
            elif isinstance(event.message, ImageMessageContent):
                on_image_message(event)

    return "OK"


def handle_text_message(event):
    from linebot.v3.messaging import MessagingApi, ReplyMessageRequest, TextMessage
    messaging_api = MessagingApi(api_client)
    messaging_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="画像を送ってください（正面と横向き）")]
        )
    )


def download_line_image_to_temp(message_id):
    try:
        blob_resp = messaging_api_blob.get_message_content(message_id)
        temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(temp_fd, "wb") as f:
            for chunk in blob_resp.iter_content():
                f.write(chunk)
        return temp_path
    except Exception as e:
        logger.error(f"download error: {e}")
        raise


def on_image_message(event):
    from linebot.v3.messaging import MessagingApi, ReplyMessageRequest, TextMessage
    messaging_api = MessagingApi(api_client)

    try:
        # 一時保存
        saved_path = download_line_image_to_temp(event.message.id)

        # Analyzerに送信（この例では正面・横の2枚が揃ったら送信する想定）
        files = {"front": open(saved_path, "rb")}
        try:
            resp = requests.post(ANALYZER_URL, files=files, timeout=60)
            resp.raise_for_status()
            result_text = resp.text
        except Exception as e:
            logger.error(f"analyzer post error: {e}")
            result_text = "診断API呼び出しでエラーが発生しました。"

        # 結果を返信
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=result_text)]
            )
        )

        os.remove(saved_path)

    except Exception as e:
        logger.error(f"on_image_message error: {e}")
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="画像処理中にエラーが発生しました。")]
            )
        )


# ヘルスチェック用エンドポイント
@app.get("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
