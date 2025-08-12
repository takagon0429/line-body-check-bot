# app.py — LINE Bot (v3) that collects 2 photos (front/side), calls Analyzer, and replies the result.

import os
import json
import tempfile
import logging
from typing import Dict, Optional

import requests
from flask import Flask, request, abort, jsonify

# LINE SDK v3
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.messaging import (
    MessagingApi,
    ApiClient,
    Configuration,
)
from linebot.v3.messaging.models import (
    ReplyMessageRequest,
    TextMessage,
)

# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# Analyzer のエンドポイント（Render の Analyzer サービスを指定）
ANALYZER_URL = os.getenv(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を環境変数に設定してください。")

# Flask
app = Flask(__name__)

# Logging（Render のログに出る）
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("line-bot")

# LINE clients (v3)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
messaging_api = MessagingApi(api_client)
handler = WebhookHandler(CHANNEL_SECRET)

# ------------------------------------------------------------------------------
# Simple health route
# ------------------------------------------------------------------------------
@app.get("/")
def index():
    return jsonify({"ok": True, "service": "line-body-check-bot", "analyzer": ANALYZER_URL}), 200

# ------------------------------------------------------------------------------
# In-memory session to collect 2 images per user
#  user_id -> {"front": "/tmp/xxx.jpg" or None, "side": "/tmp/yyy.jpg" or None}
# ------------------------------------------------------------------------------
SESSIONS: Dict[str, Dict[str, Optional[str]]] = {}

def ensure_session(user_id: str) -> Dict[str, Optional[str]]:
    sess = SESSIONS.get(user_id)
    if sess is None:
        sess = {"front": None, "side": None}
        SESSIONS[user_id] = sess
    return sess

def reset_session(user_id: str):
    sess = SESSIONS.get(user_id)
    if not sess:
        return
    # ファイル掃除
    for k in ("front", "side"):
        p = sess.get(k)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    SESSIONS[user_id] = {"front": None, "side": None}

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def reply_text(reply_token: str, text: str):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
    except Exception as e:
        # エラー内容をログへ
        logger.error("reply_text error: %s", e, exc_info=True)

def download_line_image_to_temp(message_id: str) -> str:
    """
    LINEサーバーから画像を取得して、一時ファイルへ保存してパスを返す。
    v3 SDKは get_message_content が bytes を返す実装。
    環境差に備えて bytes 以外のレスポンスもケア。
    """
    resp = messaging_api.get_message_content(message_id)

    # tempfile へ保存
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    path = f.name

    try:
        if isinstance(resp, (bytes, bytearray)):
            f.write(resp)
        else:
            # 念のため file-like も対応
            if hasattr(resp, "read"):
                f.write(resp.read())
            elif hasattr(resp, "data"):
                f.write(resp.data)  # 一部のHTTPResponse互換
            else:
                # 不明な型でも to bytes を試みる
                f.write(bytes(resp))
    finally:
        f.close()
    return path

def call_analyzer(front_path: str, side_path: str) -> dict:
    with open(front_path, "rb") as f1, open(side_path, "rb") as f2:
        files = {
            "front": ("front.jpg", f1, "image/jpeg"),
            "side": ("side.jpg", f2, "image/jpeg"),
        }
        r = requests.post(ANALYZER_URL, files=files, timeout=60)
        r.raise_for_status()
        return r.json()

def format_analysis_result(res: dict) -> str:
    """
    Analyzer からの JSON をLINE向けのテキストに整形。
    期待JSON（例）:
      {
        "scores": {"overall": 8.8, "posture": 9.8, "balance": 7.3, "muscle_fat": 9.8, "fashion": 8.8},
        "advice": ["～しましょう", "..."],
        "front_metrics": {...},
        "side_metrics": {...}
      }
    """
    scores = res.get("scores", {})
    advice = res.get("advice", [])
    fm = res.get("front_metrics", {})
    sm = res.get("side_metrics", {})

    lines = []
    lines.append("📊 解析結果")
    if scores:
        def g(k):  # 取り出し時は小数点1桁に
            v = scores.get(k)
            return f"{float(v):.1f}" if isinstance(v, (int, float)) else "-"

        lines.append(f"- 総合: {g('overall')}")
        lines.append(f"- 姿勢: {g('posture')} / バランス: {g('balance')}")
        lines.append(f"- 筋肉・脂肪: {g('muscle_fat')} / ファッション: {g('fashion')}")

    # 簡単にメトリクスの一部も表示
    if fm or sm:
        lines.append("")
        lines.append("🔎 指標（抜粋）")
        if 'shoulder_delta_y' in fm:
            lines.append(f"- 肩の左右差: {fm.get('shoulder_delta_y')}")
        if 'pelvis_delta_y' in fm:
            lines.append(f"- 骨盤の左右差: {fm.get('pelvis_delta_y')}")
        if 'trunk_angle' in sm:
            lines.append(f"- 体幹角度: {sm.get('trunk_angle')}")
        if 'forward_head' in sm:
            lines.append(f"- 頭部前方: {sm.get('forward_head')}")

    if advice:
        lines.append("")
        lines.append("💡 アドバイス")
        for a in advice[:3]:
            lines.append(f"- {a}")

    lines.append("")
    lines.append("※ 本結果は参考値です。撮影条件（姿勢・距離・明るさ）で変動します。")

    return "\n".join(lines)

# ------------------------------------------------------------------------------
# Webhook
# ------------------------------------------------------------------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        logger.error("callback handle error: %s", e, exc_info=True)
        return "NG", 400
    return "OK", 200

# ------------------------------------------------------------------------------
# Event Handlers
# ------------------------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    user_id = event.source.user_id if event.source else "unknown"
    text = event.message.text.strip() if isinstance(event.message, TextMessageContent) else ""

    # コマンド: リセット
    if text in ("/reset", "リセット"):
        reset_session(user_id)
        reply_text(event.reply_token, "状態をリセットしました。まずは正面の写真を送ってください。")
        return

    # まだ正面が無ければ案内、次に横
    sess = ensure_session(user_id)
    if not sess.get("front"):
        reply_text(event.reply_token, "テキストを受け取りました。まずは【正面】の写真を送ってください。")
    elif not sess.get("side"):
        reply_text(event.reply_token, "正面は受け取り済みです。次に【横】の写真を送ってください。")
    else:
        reply_text(event.reply_token, "すでに2枚受領済みです。/reset でやり直しできます。")

@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event: MessageEvent):
    user_id = event.source.user_id if event.source else "unknown"
    sess = ensure_session(user_id)

    # 受け取る順序: front → side
    next_slot = "front" if not sess.get("front") else ("side" if not sess.get("side") else None)
    if not next_slot:
        reply_text(event.reply_token, "すでに【正面】【横】の2枚を受領済みです。/reset でやり直しできます。")
        return

    # 画像をダウンロード
    try:
        saved_path = download_line_image_to_temp(event.message.id)
        sess[next_slot] = saved_path
        logger.info("Saved %s image for user %s -> %s", next_slot, user_id, saved_path)
    except Exception as e:
        logger.error("download error: %s", e, exc_info=True)
        reply_text(event.reply_token, f"画像の取得に失敗しました。もう一度送ってください。（{next_slot}）")
        return

    # 片方しかない場合は次の案内
    if next_slot == "front":
        reply_text(event.reply_token, "【正面】を受け取りました。次に【横】の写真を送ってください。")
        return

    # ここまで来たら front & side が揃った
    reply_text(event.reply_token, "【横】を受け取りました。解析中です。しばらくお待ちください…")

    front_path = sess.get("front")
    side_path = sess.get("side")

    try:
        result = call_analyzer(front_path, side_path)
        message = format_analysis_result(result)
        # 解析結果を返信
        reply_text(event.reply_token, message)
    except requests.HTTPError as he:
        logger.error("Analyzer HTTP error: %s / body=%s", he, getattr(he.response, "text", ""))
        reply_text(event.reply_token, "解析サーバーからエラーが返りました。時間をおいて再試行してください。")
    except Exception as e:
        logger.error("Analyzer call error: %s", e, exc_info=True)
        reply_text(event.reply_token, "解析に失敗しました。お手数ですが、/reset 後に撮影し直してお試しください。")
    finally:
        # 解析後はセッションをリセットして次回に備える
        reset_session(user_id)

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    # Render の Web Service は環境変数 PORT を渡してくる
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
