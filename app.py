# app.py
import os
import io
import json
import threading
import logging
from typing import Optional, Dict

from flask import Flask, request, jsonify

# --- Flask ----------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 環境変数 -------------------------------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
ANALYZER_URL = os.getenv(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
).strip()

if not CHANNEL_ACCESS_TOKEN:
    logger.warning("LINE_CHANNEL_ACCESS_TOKEN が未設定です")
if not CHANNEL_SECRET:
    logger.warning("LINE_CHANNEL_SECRET が未設定です")
if not ANALYZER_URL:
    logger.warning("ANALYZER_URL が未設定です")

# --- LINE SDK (v3) --------------------------------------------
from linebot.v3.webhook import WebhookParser  # v3は WebhookParser を使う
from linebot.v3.messaging import (
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.exceptions import InvalidSignatureError, ApiException

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
msg_api = MessagingApi(configuration)
parser = WebhookParser(CHANNEL_SECRET)

# --- 簡易的なユーザ状態管理（front/side 指示） ----------------
# 本番は Redis 等へ
user_mode: Dict[str, str] = {}         # user_id -> "front" | "side"
pending_images: Dict[str, Dict[str, bytes]] = {}  # user_id -> {"front": b"...", "side": b"..."}

# --- ユーティリティ -------------------------------------------
def _get_event_type(ev) -> str:
    # v3モデルを直接 import しなくても duck typing で判定
    try:
        return getattr(ev, "type", "")
    except Exception:
        return ""

def _get_message_type(ev) -> str:
    try:
        msg = getattr(ev, "message", None)
        return getattr(msg, "type", "") if msg else ""
    except Exception:
        return ""

def _get_message_id(ev) -> Optional[str]:
    try:
        msg = getattr(ev, "message", None)
        return getattr(msg, "id", None) if msg else None
    except Exception:
        return None

def _get_user_id(ev) -> Optional[str]:
    try:
        src = getattr(ev, "source", None)
        return getattr(src, "user_id", None) if src else None
    except Exception:
        return None

def _safe_text(s: str) -> str:
    return (s or "").strip().lower()

def fetch_image_bytes(message_id: str) -> bytes:
    """
    LINE からバイナリを取得して bytes を返す。
    SDKの戻り値差異を吸収（body/data/iter_content のどれでも拾う）
    """
    resp = msg_api.get_message_content(message_id)
    # いくつかのSDKバージョン互換
    if hasattr(resp, "body") and isinstance(resp.body, (bytes, bytearray)):
        return bytes(resp.body)
    if hasattr(resp, "data") and isinstance(resp.data, (bytes, bytearray)):
        return bytes(resp.data)
    if hasattr(resp, "iter_content"):
        buf = io.BytesIO()
        for chunk in resp.iter_content():
            if chunk:
                buf.write(chunk)
        return buf.getvalue()
    # 直接bytesの可能性
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    raise ValueError("画像の取得に失敗：未知のレスポンス形式でした")

def run_analysis_and_push(user_id: str, front_bytes: Optional[bytes], side_bytes: Optional[bytes]) -> None:
    """
    別スレッドで Analyzer に投げて、結果を push する。
    """
    import requests  # 遅延 import
    files = {}
    try:
        if front_bytes:
            files["front"] = ("front.jpg", io.BytesIO(front_bytes), "image/jpeg")
        if side_bytes:
            files["side"] = ("side.jpg", io.BytesIO(side_bytes), "image/jpeg")
        if not files:
            raise ValueError("解析対象画像がありません")

        r = requests.post(ANALYZER_URL, files=files, timeout=(5, 25))  # connect=5s, read=25s
        r.raise_for_status()
        data = r.json()

        # 整形
        lines = []
        if isinstance(data, dict):
            scores = data.get("scores") or {}
            if scores:
                lines.append(
                    f"総合:{scores.get('overall','-')} 姿勢:{scores.get('posture','-')} "
                    f"バランス:{scores.get('balance','-')} 体型:{scores.get('muscle_fat','-')} "
                    f"ファッション:{scores.get('fashion','-')}"
                )
            front_m = data.get("front_metrics") or {}
            side_m = data.get("side_metrics") or {}
            if front_m:
                lines.append(f"[正面] 肩角:{front_m.get('shoulder_angle','-')} 骨盤傾き:{front_m.get('pelvis_tilt','-')}")
            if side_m:
                lines.append(f"[側面] 前方頭位:{side_m.get('forward_head','-')} 胸椎:{side_m.get('kyphosis','-')}")

            advice = data.get("advice") or []
            for a in advice:
                lines.append(f"・{a}")

        if not lines:
            lines = ["解析は成功しましたが、表示する項目がありませんでした。"]

        msg = "\n".join(lines)

    except Exception as e:
        logger.exception("Analyzer 呼び出しエラー")
        msg = f"解析に失敗しました：{e}"

    # push で送信（失敗しても致命ではない）
    try:
        msg_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=msg)],
            )
        )
    except ApiException as e:
        logger.exception("push_message 失敗: %s", e)

