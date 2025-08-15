# app.py
import os
import io
from typing import Dict, Any, Optional

from flask import Flask, request, abort, jsonify

# --- LINE SDK v3 imports ---
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)

import requests  # 軽量なので先頭でOK

# ====== Flask ======
app = Flask(__name__)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


# ====== Config ======
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.environ.get(
    "ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze"
)

if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    app.logger.warning("LINE_CHANNEL_SECRET or LINE_CHANNEL_ACCESS_TOKEN is missing.")

# LINE APIクライアントはリクエスト毎にApiClient()を開くのがv3の基本
line_config = Configuration(access_token=CHANNEL_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 簡易セッション（プロセス内; Renderの再起動で消えます）======
# userId -> {"expect": Optional["front"|"side"|"both"], "front": Optional[bytes], "side": Optional[bytes]}
SESSIONS: Dict[str, Dict[str, Any]] = {}

HELP_TEXT = (
    "姿勢チェックを始めます。\n"
    "1) 「front」 と送信 → 正面の姿勢写真を1枚送信\n"
    "2) 「side」 と送信 → 横の姿勢写真を1枚送信\n"
    "※ 正面と横の両方を解析したい場合は、まず「front」を送って正面写真、"
    "続けて「side」を送って横写真を送ってください。両方揃うと自動解析します。"
)

def reply_text(reply_token: str, text: str) -> None:
    with ApiClient(line_config) as api_client:
        msg_api = MessagingApi(api_client)
        msg_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)],
            )
        )

def get_user_id(ev: MessageEvent) -> Optional[str]:
    # v3では source がdict化される場合もあるので両対応
    src = getattr(ev, "source", None)
    if not src:
        return None
    # userId / groupId / roomId いずれか
    return getattr(src, "userId", None) or getattr(src, "groupId", None) or getattr(src, "roomId", None)

def fetch_image_bytes(message_id: str) -> bytes:
    with ApiClient(line_config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        content = blob_api.get_message_content(message_id)
        # content.body は bytes（v3）
        return content.body

def try_call_analyzer(front: Optional[bytes], side: Optional[bytes]) -> Optional[dict]:
    """
    front/side のいずれか一方でもあれば送る設計にもできるが、
    ここでは「両方揃ったら送る」挙動にする。
    """
    if not front or not side:
        return None

    files = {
        "front": ("front.jpg", io.BytesIO(front), "image/jpeg"),
        "side": ("side.jpg", io.BytesIO(side), "image/jpeg"),
    }
    try:
        resp = requests.post(ANALYZER_URL, files=files, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        else:
            app.logger.error(f"Analyzer error: {resp.status_code} {resp.text}")
            return {"error": f"analyzer status {resp.status_code}"}
    except Exception as e:
        app.logger.exception(e)
        return {"error": str(e)}


# ====== LINE Webhook ======
@app.post("/callback")
def callback():
    # 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not signature:
        abort(400, "Missing signature")

    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.exception(e)
        abort(400)

    return "OK", 200


# ====== イベントハンドラ ======
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(ev: MessageEvent):
    text = (ev.message.text or "").strip().lower()
    reply_token = ev.reply_token
    user_id = get_user_id(ev) or "unknown"

    # セッション初期化
    sess = SESSIONS.setdefault(user_id, {"expect": None, "front": None, "side": None})

    if text in ("開始", "start", "はじめ", "はじめる"):
        SESSIONS[user_id] = {"expect": None, "front": None, "side": None}
        reply_text(reply_token, HELP_TEXT)
        return

    if text == "front":
        sess["expect"] = "front"
        reply_text(reply_token, "OK。正面の姿勢写真を1枚送ってください。")
        return

    if text == "side":
        sess["expect"] = "side"
        reply_text(reply_token, "OK。横の姿勢写真を1枚送ってください。")
        return

    # その他はヘルプ
    reply_text(reply_token, "コマンドが分かりません。\n" + HELP_TEXT)


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(ev: MessageEvent):
    reply_token = ev.reply_token
    user_id = get_user_id(ev) or "unknown"
    sess = SESSIONS.setdefault(user_id, {"expect": None, "front": None, "side": None})

    expect = sess.get("expect")
    if expect not in ("front", "side"):
        # 指示なしで画像が来た場合
        reply_text(
            reply_token,
            "画像の取得に失敗：送る前に「front」または「side」と送信してください。\n"
            "・LINEアプリから“直接”画像を送ってください（共有URL不可）"
        )
        return

    # 画像bytes取得
    try:
        img_bytes = fetch_image_bytes(ev.message.id)
    except Exception as e:
        app.logger.exception(e)
        reply_text(
            reply_token,
            "画像の取得に失敗：'bytes' object has no attribute 'read' 等のエラー回避済みです。\n"
            "・LINEアプリから“直接”画像を送ってください（共有URL不可）\n"
            "・うまくいかない場合は別の画像でも試してください"
        )
        return

    # セッションにセット
    sess[expect] = img_bytes
    sess["expect"] = None  # 消費
    app.logger.info(f"Stored {expect} image for user:{user_id}; front={bool(sess['front'])}, side={bool(sess['side'])}")

    # 両方揃ったら解析
    if sess.get("front") and sess.get("side"):
        reply_text(reply_token, "画像を受け取りました。解析します…（最大30秒）")
        result = try_call_analyzer(sess["front"], sess["side"])

        if result and not result.get("error"):
            # 結果を要約して返信
            try:
                scores = result.get("scores", {})
                advice = result.get("advice", [])
                summary = (
                    f"解析完了！\n"
                    f"総合: {scores.get('overall','-')}\n"
                    f"姿勢: {scores.get('posture','-')}\n"
                    f"バランス: {scores.get('balance','-')}\n"
                    f"アドバイス: " + " / ".join(advice[:2])  # 長すぎないように2件
                )
                reply_text(reply_token, summary)
            except Exception:
                reply_text(reply_token, "解析は完了しましたが、結果の整形に失敗しました。")
        else:
            err = result.get("error") if result else "unknown"
            reply_text(reply_token, f"解析に失敗しました。時間をおいて再試行してください。({err})")

        # 使い切りセッションにしてクリア
        SESSIONS[user_id] = {"expect": None, "front": None, "side": None}
    else:
        # もう片方を促す
        if not sess.get("front"):
            reply_text(reply_token, "正面（front）がまだです。先に「front」と送ってから正面写真を送ってください。")
        elif not sess.get("side"):
            reply_text(reply_token, "横（side）がまだです。次に「side」と送ってから横写真を送ってください.")


# ====== ローカル開発用エントリ ======
if __name__ == "__main__":
    # Render では Procfile/gunicorn が使われます。ローカルのみ run()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
