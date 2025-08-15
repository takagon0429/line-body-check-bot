import os
import logging
from typing import Dict, Any, Optional

from flask import Flask, request, abort

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    WebhookParser,             # v3はこれを使う
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError

import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# 環境変数から取得
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL   = os.environ.get("ANALYZER_URL")  # 例: https://ai-body-check-analyzer.onrender.com/analyze

conf = Configuration(access_token=CHANNEL_TOKEN)
parser = WebhookParser(channel_secret=CHANNEL_SECRET)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")

    with ApiClient(conf) as api_client:
        msg_api = MessagingApi(api_client)

        for ev in events:
            # --- テキストメッセージ処理 ---
            if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessageContent):
                text = (ev.message.text or "").strip().lower()
                if text in ("開始", "start"):
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text="front か side と送ってから、該当の写真を送ってください。")]
                        )
                    )
                elif text in ("front", "side"):
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=f"{text} を受け付けました。続けて写真を送ってください。")]
                        )
                    )
                else:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text="「開始」「front」「side」を送ってください。")]
                        )
                    )

            # --- 画像メッセージ処理 ---
            elif isinstance(ev, MessageEvent) and isinstance(ev.message, ImageMessageContent):
                try:
                    blob_api = MessagingApiBlob(api_client)
                    content = blob_api.get_message_content(ev.message.id)
                    img_bytes = b"".join(chunk for chunk in content.iter_content())
                except Exception as e:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(
                                text=f"画像の取得に失敗：{e}\n・LINEから“直接”画像を送ってください（共有URL不可）"
                            )]
                        )
                    )
                    continue

                # 遅延import
                try:
                    import numpy as np
                    import cv2
                except Exception as e:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=f"解析の準備に失敗：{e}")]
                        )
                    )
                    continue

                # OpenCVでデコード
                try:
                    np_arr = np.frombuffer(img_bytes, dtype=np.uint8)
                    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if img is None:
                        raise ValueError("OpenCVでデコードできませんでした")
                except Exception as e:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=f"画像のデコードに失敗：{e}")]
                        )
                    )
                    continue

                # --- 解析API呼び出し or ダミー応答 ---
                try:
                    if ANALYZER_URL:
                        files = {"front": ("front.jpg", img_bytes, "image/jpeg")}
                        r = requests.post(ANALYZER_URL, files=files, timeout=20)
                        r.raise_for_status()
                        result = r.json()
                        msg = f"診断: overall={result['scores']['overall']}\nアドバイス: " + " / ".join(result.get("advice", []))
                    else:
                        msg = "画像を受け取りました（ダミー解析）。"

                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=msg)]
                        )
                    )
                except Exception as e:
                    msg_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=ev.reply_token,
                            messages=[TextMessage(text=f"診断API呼び出しでエラー：{e}")]
                        )
                    )

    return "OK", 200

if __name__ == "__main__":
    # ローカル用
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
