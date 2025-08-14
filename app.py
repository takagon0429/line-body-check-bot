import os
import time
import io
import json
from typing import Dict, Any

from flask import Flask, request, abort

# LINE SDK v3
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent
)

import requests


# ========= 設定 =========
ANALYZER_URL = os.environ.get(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze"
)
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

import logging
logging.basicConfig(level=logging.INFO)

# ユーザごとの状態: { userId: {"front": bytes|None, "side": bytes|None, "ts": float} }
USER_STATE: Dict[str, Dict[str, Any]] = {}
STATE_TTL_SEC = 15 * 60  # 15分で破棄

# ========= ルーティング =========
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    app.logger.info(f"/callback hit sig={bool(signature)} body_head={body[:120]!r}")
    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.exception("LINE handler error")
        return "NG", 400
    return "OK", 200



# ========= ハンドラ =========
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ("開始", "リセット", "reset", "start"):
        USER_STATE[user_id] = {"front": None, "side": None, "ts": time.time()}
        _reply(event.reply_token, "診断を開始します。まず【正面】の写真を送ってください。")
        return

    if text in ("使い方", "help", "ヘルプ"):
        _reply(event.reply_token,
               "姿勢診断の使い方：\n1)『開始』と送信\n2) 正面の全身写真を送信\n3) 側面の全身写真を送信\n（顔〜足先まで入るように）")
        return

    _reply(event.reply_token, "『開始』と送信してから、正面→側面の順に写真を送ってください。")


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event: MessageEvent):
    user_id = event.source.user_id
    _ensure_state(user_id)

    # 画像バイトを取得
    img_bytes = _download_image_bytes(event.message.id)
    if img_bytes is None:
        _reply(event.reply_token, "画像の取得に失敗しました。もう一度お試しください。")
        return

    # 1枚目→front, 2枚目→side の自動判定
    st = USER_STATE[user_id]
    if st["front"] is None:
        st["front"] = img_bytes
        st["ts"] = time.time()
        _reply(event.reply_token, "正面写真を受け取りました。次に【側面】の写真を送ってください。")
        return

    if st["side"] is None:
        st["side"] = img_bytes
        st["ts"] = time.time()
        # 両方揃ったので解析リクエスト
        _reply(event.reply_token, "側面写真を受け取りました。解析中です…（20〜30秒）")

        result_text = _call_analyzer_and_format(st["front"], st["side"])
        _reply(event.reply_token, result_text)

        # 終わったら状態を破棄
        USER_STATE.pop(user_id, None)
        return

    # 3枚目以降はリセット案内
    _reply(event.reply_token, "すでに2枚受け取りました。『開始』と送信してやり直してください。")


# ========= 内部関数 =========
def _reply(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def _ensure_state(user_id: str):
    st = USER_STATE.get(user_id)
    now = time.time()
    if (st is None) or (now - st.get("ts", 0) > STATE_TTL_SEC):
        USER_STATE[user_id] = {"front": None, "side": None, "ts": now}

def _download_image_bytes(message_id: str) -> bytes | None:
    try:
        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)
            content_resp = messaging_api.get_message_content(message_id)
            data = b"".join(content_resp.iter_content(chunk_size=1024))
            return data
    except Exception:
        return None

def _call_analyzer_and_format(front_bytes: bytes, side_bytes: bytes) -> str:
    try:
        files = {
            "front": ("front.jpg", io.BytesIO(front_bytes), "image/jpeg"),
            "side": ("side.jpg", io.BytesIO(side_bytes), "image/jpeg"),
        }
        # Render無料のタイムアウトを考慮して30秒
        r = requests.post(ANALYZER_URL, files=files, timeout=30)
        if r.status_code != 200:
            return f"診断APIエラー（HTTP {r.status_code}）。時間を置いて再度お試しください。"
        data = r.json()
        return _format_result(data)
    except requests.Timeout:
        return "診断がタイムアウトしました。サーバ負荷の可能性があります。しばらくしてからお試しください。"
    except Exception:
        return "診断に失敗しました。少し時間を置いて再度お試しください。"

def _format_result(data: Dict[str, Any]) -> str:
    # 解析JSONの例：
    # {
    #  "scores":{"overall":7.3,"posture":6.0,...},
    #  "front_metrics":{"shoulder_angle":"178.3°","pelvis_tilt":"179.9°"},
    #  "side_metrics":{"forward_head":"2.9cm","kyphosis":"軽度"},
    #  "advice":["...", "..."]
    # }
    scores = data.get("scores", {})
    fm = data.get("front_metrics", {})
    sm = data.get("side_metrics", {})
    advice = data.get("advice", [])

    lines = []
    if "overall" in scores:
        lines.append(f"総合: {scores.get('overall')}")
    if "posture" in scores:
        lines.append(f"姿勢: {scores.get('posture')}")

    if fm:
        lines.append("【正面】"
                     f" 肩角度: {fm.get('shoulder_angle','-')}"
                     f" / 骨盤傾き: {fm.get('pelvis_tilt','-')}")
    if sm:
        lines.append("【側面】"
                     f" 前方頭位: {sm.get('forward_head','-')}"
                     f" / 胸椎後弯: {sm.get('kyphosis','-')}")

    if advice:
        lines.append("アドバイス：")
        for a in advice[:3]:
            lines.append(f"・{a}")

    lines.append("\nまた『開始』と送ると再診断できます。")
    return "\n".join(lines)


if __name__ == "__main__":
    # ローカル起動用
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
