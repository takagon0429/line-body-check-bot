# app.py  — LINE Bot (v3 SDK) / 2枚画像→Analyzer連携 / healthz付き 完全版

import os
import io
import json
import tempfile
import logging
from datetime import datetime
from typing import Dict, Optional

import requests
from flask import Flask, request, abort

# === LINE SDK v3 ===
from linebot.v3.webhooks import (
    WebhookHandler,
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)

# ----------------------------------
# 環境変数
# ----------------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# Analyzer 側のエンドポイント（必要に応じて調整）
# 例: https://ai-body-check-analyzer.onrender.com/analyze
ANALYZER_URL = os.getenv(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

PORT = int(os.getenv("PORT", "10000"))

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

# ----------------------------------
# Flask & Logger
# ----------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("line-bot")

# ----------------------------------
# LINE API クライアント
# ----------------------------------
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration=config)
messaging_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)
handler = WebhookHandler(CHANNEL_SECRET)

# ----------------------------------
# ユーザーごとの一時収納（front/side）
# 実運用ではRedis/DB推奨。ここでは簡易辞書。
# ----------------------------------
# user_temp[user_id] = {"front": "/tmp/xxx.jpg", "side": "/tmp/yyy.jpg"}
user_temp: Dict[str, Dict[str, str]] = {}

def _tmp_path(prefix: str, suffix: str = ".jpg") -> str:
    fd, path = tempfile.mkstemp(prefix=f"{prefix}_", suffix=suffix)
    os.close(fd)
    return path

def _save_bytes_to_file(data: bytes, path: str):
    with open(path, "wb") as f:
        f.write(data)

def download_line_image_to_temp(message_id: str) -> str:
    """
    LINEサーバーから画像データを取得して /tmp に保存し、保存パスを返す。
    v3 ではバイナリ取得に MessagingApiBlob を使う。
    """
    try:
        resp = blob_api.get_message_content(message_id)
        # SDKの戻りが bytes の実体 or file-like どちらでも対応
        if hasattr(resp, "read"):
            content = resp.read()  # type: ignore[assignment]
        elif isinstance(resp, (bytes, bytearray)):
            content = bytes(resp)
        elif hasattr(resp, "data"):
            content = resp.data  # type: ignore[attr-defined]
        else:
            # 想定外フォーマット
            content = bytes(resp) if resp is not None else b""
        path = _tmp_path("lineimg", ".jpg")
        _save_bytes_to_file(content, path)
        return path
    except Exception as e:
        log.error(f"download error: {e}", exc_info=True)
        raise

def call_analyzer(front_path: str, side_path: str, timeout: int = 25) -> dict:
    """
    Analyzer API を叩いて JSON を返す
    """
    with open(front_path, "rb") as f1, open(side_path, "rb") as f2:
        files = {
            "front": ("front.jpg", f1, "image/jpeg"),
            "side": ("side.jpg", f2, "image/jpeg"),
        }
        r = requests.post(ANALYZER_URL, files=files, timeout=timeout)
    r.raise_for_status()
    return r.json()

def format_result(result: dict) -> str:
    """
    Analyzer の戻り JSON をユーザー向けに整形（存在すれば）
    期待例:
      {
        "scores": {"overall":8.8,"posture":9.8,"balance":7.3,"fashion":8.8,"muscle_fat":9.8},
        "front_metrics": {...}, "side_metrics": {...},
        "advice": ["...","..."]
      }
    """
    parts = []
    scores = result.get("scores")
    if scores:
        parts.append("■スコア")
        for k in ["overall", "posture", "balance", "fashion", "muscle_fat"]:
            if k in scores:
                parts.append(f"・{k}: {scores[k]}")
        parts.append("")

    fm = result.get("front_metrics")
    if fm:
        parts.append("■正面メトリクス")
        for k, v in fm.items():
            parts.append(f"・{k}: {v}")
        parts.append("")

    sm = result.get("side_metrics")
    if sm:
        parts.append("■側面メトリクス")
        for k, v in sm.items():
            parts.append(f"・{k}: {v}")
        parts.append("")

    adv = result.get("advice")
    if adv and isinstance(adv, list) and adv:
        parts.append("■アドバイス")
        for a in adv:
            parts.append(f"・{a}")

    if not parts:
        parts.append("診断結果を受け取りました。詳細は後ほどご案内します。")

    return "\n".join(parts)

def reply_text(reply_token: str, text: str):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)],
            )
        )
    except Exception as e:
        log.error(f"reply text error: {e}", exc_info=True)

# ----------------------------------
# ルーティング
# ----------------------------------

