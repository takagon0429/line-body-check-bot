# app.py（該当箇所を丸ごと置き換え OK）

import os
import uuid
import logging
import json
import requests
from flask import Flask, request

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    MessagingApi, MessagingApiBlob, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, TextMessageContent
)

# --- ログ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- LINE 初期化 ---
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

# ---------- 画像保存ユーティリティ ----------
def _save_bytes_to_tmp(data: bytes, suffix: str = ".jpg") -> str:
    os.makedirs("/tmp", exist_ok=True)
    path = os.path.join("/tmp", f"{uuid.uuid4().hex}{suffix}")
    with open(path, "wb") as f:
        f.write(data)
    return path

# ---------- contentProvider を安全に取り出す ----------
def _extract_provider_info(msg: ImageMessageContent) -> dict:
    """
    v3 SDK は camelCase( contentProvider )、属性アクセスは snake_case も混在しうる。
    どちらでも拾えるように safe に吸い上げる。
    """
    provider = {}
    # dataclass→dict 化（失敗しても落ちないように）
    try:
        provider = json.loads(msg.to_json())
        provider = provider.get("contentProvider", {}) or provider.get("content_provider", {}) or {}
    except Exception:
        pass

    # 属性直読みの保険
    for key in ("type", "originalContentUrl", "previewImageUrl", "original_content_url", "preview_image_url"):
        if key not in provider:
            try:
                provider[key] = getattr(msg.content_provider, key)  # snake_case側
            except Exception:
                try:
                    provider[key] = getattr(msg, key)                # 直下にある場合の保険
                except Exception:
                    pass
    return provider

# ---------- LINE 取得 / 外部URL 取得 ----------
def fetch_image_bytes_from_line(message_id: str) -> bytes:
    with ApiClient(config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        stream = blob_api.get_message_content(message_id)
        return stream.read()

def fetch_image_bytes_from_url(url: str) -> tuple[bytes, int]:
    headers = {
        "User-Agent": "Mozilla/5.0 (LINE-Bot; Analyzer)",
        "Accept": "*/*",
        "Connection": "close",
    }
    # リダイレクト許可・タイムアウト短め
    resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
    return resp.content, resp.status_code

def save_image_from_event(event: MessageEvent) -> tuple[str, str]:
    """
    /tmp に保存して (path, provider_type) を返す。
    """
    msg: ImageMessageContent = event.message
    provider = _extract_provider_info(msg)

    ptype = (provider.get("type") or "").lower() or "line"
    o_url = provider.get("originalContentUrl") or provider.get("original_content_url")

    logger.info(f"[fetch] provider={ptype} message_id={msg.id} provider_raw={provider}")

    if ptype == "line":
        # 直接送信（カメラ or 端末から）の標準経路
        data = fetch_image_bytes_from_line(msg.id)
        if not data:
            raise RuntimeError("LINEバイナリが空でした")
        return _save_bytes_to_tmp(data, ".jpg"), ptype

    if ptype == "external":
        if not o_url:
            raise RuntimeError("external だが originalContentUrl がありません")
        data, status = fetch_image_bytes_from_url(o_url)
        if status != 200 or not data:
            raise RuntimeError(f"external URL 取得失敗 status={status} url={o_url}")
        return _save_bytes_to_tmp(data, ".jpg"), ptype

    # 想定外
    raise RuntimeError(f"未対応の contentProvider.type={ptype} raw={provider}")

# ---------- Webhook ----------
@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event):
    text = event.message.text.strip()
    with ApiClient(config) as api_client:
        msg_api = MessagingApi(api_client)

        if text in ("開始", "start", "Start"):
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="OK、正面→側面の順で“LINEから直接”写真を送ってください。共有リンクは不可です。")]
                )
            )
        else:
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="「開始」と送ると診断を案内します。")]
                )
            )

@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event):
    with ApiClient(config) as api_client:
        msg_api = MessagingApi(api_client)
        try:
            saved_path, provider = save_image_from_event(event)
            logger.info(f"[saved] {saved_path} via provider={provider}")

            # 最低限の受領返信（ここで正面/側面の状態管理→2枚揃ったら /analyze 叩く、を後段で）
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"画像を受け取りました（{provider}）。もう1枚送ってください。")]
                )
            )
        except Exception as e:
            logger.exception("image fetch failed")
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(
                        text=f"画像の取得に失敗：{e}\n"
                             f"・LINEアプリから“直接”画像を送ってください（共有URL不可）\n"
                             f"・うまくいかない場合は別の画像でも試してください"
                    )]
                )
            )

# ---------- Flask 基本エンドポイント ----------
@app.get("/")
def root():
    return "ok"

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        logger.exception("callback error")
        return "NG", 400
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
