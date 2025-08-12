import os
import tempfile
import time
import logging
from typing import Dict, Optional

import requests
from flask import Flask, request, abort

# ==== LINE SDK v3 ====
from linebot.v3 import WebhookParser
from linebot.v3.webhook import WebhookHandler  # シグネチャ検証＆ディスパッチ
from linebot.v3.messaging import (
    Configuration, ApiClient,
    MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent,
)

# -----------------------------
# 環境変数
# -----------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ANALYZER_URL = os.getenv("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("line-bot")

# -----------------------------
# LINE クライアント
# -----------------------------
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)
handler = WebhookHandler(CHANNEL_SECRET)

# -----------------------------
# ユーザーごとの一時状態（メモリ）
#   user_id -> {"front": "/tmp/xxx.jpg", "ts": 172...}
# -----------------------------
user_state: Dict[str, Dict[str, str]] = {}
IMAGE_TIMEOUT_SEC = 15 * 60  # 15分で破棄

# -----------------------------
# ユーティリティ
# -----------------------------
def reply_text(reply_token: str, text: str) -> None:
    """テキスト返信（v3）"""
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        logger.error("reply text error: %s", e)

def cleanup_user(user_id: str) -> None:
    """ユーザーの一時計測状態＆ファイル片付け"""
    st = user_state.pop(user_id, None)
    if not st:
        return
    for key in ("front", "side"):
        p = st.get(key)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

def download_line_image_to_temp(message_id: str) -> str:
    """
    LINEの画像を一時ファイルに保存してパスを返す。
    SDK v3では MessagingApiBlob.get_message_content を使う。
    """
    content_bytes: bytes = blob_api.get_message_content(message_id)  # ← v3はこれ
    fd, path = tempfile.mkstemp(prefix="lineimg_", suffix=".jpg")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(content_bytes)
    return path

def post_to_analyzer(front_path: str, side_path: str) -> Optional[dict]:
    """Analyzer の /analyze に2枚送信して結果JSONを返す。失敗時は None"""
    try:
        with open(front_path, "rb") as ff, open(side_path, "rb") as sf:
            files = {
                "front": ("front.jpg", ff, "image/jpeg"),
                "side": ("side.jpg", sf, "image/jpeg"),
            }
            r = requests.post(ANALYZER_URL, files=files, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("analyzer post error: %s", e)
        return None

def build_report_text(result: dict) -> str:
    """AnalyzerのJSONを読みやすいテキストに整形"""
    scores = result.get("scores", {})
    front = result.get("front_metrics", {})
    side = result.get("side_metrics", {})
    advice_list = result.get("advice", [])

    lines = []
    if scores:
        lines.append("【総合スコア】")
        lines.append(
            f"  総合: {scores.get('overall','-')}"
            f" / 姿勢: {scores.get('posture','-')}"
            f" / バランス: {scores.get('balance','-')}"
            f" / 筋肉・脂肪: {scores.get('muscle_fat','-')}"
            f" / ファッション: {scores.get('fashion','-')}"
        )
    if front:
        lines.append("【正面の指標】")
        lines.append(
            f"  肩の高さ差: {front.get('shoulder_delta_y','-')} px, "
            f"骨盤の高さ差: {front.get('pelvis_delta_y','-')} px, "
            f"膝/足首バランス: {front.get('knee_ankle_ratio','-'):.3f}"
            if isinstance(front.get('knee_ankle_ratio'), (int, float))
            else f"  肩の高さ差: {front.get('shoulder_delta_y','-')} px, "
                 f"骨盤の高さ差: {front.get('pelvis_delta_y','-')} px, "
                 f"膝/足首バランス: -"
        )
    if side:
        lines.append("【側面の指標】")
        lines.append(
            f"  頭部前方変位: {side.get('forward_head','-')} px, "
            f"体幹角度: {side.get('trunk_angle','-')}°, "
            f"骨盤角度: {side.get('pelvic_angle','-')}°"
        )
    if advice_list:
        lines.append("【アドバイス】")
        for a in advice_list:
            lines.append(f"  - {a}")

    if not lines:
        return "診断結果の解析に失敗しました。時間をおいて再度お試しください。"
    return "\n".join(lines)

def is_timeout(ts: float) -> bool:
    return (time.time() - ts) > IMAGE_TIMEOUT_SEC

# -----------------------------
# ルート
# -----------------------------
@app.get("/")
def index():
    return "LINE Bot is running.", 200

# -----------------------------
# Webhook 受信
# -----------------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except Exception as e:
        logger.error("callback handle error: %r", e)
        return "bad request", 400
    return "OK", 200

# -----------------------------
# イベントハンドラ（テキスト）
# -----------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    user_id = event.source.user_id
    text = event.message.text.strip() if hasattr(event.message, "text") else ""

    # コマンド風
    if text in ("使い方", "help", "ヘルプ"):
        reply_text(event.reply_token,
                   "正面→側面の順で、全身が映る画像を2枚送ってください。\n"
                   "15分以内に2枚目を送らない場合はリセットされます。")
        return
    if text in ("リセット", "reset"):
        cleanup_user(user_id)
        reply_text(event.reply_token, "状態をリセットしました。正面→側面の順でお送りください。")
        return

    reply_text(event.reply_token, "受け取りました。画像は正面→側面の順で2枚お送りください。")

# -----------------------------
# イベントハンドラ（画像）
# -----------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event: MessageEvent):
    user_id = event.source.user_id
    try:
        # 画像を一時保存
        saved_path = download_line_image_to_temp(event.message.id)
    except Exception as e:
        logger.error("download error: %s", e)
        reply_text(event.reply_token, "画像の取得に失敗しました。もう一度お試しください。")
        return

    st = user_state.get(user_id)
    # 既に正面がある→今回を側面として解析へ
    if st and "front" in st and not is_timeout(float(st.get("ts", 0))):
        front_path = st["front"]
        side_path = saved_path
        # 解析実行
        result = post_to_analyzer(front_path, side_path)
        # 片付け
        try:
            os.remove(front_path)
        except Exception:
            pass
        try:
            os.remove(side_path)
        except Exception:
            pass
        user_state.pop(user_id, None)

        if result is None:
            reply_text(event.reply_token, "診断API呼び出しでエラーが発生しました。時間をおいて再度お試しください。")
            return

        # 結果整形して返信
        report = build_report_text(result)
        reply_text(event.reply_token, report)
        return

    # それ以外（最初の1枚 or タイムアウト）
    # → 今回を「正面」として保持（厳密な判定はせず順序で運用）
    user_state[user_id] = {"front": saved_path, "ts": str(time.time())}
    reply_text(event.reply_token, "正面画像を受け取りました。続けて側面画像を送ってください。")

# -----------------------------
# エントリポイント
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
