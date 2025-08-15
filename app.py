# -*- coding: utf-8 -*-
# line-body-check-bot / app.py

import os
import io
import json
from typing import Dict, Optional

from flask import Flask, request, abort

# ---- LINE SDK v3（正しい import 先）-----------------------------------------
from linebot.v3.webhook import WebhookParser  # singular: webhook
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.messaging.exceptions import ApiException  # ← v3 ではここ

# ---- その他 ---------------------------------------------------------------
import requests

# --------------------------------------------------------------------------
# 環境変数
# --------------------------------------------------------------------------
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

# 解析 API（ai-body-check-analyzer）のエンドポイント
ANALYZER_URL = os.environ.get(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    # 起動時に必須環境変数が空ならログに出しておく（Render では logs で確認）
    print("[WARN] LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定の可能性があります。")

# --------------------------------------------------------------------------
# Flask
# --------------------------------------------------------------------------
app = Flask(__name__)

@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# --------------------------------------------------------------------------
# LINE Messaging API クライアント
# --------------------------------------------------------------------------
_line_config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
_api_client = ApiClient(_line_config)
msg_api = MessagingApi(_api_client)

# Webhook 署名パーサ
parser = WebhookParser(CHANNEL_SECRET)

# --------------------------------------------------------------------------
# ユーザ毎の状態管理（超簡易インメモリ）
#  - expect_kind: "front" or "side" を待っているか
#  - pending: 受け取った画像 bytes を保持（front/side）
# --------------------------------------------------------------------------
UserState = Dict[str, Dict[str, Optional[bytes]]]
STATE: UserState = {}  # { userId: {"expect_kind": Optional[str], "front": bytes|None, "side": bytes|None} }


def ensure_user(uid: str) -> Dict[str, Optional[bytes]]:
    if uid not in STATE:
        STATE[uid] = {"expect_kind": None, "front": None, "side": None}
    return STATE[uid]


def reply_text(reply_token: str, text: str) -> None:
    """テキスト返信のヘルパ"""
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except ApiException as e:
        app.logger.error(f"[LINE API] reply_message failed: {e.status} {e.reason} {getattr(e, 'body', '')}")


def push_text(user_id: str, text: str) -> None:
    """プッシュ送信のヘルパ（必要なら使用）"""
    try:
        msg_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )
    except ApiException as e:
        app.logger.error(f"[LINE API] push_message failed: {e.status} {e.reason} {getattr(e, 'body', '')}")


def fetch_image_bytes(message_id: str) -> Optional[bytes]:
    """
    LINE サーバから画像バイナリを取得。
    SDKの戻りが bytes だったり、ストリームだったりに備えて両対応。
    """
    try:
        content = msg_api.get_message_content(message_id)
        # content が bytes の場合
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)

        # content が StreamingResponse 風（iter_content を持つ）場合
        if hasattr(content, "iter_content"):
            data = bytearray()
            for chunk in content.iter_content(chunk_size=1024 * 64):
                if chunk:
                    data.extend(chunk)
            return bytes(data)

        # content が file-like（read を持つ）場合
        if hasattr(content, "read"):
            return content.read()

        app.logger.error(f"[fetch_image_bytes] Unsupported content type: {type(content)}")
        return None
    except ApiException as e:
        app.logger.error(f"[LINE API] get_message_content failed: {e.status} {e.reason} {getattr(e, 'body', '')}")
        return None
    except Exception as e:
        app.logger.exception(f"[fetch_image_bytes] unexpected error: {e}")
        return None