@app.get("/")
def index():
    return (
        "LINE Bot is running. POST /callback by LINE platform. Health: /healthz",
        200,
    )

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/callback")
def callback():
    # LINE 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        log.error(f"callback handle error: {e}", exc_info=True)
        return "Bad Request", 400
    return "OK", 200

# ----------------------------------
# ハンドラ（テキスト）
# ----------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    text = event.message.text.strip() if event.message and hasattr(event.message, "text") else ""
    user_id = getattr(event.source, "user_id", None) or getattr(event.source, "userId", None)

    if text in ("開始", "スタート", "はじめる", "診断"):
        reply_text(
            event.reply_token,
            "姿勢診断を始めます。\n① 正面の全身写真を送ってください。\n② 次に側面の全身写真を送ってください。\n（顔は写ってもOK／服は体の線が分かるもの推奨）",
        )
        # 既存の一時データを初期化
        if user_id:
            user_temp[user_id] = {}
        return

    # デバッグ/ヘルスチェック
    if text.lower() in ("ping", "health", "status"):
        reply_text(event.reply_token, "pong / bot alive")
        return

    # それ以外のテキストには説明を返す
    reply_text(
        event.reply_token,
        "テキストありがとうございます。姿勢診断を行うには、\n正面→側面 の順に全身写真を2枚お送りください。\n（先に「開始」と送ると案内が表示されます）",
    )

# ----------------------------------
# ハンドラ（画像）
# ----------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event: MessageEvent):
    # 画像を一時保存
    try:
        img_path = download_line_image_to_temp(event.message.id)
    except Exception:
        reply_text(event.reply_token, "画像の取得に失敗しました。少し待って再送してください。")
        return

    user_id = getattr(event.source, "user_id", None) or getattr(event.source, "userId", None)
    if not user_id:
        reply_text(event.reply_token, "ユーザー識別に失敗しました。もう一度お試しください。")
        return

    # 受け取り順の推定：（未保存なら front、保存済みなら side）
    # ユーザーが「正面」「側面」とテキスト指定できる作りにもできますが、簡易実装。
    entry = user_temp.get(user_id) or {}
    front_path = entry.get("front")
    side_path = entry.get("side")

    if not front_path:
        entry["front"] = img_path
        user_temp[user_id] = entry
        reply_text(event.reply_token, "正面の写真を受け取りました。次に『側面の全身写真』を送ってください。")
        return

    if not side_path:
        entry["side"] = img_path
        user_temp[user_id] = entry
        # 両方揃ったので Analyzer 呼び出し
        reply_text(event.reply_token, "側面の写真を受け取りました。診断を開始します（数十秒かかることがあります）。")
    else:
        # 既に2枚ある場合、古いものを入れ替える（最後の2枚で診断）
        # 今回は simple に front を上書きする例（必要なら指示UIを拡張）
        entry["front"] = entry["side"]
        entry["side"] = img_path
        user_temp[user_id] = entry
        reply_text(event.reply_token, "画像を更新しました。最新2枚で診断を開始します。")

    # 最新の front/side を確認
    entry = user_temp[user_id]
    front_path = entry.get("front")
    side_path = entry.get("side")

    if not front_path or not side_path:
        # 念のための二重チェック
        reply_text(event.reply_token, "画像が2枚揃っていません。正面→側面の順に送ってください。")
        return

    # Analyzer 呼び出し
    try:
        result = call_analyzer(front_path, side_path, timeout=25)
        pretty = format_result(result)
        reply_text(event.reply_token, f"診断が完了しました。\n\n{pretty}")
    except requests.HTTPError as he:
        log.error(f"analyzer HTTP error: {he}", exc_info=True)
        reply_text(
            event.reply_token,
            "診断API呼び出しでエラーが発生しました。（HTTP）\n時間をおいて再度お試しください。",
        )
    except requests.Timeout:
        log.error("analyzer timeout", exc_info=True)
        reply_text(
            event.reply_token,
            "診断がタイムアウトしました。サーバ負荷の可能性があります。しばらくしてからお試しください。",
        )
    except Exception as e:
        log.error(f"analyzer post error: {e}", exc_info=True)
        reply_text(
            event.reply_token,
            "診断API呼び出しでエラーが発生しました。時間をおいて再度お試しください。",
        )
    finally:
        # 使い終わったら一時ファイルを削除（失敗は握りつぶす）
        for p in [front_path, side_path]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        # 状態もクリア
        user_temp[user_id] = {}

# ----------------------------------
# main
# ----------------------------------
if __name__ == "__main__":
    # Render では自動で PORT が渡されるので host="0.0.0.0", port=PORT
    app.run(host="0.0.0.0", port=PORT)
