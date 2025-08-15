# app.py
# -------------------------------
# 役割:
# - ヘルスチェック: /, /healthz
# - LINE Webhook:  /callback
# - ユーザーから「front」「side」を受け、続く画像を2枚そろえたら解析APIに連携
# ポイント:
# - 重いライブラリ(cv2, numpy 等)は遅延 import（関数内でだけ import）
# - 画像バイトは bytes のまま requests.files に渡す（.read() は使わない）
# - LINE SDK v3 での返信は ReplyMessageRequest を必ず使う
# -------------------------------
import os
import logging
from typing import Dict, Any, Optional

from flask import Flask, request, abort

# Messaging API クライアント類
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
# ←ここがポイント：webhooks（複数）
from linebot.v3.webhooks import (
    WebhookHandler,
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError

import requests  # 軽量なので先頭でOK（重いライブラリは遅延import）


# -------------------------------
# 設定/初期化
# -------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.getenv(
    "ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze"
)

if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    logger.warning(
        "LINE の環境変数が未設定です。LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。"
    )

config = Configuration(access_token=CHANNEL_TOKEN)
api_client = ApiClient(config)
msg_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)
handler = WebhookHandler(CHANNEL_SECRET)

# メモリ簡易セッション: { user_id: {"expect": "front"/"side"/None, "front": bytes|None, "side": bytes|None} }
SESSIONS: Dict[str, Dict[str, Optional[bytes]]] = {}


# -------------------------------
# ヘルスチェック
# -------------------------------
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


# -------------------------------
# ユーティリティ
# -------------------------------
def get_user_id(ev: MessageEvent) -> Optional[str]:
    """送信者の userId を取得（友だち追加済み想定）。"""
    try:
        return ev.source.user_id  # type: ignore[attr-defined]
    except Exception:
        return None


def reply_text(reply_token: str, text: str) -> None:
    """テキストで返信（v3 は ReplyMessageRequest が必須）。"""
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
    except Exception:
        logger.exception("reply_text failed")


def fetch_image_bytes(message_id: str) -> bytes:
    """
    LINE の Blob API から画像バイトを取得。
    SDK 実装差に備えて bytes or ストリームの両対応にしておく。
    """
    resp = blob_api.get_message_content(message_id)

    # v3 SDK 実装差異対策: bytes or stream どちらも取り出せるように
    # 1) そのまま bytes の場合
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)

    # 2) requests.Response 互換のストリーム風オブジェクトの場合
    body = b""
    if hasattr(resp, "iter_content"):
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            if chunk:
                body += chunk
        return body

    # 3) content 属性だけあるケース
    if hasattr(resp, "content"):
        return bytes(resp.content)

    # 4) data 属性だけあるケース
    if hasattr(resp, "data"):
        return bytes(resp.data)

    # それでもダメなら型エラー
    raise TypeError(f"unsupported message content type from LINE SDK: {type(resp)}")


def try_call_analyzer(front_bytes: bytes, side_bytes: bytes, *, timeout_sec: int = 30) -> Dict[str, Any]:
    """
    解析APIに画像を投げる。
    **bytes に .read() は呼ばない**。requests の files に素の bytes を渡す。
    """
    files = {
        "front": ("front.jpg", front_bytes, "image/jpeg"),
        "side": ("side.jpg", side_bytes, "image/jpeg"),
    }
    try:
        r = requests.post(ANALYZER_URL, files=files, timeout=timeout_sec)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        logger.exception("Analyzer timeout")
        return {"error": "timeout"}
    except Exception as e:
        logger.exception("Analyzer error: %s", e)
        return {"error": str(e)}


def ensure_session(user_id: str) -> Dict[str, Optional[bytes]]:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = {"expect": None, "front": None, "side": None}
    return SESSIONS[user_id]


def set_expect(user_id: str, expect: Optional[str]) -> None:
    sess = ensure_session(user_id)
    sess["expect"] = expect


def reset_session(user_id: str) -> None:
    SESSIONS[user_id] = {"expect": None, "front": None, "side": None}