def try_analyze_and_reply(user_id: str, reply_token: str) -> None:
    """
    front と side の両方が揃っていれば解析 API を叩いて結果を返信。
    """
    s = ensure_user(user_id)
    if not s.get("front") or not s.get("side"):
        # まだ片方足りない
        missing = "front" if not s.get("front") else "side"
        reply_text(reply_token, f"{missing} の画像をまだ受け取っていません。先に「{missing}」と送って、続けて画像を送ってください。")
        return

    files = {
        "front": ("front.jpg", io.BytesIO(s["front"]), "image/jpeg"),
        "side": ("side.jpg", io.BytesIO(s["side"]), "image/jpeg"),
    }

    try:
        r = requests.post(ANALYZER_URL, files=files, timeout=60)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.RequestException as e:
        app.logger.exception(f"[analyze] request error: {e}")
        reply_text(reply_token, "解析サーバとの通信に失敗しました。時間を置いて再度お試しください。")
        return
    except json.JSONDecodeError:
        app.logger.error(f"[analyze] invalid JSON response: {r.text[:200]}")
        reply_text(reply_token, "解析サーバの応答が不正でした。")
        return

    # 結果を整形して返信
    try:
        overall = data.get("scores", {}).get("overall")
        advice = data.get("advice") or []
        front_metrics = data.get("front_metrics") or {}
        side_metrics = data.get("side_metrics") or {}

        lines = []
        if overall is not None:
            lines.append(f"総合スコア: {overall}")
        if front_metrics:
            lines.append(f"[正面] 肩角度: {front_metrics.get('shoulder_angle')}, 骨盤傾き: {front_metrics.get('pelvis_tilt')}")
        if side_metrics:
            lines.append(f"[側面] 猫背: {side_metrics.get('kyphosis')}, 頭位: {side_metrics.get('forward_head')}")
        if advice:
            lines.append("アドバイス:")
            for a in advice[:3]:
                lines.append(f"・{a}")

        text = "\n".join(lines) if lines else "解析が完了しました。"
        reply_text(reply_token, text)

        # 解析後は状態をクリア（毎回新しい計測に）
        STATE[user_id] = {"expect_kind": None, "front": None, "side": None}
    except Exception as e:
        app.logger.exception(f"[analyze] format reply error: {e}")
        reply_text(reply_token, "解析結果の整形でエラーが発生しました。")


@app.post("/callback")
def callback():
    # 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        app.logger.warning("[callback] invalid signature")
        abort(400)
    except Exception as e:
        app.logger.exception(f"[callback] parse error: {e}")
        abort(400)

    # イベント処理
    for ev in events:
        user_id = getattr(getattr(ev, "source", None), "userId", None)
        reply_token = getattr(ev, "replyToken", None)

        # 安全ガード
        if not user_id or not reply_token:
            continue

        state = ensure_user(user_id)

        # ---- Text メッセージ ------------------------------------------------
        if getattr(ev, "type", "") == "message" and getattr(getattr(ev, "message", None), "type", "") == "text":
            text = (getattr(ev.message, "text", "") or "").strip().lower()

            if text in ("start", "開始"):
                state["expect_kind"] = None
                state["front"] = None
                state["side"] = None
                reply_text(
                    reply_token,
                    "はじめまして！\n正面写真と側面写真の2枚で姿勢を解析します。\n\n"
                    "1) まずは「front」または「side」と送信\n"
                    "2) 続けて該当の写真を“画像として”送信\n"
                    "（※共有リンクやファイルではなく、写真として送ってください）"
                )
                continue

            if text in ("front", "side"):
                state["expect_kind"] = text
                reply_text(reply_token, f"{text} を受け付けました。続けて {text} の写真を送ってください。")
                continue

            # その他テキスト
            reply_text(
                reply_token,
                "メニュー:\n・「開始」…手順の案内\n・「front」…正面写真を送る準備\n・「side」…側面写真を送る準備"
            )
            continue

        # ---- 画像メッセージ ------------------------------------------------
        if getattr(ev, "type", "") == "message" and getattr(getattr(ev, "message", None), "type", "") == "image":
            # 画像を“写真として”送ってもらう必要がある
            expect = state.get("expect_kind")
            if expect not in ("front", "side"):
                reply_text(reply_token, "先に「front」または「side」と送ってください。続けて該当の写真を送れます。")
                continue

            message_id = getattr(ev.message, "id", None)
            if not message_id:
                reply_text(reply_token, "画像IDの取得に失敗しました。別の画像で試してください。")
                continue

            img_bytes = fetch_image_bytes(message_id)
            if not img_bytes:
                reply_text(
                    reply_token,
                    "画像の取得に失敗しました。\n"
                    "・LINEアプリから“直接”画像を送ってください（共有URL不可）\n"
                    "・うまくいかない場合は別の画像でも試してください"
                )
                continue

            state[expect] = img_bytes
            state["expect_kind"] = None  # 画像を受け取ったので解除

            # もう片方が揃っていれば解析へ
            if state.get("front") and state.get("side"):
                reply_text(reply_token, "2枚そろいました。解析を開始します。少々お待ちください。")
                try_analyze_and_reply(user_id, reply_token)
            else:
                remaining = "side" if expect == "front" else "front"
                reply_text(reply_token, f"{expect} の写真を受け付けました。次は「{remaining}」と送って、続けて {remaining} の写真を送ってください。")
            continue

        # ---- その他のイベント ----------------------------------------------
        # 既定ではスルー（必要に応じて follow/postback 等を追加）
        # pass

    return "OK", 200


# --------------------------------------------------------------------------
# ローカル用エントリ
# Render では Procfile の gunicorn を使う
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
