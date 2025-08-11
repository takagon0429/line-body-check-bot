import os
import io
import json
import time
from typing import Dict, Optional, Tuple

import requests
from flask import Flask, request, abort

# === LINE v3 SDK ===
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    MessagingApi, MessagingApiBlob, ApiClient, Configuration,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    ImageMessageContent,
    TextMessageContent,
)

# -----------------------
# 必須環境変数
# -----------------------
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
ANALYZER_URL = os.environ.get("ANALYZER_URL")  # 例: https://ai-body-check-analyzer.onrender.com/analyze

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")
if not ANALYZER_URL:
    # デフォルト（あなたのAnalyzer）
    ANALYZER_URL = "https://ai-body-check-analyzer.onrender.com/analyze"

# -----------------------
# Flask / LINE 初期化
# -----------------------
app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# -----------------------
# ユーザーごとの一時保存（メモリ）
# -----------------------
# pending[user_id] = {"front": bytes or None, "side": bytes or None, "ts": time.time()}
pending: Dict[str, Dict[str, Optional[bytes]]] = {}
EXPIRE_SEC = 10 * 60  # 10分で期限切れ


def _cleanup_expired():
    now = time.time()
    expired = [uid for uid, v in pending.items() if (now - v.get("ts", now)) > EXPIRE_SEC]
    for uid in expired:
        pending.pop(uid, None)


def _fetch_image_bytes(message_id: str) -> bytes:
    """LINEのメッセージIDから画像バイトを取得"""
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        resp = blob_api.get_message_content(message_id)
        bio = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=1024):
            if chunk:
                bio.write(chunk)
        return bio.getvalue()


def _post_to_analyzer(front: bytes, side: bytes) -> Tuple[bool, str]:
    """AnalyzerにPOSTして結果テキストを返す"""
    try:
        files = {
            "front": ("front.jpg", front, "image/jpeg"),
            "side": ("side.jpg", side, "image/jpeg"),
        }
        r = requests.post(ANALYZER_URL, files=files, timeout=60)
        if r.status_code != 200:
            return False, f"診断APIエラー: {r.status_code} {r.text[:200]}"

        data = r.json()
        scores = data.get("scores", {})
        advice = data.get("advice", [])
        front_m = data.get("front_metrics", {})
        side_m = data.get("side_metrics", {})

        lines = []
        if scores:
            lines.append("【スコア】")
            for k in ["overall", "posture", "balance", "muscle_fat", "fashion"]:
                if k in scores:
                    lines.append(f"- {k}: {scores[k]}")
        if advice:
            lines.append("\n【アドバイス】")
            for a in advice[:3]:
                lines.append(f"- {a}")

        if front_m or side_m:
            lines.append("\n【簡易メトリクス】")
            if "shoulder_delta_y" in front_m:
                lines.append(f"- 肩の左右差: {front_m['shoulder_delta_y']:.1f}px")
            if "pelvis_delta_y" in front_m:
                lines.append(f"- 骨盤の左右差: {front_m['pelvis_delta_y']:.1f}px")
            if "trunk_angle" in side_m:
                lines.append(f"- 体幹角度: {side_m['trunk_angle']:.1f}°")
            if "pelvic_angle" in side_m:
                lines.append(f"- 骨盤角度: {side_m['pelvic_angle']:.1f}°")

        text = "\n".join(lines) if lines else "診断完了しました。"
        return True, text

    except requests.RequestException as e:
        return False, f"診断API通信エラー: {e}"


# -----------------------
# ヘルスチェック
# -----------------------
@app.get("/")
def health():
    return "ok", 200


# -----------------------
# LINE Webhook
# -----------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        # ここが今回のポイント：エラー詳細をログに出す
        print("[/callback ERROR]", type(e).__name__, str(e))
        print("[/callback BODY]", body[:500])
        print("[/callback SIGNATURE]", signature)
        abort(400, str(e))
    return "OK"


# テキスト（リセットなど）
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    uid = event.source.user_id

    if text in ["リセット", "reset", "cancel", "キャンセル"]:
        pending.pop(uid, None)
        reply = "状態をリセットしました。正面→横の順で写真を2枚送ってください。"
    else:
        reply = "画像を2枚（正面→横）で送ってください。途中で「リセット」と送るとやり直しできます。"

    with ApiClient(configuration) as api_client:
        msg_api = MessagingApi(api_client)
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )


# 画像受信
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent):
    _cleanup_expired()

    uid = event.source.user_id
    message_id = event.message.id

    # 画像取得
    try:
        img_bytes = _fetch_image_bytes(message_id)
        if not img_bytes:
            raise ValueError("画像データが空です。")
    except Exception as e:
        with ApiClient(configuration) as api_client:
            msg_api = MessagingApi(api_client)
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"画像の取得でエラー: {e}")]
                )
            )
        return

    # 状態更新（正面→横の順）
    state = pending.get(uid)
    if not state:
        state = {"front": None, "side": None, "ts": time.time()}
        pending[uid] = state

    if state["front"] is None:
        state["front"] = img_bytes
        state["ts"] = time.time()
        reply = "正面の写真を受け取りました。次に『横』の写真を送ってください。"
        with ApiClient(configuration) as api_client:
            msg_api = MessagingApi(api_client)
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)]
                )
            )
        return

    if state["side"] is None:
        state["side"] = img_bytes
        state["ts"] = time.time()

        # 2枚そろったのでAnalyzerへ
        front_b = state["front"]
        side_b = state["side"]
        pending.pop(uid, None)  # クリア（失敗時は再送してもらう）

        ok, msg = _post_to_analyzer(front_b, side_b)

        with ApiClient(configuration) as api_client:
            msg_api = MessagingApi(api_client)
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg if ok else f"診断に失敗しました。\n{msg}")]
                )
            )
        return

    # それ以外（両方埋まっていたケース）
    pending.pop(uid, None)
    with ApiClient(configuration) as api_client:
        msg_api = MessagingApi(api_client)
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="最初からやり直します。正面→横の順で送ってください。")]
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
