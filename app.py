import os
import json
from io import BytesIO

from flask import Flask, request, abort

# --- LINE SDK v3（★モジュール位置に注意） ---
from linebot.v3.webhook import WebhookParser  # singular: webhook
from linebot.v3.webhooks import (             # plural: webhooks（イベント/メッセージ型）
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    Configuration,
    ApiClient,
)
from linebot.v3.messaging.exceptions import ApiException

import requests

# ------------------------
# 環境変数
# ------------------------
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ANALYZER_URL = os.environ.get(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    # 起動時に落とさず Render の /healthz だけでも返せるようにする
    print("[WARN] LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET が未設定です。")

# ------------------------
# Flask
# ------------------------
app = Flask(__name__)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# ------------------------
# LINE クライアント初期化（v3は ApiClient をかませる）
# ------------------------
_config = Configuration(access_token=CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else None
_api_client = ApiClient(_config) if _config else None
msg_api = MessagingApi(_api_client) if _api_client else None
blob_api = MessagingApiBlob(_api_client) if _api_client else None

parser = WebhookParser(CHANNEL_SECRET) if CHANNEL_SECRET else None

# ------------------------
# セッション状態（簡易）：ユーザーごとに front / side の待機状態を記録
# 本番はKV等を推奨
# ------------------------
EXPECTING = {}  # { user_id: "front" | "side" }

HELP_TEXT = (
    "使い方：\n"
    "1) 「開始」を送信\n"
    "2) 「front」 か 「side」と送信\n"
    "3) 続けて該当の写真を“画像として”送信\n"
)

WELCOME_TEXT = "front か side と送ってから、該当の姿勢写真を送ってください。"

def safe_reply(reply_token: str, text: str):
    """LINE 返信（失敗しても落とさない）"""
    if not msg_api:
        print("[WARN] msg_api 未初期化のため返信不可:", text)
        return
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
    except ApiException as e:
        print(f"[ERROR] Reply failed: {e}")

def post_to_analyzer(front_bytes: bytes | None, side_bytes: bytes | None, timeout: int = 60):
    """解析APIへ multipart/form-data でPOST"""
    files = {}
    if front_bytes:
        files["front"] = ("front.jpg", front_bytes, "image/jpeg")
    if side_bytes:
        files["side"] = ("side.jpg", side_bytes, "image/jpeg")

    resp = requests.post(ANALYZER_URL, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

@app.post("/callback")
def callback():
    # シグネチャ検証の準備
    if not parser:
        abort(500, "LINE Webhook parser is not initialized (missing secrets).")

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")

    # 複数イベント対応
    for ev in events:
        # メッセージイベント以外は無視
        if not isinstance(ev, MessageEvent):
            continue

        user_id = getattr(ev.source, "user_id", None)
        reply_token = ev.reply_token

        # テキスト
        if isinstance(ev.message, TextMessageContent):
            text = (ev.message.text or "").strip().lower()
            if text in ("開始", "start"):
                safe_reply(reply_token, WELCOME_TEXT + "\n\n" + HELP_TEXT)
                continue

            if text in ("front", "side"):
                if user_id:
                    EXPECTING[user_id] = text
                safe_reply(reply_token, f"OK。{text} の写真を“画像として”送ってください。")
                continue

            # それ以外
            safe_reply(reply_token, HELP_TEXT)
            continue

        # 画像
        if isinstance(ev.message, ImageMessageContent):
            if not blob_api:
                safe_reply(reply_token, "内部エラー：画像API未初期化")
                continue

            # 期待状態を確認
            expecting = EXPECTING.get(user_id or "", None)
            if expecting not in ("front", "side"):
                safe_reply(
                    reply_token,
                    "先に「front」または「side」と送ってください。\n\n" + HELP_TEXT,
                )
                continue

            # LINE から画像バイトを取得
            try:
                obj = blob_api.get_message_content(ev.message.id)
                # SDK v3 は bytes を返す
                content_bytes = obj if isinstance(obj, (bytes, bytearray)) else bytes(obj)
            except Exception as e:
                print(f"[ERROR] get_message_content failed: {e}")
                safe_reply(
                    reply_token,
                    "画像の取得に失敗しました。\n"
                    "・LINEアプリから“直接”画像を送ってください（共有URL不可）\n"
                    "・別の画像でも試してください",
                )
                continue

            # front/side を蓄積 → 2枚揃ったら解析
            # 簡易のため、一時的にユーザー毎のバッファとして保持
            # 本番ではストレージ or 一時URL連携を推奨
            key_front = f"{user_id}:front"
            key_side = f"{user_id}:side"

            if expecting == "front":
                app.config[key_front] = content_bytes
                safe_reply(reply_token, "front 画像を受け取りました。次は side 画像を送ってください。")
                EXPECTING[user_id] = "side"  # 次に期待するのは side
                continue

            if expecting == "side":
                app.config[key_side] = content_bytes

                # 2枚揃っているか確認
                front_bytes = app.config.get(key_front)
                side_bytes = app.config.get(key_side)
                if not front_bytes:
                    safe_reply(reply_token, "front 画像がまだ未取得です。先に front を送ってください。")
                    EXPECTING[user_id] = "front"
                    continue

                # 解析
                try:
                    result = post_to_analyzer(front_bytes, side_bytes, timeout=60)
                except requests.Timeout:
                    safe_reply(reply_token, "解析サーバへの接続がタイムアウトしました。少し待って再試行してください。")
                    continue
                except requests.RequestException as e:
                    print(f"[ERROR] analyzer request failed: {e}")
                    safe_reply(
                        reply_token,
                        "解析サーバへの送信に失敗しました。時間をおいて再試行してください。",
                    )
                    continue

                # 結果整形
                try:
                    advice = result.get("advice") or []
                    scores = result.get("scores") or {}
                    summary_lines = []
                    if scores:
                        summary_lines.append("【スコア】")
                        for k, v in scores.items():
                            summary_lines.append(f"- {k}: {v}")
                    if advice:
                        summary_lines.append("\n【アドバイス】")
                        for a in advice:
                            summary_lines.append(f"- {a}")
                    out = "\n".join(summary_lines) if summary_lines else "解析が完了しました。"

                    safe_reply(reply_token, out)

                    # 使い終わったので破棄
                    app.config.pop(key_front, None)
                    app.config.pop(key_side, None)
                    EXPECTING.pop(user_id, None)
                except Exception as e:
                    print(f"[ERROR] result formatting failed: {e}")
                    safe_reply(reply_token, "解析結果の整形に失敗しました。もう一度お試しください。")
                continue

            # ここには来ない想定
            safe_reply(reply_token, HELP_TEXT)
            continue

        # 上記以外のメッセージタイプ
        safe_reply(reply_token, "テキストまたは画像のみ対応しています。")
        continue

    return "OK", 200


if __name__ == "__main__":
    # ローカル起動用（Render は Procfile/gunicorn を使う）
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