# -------------------------------
# Webhook 受信口
# -------------------------------
@app.post("/callback")
def callback():
    # LINE 署名検証
    signature = request.headers.get("x-line-signature")
    if signature is None:
        abort(400, "Missing signature")

    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature.")
        abort(400, "Invalid signature")
    except Exception:
        logger.exception("handler.handle failed")
        return "NG", 500

    return "OK", 200


# -------------------------------
# イベントハンドラ
# -------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(ev: MessageEvent):
    user_id = get_user_id(ev) or "unknown"
    text = (ev.message.text or "").strip().lower()

    if text in ("start", "開始", "start!", "はじめ", "begin"):
        reset_session(user_id)
        msg = (
            "姿勢チェックを開始します。\n"
            "1) まず「front」と送信 → 正面写真を“写真として”送信\n"
            "2) 次に「side」と送信 → 横写真を“写真として”送信\n"
            "両方そろったら自動的に解析します。"
        )
        reply_text(ev.reply_token, msg)
        return

    if text in ("front", "正面"):
        set_expect(user_id, "front")
        reply_text(ev.reply_token, "正面写真を“写真として”送ってください。")
        return

    if text in ("side", "横"):
        set_expect(user_id, "side")
        reply_text(ev.reply_token, "横写真を“写真として”送ってください。")
        return

    # ヘルプ
    if text in ("help", "ヘルプ", "使い方"):
        reply_text(
            ev.reply_token,
            "使い方:\n"
            "・「front」→ 正面の写真を送る\n"
            "・「side」 → 横の写真を送る\n"
            "・「開始」  → はじめから案内\n"
            "写真は“写真として送信”（共有URLは不可）"
        )
        return

    # その他テキスト
    reply_text(
        ev.reply_token,
        "「front」または「side」と送ってから、対応する写真を“写真として”送ってください。"
    )


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(ev: MessageEvent):
    user_id = get_user_id(ev) or "unknown"
    sess = ensure_session(user_id)
    expect = sess.get("expect")

    if expect not in ("front", "side"):
        reply_text(
            ev.reply_token,
            "画像の取得に失敗：送る前に「front」または「side」と送信してください。\n"
            "・LINEアプリから“直接”写真として送ってください（共有URL不可）"
        )
        return

    # 画像取得
    try:
        img_bytes = fetch_image_bytes(ev.message.id)
    except Exception:
        logger.exception("fetch_image_bytes failed")
        reply_text(
            ev.reply_token,
            "画像の取得に失敗しました。\n"
            "・LINEアプリから“直接”写真として送ってください（共有URL不可）\n"
            "・別の画像でもお試しください"
        )
        return

    # セッションに格納
    sess[expect] = img_bytes
    sess["expect"] = None

    # 両方そろったら解析へ
    if sess.get("front") and sess.get("side"):
        reply_text(ev.reply_token, "画像を受け取りました。解析します…（最大30秒）")

        # 重いライブラリはここで遅延 import（必要なら）
        # import numpy as np
        # import cv2

        result = try_call_analyzer(sess["front"], sess["side"])
        if result and not result.get("error"):
            scores = result.get("scores", {})
            advice = result.get("advice", [])
            summary = (
                "解析完了！\n"
                f"総合: {scores.get('overall','-')} / "
                f"姿勢: {scores.get('posture','-')} / "
                f"バランス: {scores.get('balance','-')}\n"
            )
            if advice:
                summary += "アドバイス: " + " / ".join(advice[:2])
            reply_text(ev.reply_token, summary)
        else:
            err = result.get("error") if result else "unknown"
            reply_text(ev.reply_token, f"解析に失敗しました。時間をおいて再試行してください。（{err}）")

        # 使い切りにして毎回リセット
        reset_session(user_id)
        return

    # まだ片方だけの場合の案内
    if not sess.get("front"):
        reply_text(ev.reply_token, "正面（front）がまだです。「front」と送ってから正面写真を送ってください。")
    elif not sess.get("side"):
        reply_text(ev.reply_token, "横（side）がまだです。「side」と送ってから横写真を送ってください。")


# -------------------------------
# ローカル起動
# -------------------------------
if __name__ == "__main__":
    # Render では gunicorn で起動する。ローカル開発用に Flask デバッグサーバも許可。
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
