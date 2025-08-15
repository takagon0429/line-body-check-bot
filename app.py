import os
import logging
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# ---- LINE SDK v3 正しい import ----
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.exceptions import InvalidSignatureError, LineBotApiError

# ---- 環境変数 ----
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TEST_USER_ID   = os.getenv("LINE_TEST_USER_ID", "")  # 自分のuserId（任意）

if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    logger.warning("LINE env missing: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")

parser = WebhookParser(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_TOKEN)

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature (Check LINE_CHANNEL_SECRET)")
        abort(400)
    except Exception as e:
        logger.exception(f"parse error: {e}")
        abort(400)

    with ApiClient(config) as api_client:
        msg_api = MessagingApi(api_client)

        for ev in events:
            try:
                if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessageContent):
                    txt = (ev.message.text or "").strip().lower()
                    reply = "pong" if txt == "ping" else f"受け取り: {ev.message.text}"
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=reply)]
                        )
                    )
                elif isinstance(ev, MessageEvent) and isinstance(ev.message, ImageMessageContent):
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text="画像受領（検証用）。")]
                        )
                    )
                else:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text="テキスト or 画像を送ってください。")]
                        )
                    )
            except LineBotApiError as e:
                logger.exception(f"LINE API error: {e}")  # ここ見れば原因わかる
            except Exception as e:
                logger.exception(f"handler error: {e}")

    return "OK", 200

# --- アクセストークン単体チェック（Webhook関係なしで Push を送る）---
@app.post("/selftest-push")
def selftest_push():
    if not TEST_USER_ID:
        return "Set LINE_TEST_USER_ID", 500
    try:
        with ApiClient(config) as api_client:
            msg_api = MessagingApi(api_client)
            msg_api.push_message(PushMessageRequest(
                to=TEST_USER_ID,
                messages=[TextMessage(text="✅ Pushテスト成功（アクセストークン有効）")]
            ))
        return "pushed", 200
    except LineBotApiError as e:
        logger.exception(f"push error: {e}")
        return "push failed", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