# --- ルート ---------------------------------------------------
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# --- Webhook 受信 ---------------------------------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body_bytes = request.get_data()  # bytes
    body_text = body_bytes.decode("utf-8", errors="ignore")

    try:
        events = parser.parse(body_text, signature)
    except InvalidSignatureError:
        logger.warning("Invalid signature")
        return "signature error", 400
    except Exception as e:
        logger.exception("parse error")
        return f"parse error: {e}", 400

    # 各イベント処理
    for ev in events:
        etype = _get_event_type(ev)
        if etype != "message":
            continue

        mtype = _get_message_type(ev)
        user_id = _get_user_id(ev)
        reply_token = getattr(ev, "reply_token", None)

        if not user_id or not reply_token:
            continue

        # テキストメッセージ（状態切替）
        if mtype == "text":
            text = _safe_text(getattr(getattr(ev, "message", None), "text", ""))
            if text in ("開始", "start", "help", "ヘルプ"):
                user_mode[user_id] = "front"
                msg_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text="OK！まずは『front』または『side』と送って、続けて該当の姿勢写真（画像として直接送信）を送ってください。")],
                    )
                )
            elif text in ("front", "正面"):
                user_mode[user_id] = "front"
                msg_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text="正面モードにしました。正面の姿勢写真を画像として送ってください。")],
                    )
                )
            elif text in ("side", "側面"):
                user_mode[user_id] = "side"
                msg_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text="側面モードにしました。側面の姿勢写真を画像として送ってください。")],
                    )
                )
            else:
                msg_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text="『front』または『side』と送ってから、該当の姿勢写真を画像として送ってください。")],
                    )
                )
            continue

        # 画像メッセージ（取得→解析スレッド起動）
        if mtype == "image":
            mode = user_mode.get(user_id, "front")
            message_id = _get_message_id(ev)

            try:
                img_bytes = fetch_image_bytes(message_id)
            except Exception as e:
                logger.exception("画像取得失敗")
                msg_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(
                            text=(
                                f"画像の取得に失敗：{e}\n"
                                "・LINEアプリから“画像として”直接送ってください（共有URL不可）\n"
                                "・うまくいかない場合は別の画像でもお試しください"
                            )
                        )],
                    )
                )
                continue

            # 一時保存（ユーザ毎）
            store = pending_images.setdefault(user_id, {})
            store[mode] = img_bytes

            # 先に即時返信（ここでブロックしない）
            msg_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text="画像を受け付けました。結果はこの後お送りします。")],
                )
            )

            # 解析に使う front/side を準備（片方だけでもOK）
            front = store.get("front")
            side = store.get("side")
            # 解析投げる
            threading.Thread(
                target=run_analysis_and_push,
                args=(user_id, front, side),
                daemon=True,
            ).start()

            # 好みで片方モードに戻す
            # user_mode[user_id] = "front"

    return "OK", 200


# --- ローカル実行 ---------------------------------------------
if __name__ == "__main__":
    # 開発時は Flask で直接動かす（Renderでは gunicorn）
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
