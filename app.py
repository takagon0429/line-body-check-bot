# app.py
import os
import json
from io import BytesIO
from threading import Thread

from flask import Flask, request, abort

import requests
from dotenv import load_dotenv

# ==== LINE SDK v3 ====
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)

# -----------------------
# 環境変数
# -----------------------
load_dotenv()

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.getenv(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("[WARN] LINEの環境変数が未設定です。BOT機能は動作しません。")

# LINEクライアント（v3の正しい初期化）
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
msg_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)

# Webhook署名パーサ
parser = WebhookParser(CHANNEL_SECRET)

# 状態管理（期待している次の画像: "front" or "side"）
EXPECTING: dict[str, str] = {}

# Flask
app = Flask(__name__)


# -----------------------
# ヘルスチェック
# -----------------------
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


# -----------------------
# 解析サーバ呼び出し（短いタイムアウトを明示）
# -----------------------
def post_to_analyzer(front_bytes: bytes | None, side_bytes: bytes | None):
    files = {}
    if front_bytes:
        files["front"] = ("front.jpg", front_bytes, "image/jpeg")
    if side_bytes:
        files["side"] = ("side.jpg", side_bytes, "image/jpeg")

    # 接続:5秒 / 応答読み取り:20秒
    resp = requests.post(ANALYZER_URL, files=files, timeout=(5, 20))
    resp.raise_for_status()
    return resp.json()


# -----------------------
# 画像バイト取得（v3 Blob API用）
# -----------------------
def get_image_bytes(message_id: str) -> bytes:
    """
    LINEの画像コンテンツをbytesで返す。
    SDKの戻りがbytesでもストリームでも吸収して返す。
    """
    content = blob_api.get_message_content(message_id)

    # v3はbytesが返る実装（将来互換のため念のため両対応）
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)

    # 一部実装で Response-like を返す可能性に備える
    if hasattr(content, "iter_content"):
        buf = BytesIO()
        for chunk in content.iter_content(chunk_size=1024 * 1024):
            if chunk:
                buf.write(chunk)
        return buf.getvalue()

    # 文字列やその他が来た時のフォールバック
    if hasattr(content, "read"):
        return content.read()

    raise TypeError(f"Unsupported content type from blob API: {type(content)}")


# -----------------------
# 安全リプライ
# -----------------------
def safe_reply(reply_token: str, text: str):
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])],
            )
        )
    except Exception as e:
        print(f"[WARN] reply failed: {e}")


# -----------------------
# 解析→push（非同期）
# -----------------------
def analyze_and_push(user_id: str, front_bytes: bytes, side_bytes: bytes):
    # ウォームアップ（任意・失敗しても無視）
    try:
        healthz = ANALYZER_URL.replace("/analyze", "/healthz")
        requests.get(healthz, timeout=2)
    except Exception:
        pass

    try:
        result = post_to_analyzer(front_bytes, side_bytes)

        advice = result.get("advice") or []
        scores = result.get("scores") or {}
        front_metrics = result.get("front_metrics") or {}
        side_metrics = result.get("side_metrics") or {}

        lines: list[str] = []
        if scores:
            lines.append("【スコア】")
            for k, v in scores.items():
                lines.append(f"- {k}: {v}")
        if front_metrics:
            lines.append("\n【正面】")
            for k, v in front_metrics.items():
                lines.append(f"- {k}: {v}")
        if side_metrics:
            lines.append("\n【側面】")
            for k, v in side_metrics.items():
                lines.append(f"- {k}: {v}")
        if advice:
            lines.append("\n【アドバイス】")
            for a in advice:
                lines.append(f"- {a}")

        out = "\n".join(lines) if lines else "解析が完了しました。"

    except requests.Timeout:
        out = "解析サーバが混み合っています。時間をおいて再試行してください。"
    except requests.RequestException as e:
        print(f"[ERROR] analyzer request failed: {e}")
        out = "解析サーバへの送信に失敗しました。時間をおいて再試行してください。"
    except Exception as e:
        print(f"[ERROR] result formatting failed: {e}")
        out = "解析中にエラーが発生しました。もう一度お試しください。"

    # push送信（ここでWebhookを待たせない）
    try:
        msg_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=out[:5000])],
            )
        )
    except Exception as e:
        print(f"[ERROR] push failed: {e}")


# -----------------------
# Webhook
# -----------------------
@app.post("/callback")
def callback():
    # 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        print(f"[ERROR] signature parse: {e}")
        abort(400)

    for ev in events:
        if not isinstance(ev, MessageEvent):
            continue

        reply_token = ev.reply_token
        user_id = getattr(ev.source, "user_id", None)

        # テキスト
        if isinstance(ev.message, TextMessageContent):
            text = (ev.message.text or "").strip().lower()

            if text in ("開始", "start", "かいし"):
                EXPECTING[user_id] = "front"
                safe_reply(
                    reply_token,
                    "姿勢チェックを開始します。\nまず「front」と入力してから正面の写真を送ってください。",
                )
                continue

            if text == "front":
                EXPECTING[user_id] = "front"
                safe_reply(reply_token, "正面(front)の画像を送ってください。")
                continue

            if text == "side":
                EXPECTING[user_id] = "side"
                safe_reply(reply_token, "側面(side)の画像を送ってください。")
                continue

            # その他メッセージ
            safe_reply(
                reply_token,
                "使い方:\n1) 「開始」\n2) 「front」と入力→正面写真\n3) 「side」と入力→側面写真\n解析は完了後にお送りします。",
            )
            continue

        # 画像
        if isinstance(ev.message, ImageMessageContent):
            if not user_id:
                safe_reply(reply_token, "ユーザーIDを取得できませんでした。もう一度お試しください。")
                continue

            expecting = EXPECTING.get(user_id)
            if expecting not in ("front", "side"):
                safe_reply(
                    reply_token,
                    "まず「開始」と入力し、続けて「front」または「side」を入力してから画像を送ってください。",
                )
                continue

            # 画像バイト取得
            try:
                content_bytes = get_image_bytes(ev.message.id)
            except Exception as e:
                print(f"[ERROR] get_image_bytes: {e}")
                safe_reply(
                    reply_token,
                    "画像の取得に失敗しました。LINEアプリから“画像として”送信してください（共有URL不可）。",
                )
                continue

            key_front = f"{user_id}:front"
            key_side = f"{user_id}:side"

            if expecting == "front":
                app.config[key_front] = content_bytes
                EXPECTING[user_id] = "side"
                safe_reply(reply_token, "frontを受け取りました。次に「side」と入力→側面の画像を送ってください。")
                continue

            if expecting == "side":
                app.config[key_side] = content_bytes

                front_bytes = app.config.get(key_front)
                side_bytes = app.config.get(key_side)

                if not front_bytes:
                    EXPECTING[user_id] = "front"
                    safe_reply(reply_token, "front画像が未取得です。先に「front」と入力→正面画像を送ってください。")
                    continue

                # 即時応答（Webhookは詰まらせない）
                safe_reply(reply_token, "解析を開始しました。完了次第、結果をお送りします。")

                # バックグラウンドで解析→push
                Thread(
                    target=analyze_and_push,
                    args=(user_id, front_bytes, side_bytes),
                    daemon=True,
                ).start()

                # 後始末
                app.config.pop(key_front, None)
                app.config.pop(key_side, None)
                EXPECTING.pop(user_id, None)
                continue

    return "OK", 200


# -----------------------
# 開発用エントリ
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
