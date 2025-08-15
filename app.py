# app.py
# -*- coding: utf-8 -*-
import os
import logging
from flask import Flask, request, abort, jsonify

# ===== LINE SDK v3 =====
# 署名検証用パーサ
from linebot.v3.webhook import WebhookParser
# イベント / メッセージ型
from linebot.v3.webhooks import (
    Event,
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
# メッセージ送信用
from linebot.v3.messaging import (
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    Configuration,
)
# 例外
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging.exceptions import ApiException  # ← v3 はこっち

import requests

# -----------------------------
# 環境変数
# -----------------------------
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
ANALYZER_URL = os.environ.get("ANALYZER_URL", "").rstrip("/")

# バリデーション（Render 起動時に落として原因を明確化）
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET is missing.")

if not ANALYZER_URL:
    logging.warning("ANALYZER_URL is not set. /callback の解析連携はエラーになります。")

# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# -----------------------------
# LINE クライアント
# -----------------------------
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
msg_api = MessagingApi(configuration=config)
blob_api = MessagingApiBlob(configuration=config)
parser = WebhookParser(channel_secret=CHANNEL_SECRET)

# ユーザーごとの状態（front/side の指示）を簡易に保持（インメモリ）
USER_STATE: dict[str, str] = {}

# -----------------------------
# ヘルパー
# -----------------------------
def reply_text(reply_token: str, text: str) -> None:
    """テキスト返信（失敗しても落とさない）"""
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except ApiException as e:
        app.logger.exception(f"LINE reply error: {e}")

def fetch_image_bytes(message_id: str) -> bytes:
    """
    画像バイトを取得（v3: MessagingApiBlob.get_message_content）
    戻り値は file-like ではなく bytes として扱う
    """
    resp = blob_api.get_message_content(message_id)  # HTTPResponse ライク
    # v3 SDK は resp.data に bytes、またはストリームを返す実装
    data = getattr(resp, "data", None)
    if data is None:
        # data が無い場合は read() を試す（環境差異対策）
        if hasattr(resp, "read"):
            data = resp.read()
        else:
            # 念のため body や content も確認
            data = getattr(resp, "content", None) or getattr(resp, "body", None)
    if isinstance(data, bytes):
        return data
    # ストリームなら bytes 化
    if hasattr(data, "read"):
        return data.read()
    # 最後の保険：__iter__ でチャンクを結合
    if hasattr(resp, "__iter__"):
        return b"".join(chunk for chunk in resp)
    raise RuntimeError("could not fetch image bytes from LINE blob API.")

def post_to_analyzer(which: str, img_bytes: bytes) -> dict:
    """
    Analyzer へ画像POST。files は ('front' or 'side') の片方だけでもOK
    """
    endpoint = f"{ANALYZER_URL}/analyze"
    files = {
        which: (f"{which}.jpg", img_bytes, "image/jpeg")
    }
    r = requests.post(endpoint, files=files, timeout=60)
    r.raise_for_status()
    return r.json()

# -----------------------------
# Webhook エンドポイント
# -----------------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 署名検証
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        app.logger.warning("Invalid signature.")
        return abort(400, "invalid signature")

    for ev in events:  # type: Event
        try:
            # メッセージイベントのみ対象
            if isinstance(ev, MessageEvent):
                # テキスト
                if isinstance(ev.message, TextMessageContent):
                    text = (ev.message.text or "").strip().lower()
                    uid = ev.source.user_id if hasattr(ev.source, "user_id") else None

                    if text in ("開始", "start", "help", "ヘルプ"):
                        reply_text(
                            ev.reply_token,
                            "使い方：\n1) 『front』 か 『side』 と送信\n2) 続けて該当の姿勢写真を送信\n（共有URLではなく、写真データそのものを送ってください）"
                        )
                    elif text in ("front", "side"):
                        if uid:
                            USER_STATE[uid] = text
                        reply_text(ev.reply_token, f"了解しました。{text} 用の写真を送ってください。")
                    else:
                        reply_text(
                            ev.reply_token,
                            "『front』 または 『side』 と送ってから、続けて対象の写真を送ってください。"
                        )

                # 画像
                elif isinstance(ev.message, ImageMessageContent):
                    uid = ev.source.user_id if hasattr(ev.source, "user_id") else None
                    which = USER_STATE.get(uid, "front")  # 既定は front
                    try:
                        img_bytes = fetch_image_bytes(ev.message.id)
                    except Exception as e:
                        app.logger.exception(f"image fetch failed: {e}")
                        reply_text(
                            ev.reply_token,
                            "画像の取得に失敗しました。\n・LINEアプリから“直接”画像を送ってください（共有URL不可）\n・うまくいかない場合は別の画像でも試してください"
                        )
                        continue

                    # Analyzer 連携
                    if not ANALYZER_URL:
                        reply_text(ev.reply_token, "ANALYZER_URL が未設定のため解析できません。管理者に連絡してください。")
                        continue

                    try:
                        res = post_to_analyzer(which, img_bytes)
                        # ざっくり整形して返信
                        msg = ["解析完了！"]
                        if "scores" in res:
                            s = res["scores"]
                            msg.append(f"総合: {s.get('overall','-')}, 姿勢: {s.get('posture','-')}, バランス: {s.get('balance','-')}")
                        tips = res.get("advice") or []
                        if tips:
                            msg.append("アドバイス：")
                            for t in tips[:3]:
                                msg.append(f"・{t}")
                        reply_text(ev.reply_token, "\n".join(msg))
                    except requests.RequestException as e:
                        app.logger.exception(f"analyzer request failed: {e}")
                        reply_text(
                            ev.reply_token,
                            "解析サーバへの接続に失敗しました。しばらくしてから再度お試しください。"
                        )
                    except Exception as e:
                        app.logger.exception(f"analyzer error: {e}")
                        reply_text(
                            ev.reply_token,
                            "解析中にエラーが発生しました。別の画像でお試しください。"
                        )
            # 他イベントは無視
        except Exception as e:
            app.logger.exception(f"event handling error: {e}")
            # 失敗しても 200 で返し、LINE 側の再試行ループを避ける
            continue

    return "OK", 200


# -----------------------------
# ローカル起動
# -----------------------------
if __name__ == "__main__":
    # Render は gunicorn で起動される想定。ローカル開発用に Flask 起動を用意
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
