# app.py
import os
from io import BytesIO
from threading import Thread

from flask import Flask, request, abort
import requests
from dotenv import load_dotenv

# ==== LINE SDK v3 ====
# v3 では WebhookParser を使ってイベントを取り出す
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)

# -----------------------
# .env の読み込み & 環境変数
# -----------------------
load_dotenv()

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

# Analyzer のURL（Renderの推奨：POST /analyze、Health: GET /healthz）
ANALYZER_URL = os.getenv(
    "ANALYZER_URL",
    "https://ai-body-check-analyzer.onrender.com/analyze",
)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("[WARN] LINEの環境変数が未設定です。BOT機能は動作しません。")

# LINEクライアント（v3の正しい初期化）
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
msg_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)

# Webhook署名パーサ
parser = WebhookParser(CHANNEL_SECRET)

# 状態管理（ユーザーごとに次に期待する画像: "front" or "side"）
EXPECTING: dict[str, str] = {}

# Flask
app = Flask(__name__)


# -----------------------
# ヘルスチェック
# -----------------------
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200


# -----------------------
# 日本語整形ヘルパー
# -----------------------
def _fmt_deg(v):
    try:
        return f"{float(v):.1f}°"
    except Exception:
        s = str(v)
        return s if "°" in s else f"{s}°"


def _fmt_cm(v):
    try:
        return f"{float(v):.1f}cm"
    except Exception:
        s = str(v)
        return s if s.endswith("cm") else f"{s}cm"


def format_analyzer_result_jp(result: dict) -> str:
    """
    Analyzerの返却JSONを日本語の見出し・単位付きに整形して1本文にする
    想定入力例：
      {
        "scores": {"balance": 7.0, "fashion": 8.0, "muscle_fat": 8.2, "overall": 7.3, "posture": 6.0},
        "front_metrics": {"pelvis_tilt": 179.9, "shoulder_angle": 178.3},
        "side_metrics": {"forward_head": 2.9, "kyphosis": "軽度"},
        "advice": ["...","..."]
      }
    """
    scores = result.get("scores", {}) or {}
    jp_scores = {
        "バランス": scores.get("balance"),
        "ファッション映え度": scores.get("fashion"),
        "筋肉・脂肪のつき方": scores.get("muscle_fat"),
        "全体印象": scores.get("overall"),
        "姿勢": scores.get("posture"),
    }
    score_lines = []
    for k, v in jp_scores.items():
        if v is not None:
            try:
                score_lines.append(f"- {k}：{float(v):.1f}")
            except Exception:
                score_lines.append(f"- {k}：{v}")

    front = result.get("front_metrics", {}) or {}
    pelvis = front.get("pelvis_tilt")
    shoulder = front.get("shoulder_angle")
    front_lines = []
    if pelvis is not None:
        front_lines.append(f"- 骨盤の傾き：{_fmt_deg(pelvis)}")
    if shoulder is not None:
        front_lines.append(f"- 肩の角度差：{_fmt_deg(shoulder)}")

    side = result.get("side_metrics", {}) or {}
    fwd_head = side.get("forward_head")
    kyphosis = side.get("kyphosis")
    side_lines = []
    if fwd_head is not None:
        side_lines.append(f"- 頭の前方変位：{_fmt_cm(fwd_head)}")
    if kyphosis is not None:
        side_lines.append(f"- 背中の丸まり（胸椎後弯）：{kyphosis}")

    adv = result.get("advice", []) or []
    adv_lines = [f"- {a}" for a in adv if a]

    parts = []
    if score_lines:
        parts.append("【スコア】\n" + "\n".join(score_lines))
    if front_lines:
        parts.append("【正面評価】\n" + "\n".join(front_lines))
    if side_lines:
        parts.append("【側面評価】\n" + "\n".join(side_lines))
    if adv_lines:
        parts.append("【アドバイス】\n" + "\n".join(adv_lines))

    if not parts:
        import json as _json
        return "解析結果：\n" + _json.dumps(result, ensure_ascii=False, indent=2)

    return "\n\n".join(parts)


# -----------------------
# Analyzer 呼び出し（短いタイムアウト）
# -----------------------
def post_to_analyzer(front_bytes: bytes | None, side_bytes: bytes | None, timeout=(5, 20)) -> dict:
    files = {}
    if front_bytes:
        files["front"] = ("front.jpg", front_bytes, "image/jpeg")
    if side_bytes:
        files["side"] = ("side.jpg", side_bytes, "image/jpeg")

    resp = requests.post(ANALYZER_URL, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# -----------------------
# LINE画像バイト取得（v3 Blob API）
# -----------------------
def get_image_bytes(message_id: str) -> bytes:
    """
    LINEの画像コンテンツをbytesで返す。
    SDKの戻りがbytesでもストリームでも吸収して返す。
    """
    content = blob_api.get_message_content(message_id)

    # v3 実装は bytes を返すことが多い
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)

    # Response-like の可能性にも対応
    if hasattr(content, "iter_content"):
        buf = BytesIO()
        for chunk in content.iter_content(chunk_size=1024 * 1024):
            if chunk:
                buf.write(chunk)
        return buf.getvalue()

    if hasattr(content, "read"):
        return content.read()

    raise TypeError(f"Unsupported content type from blob API: {type(content)}")


# -----------------------
# 安全リプライ
# -----------------------
def safe_reply(reply_token: str, text: str):
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])],
            )
        )
    except Exception as e:
        print(f"[WARN] reply failed: {e}")


# -----------------------
# 解析→push（非同期で本番向け）
# -----------------------
def analyze_and_push(user_id: str, front_bytes: bytes, side_bytes: bytes):
    # ウォームアップ（失敗は無視）
    try:
        healthz = ANALYZER_URL.replace("/analyze", "/healthz")
        requests.get(healthz, timeout=2)
    except Exception:
        pass

    try:
        result = post_to_analyzer(front_bytes, side_bytes, timeout=(5, 40))
        reply_text = format_analyzer_result_jp(result)
    except requests.Timeout:
        reply_text = "解析サーバが混み合っています。時間をおいて再試行してください。"
    except requests.RequestException as e:
        print(f"[ERROR] analyzer request failed: {e}")
        reply_text = "解析サーバへの送信に失敗しました。時間をおいて再試行してください。"
    except Exception as e:
        print(f"[ERROR] result formatting failed: {e}")
        reply_text = "解析中にエラーが発生しました。もう一度お試しください。"

    # push送信（Webhook処理をブロックしない）
    try:
        msg_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=reply_text[:5000])],
            )
        )
    except Exception as e:
        print(f"[ERROR] push failed: {e}")


# -----------------------
# Webhook
# -----------------------
@app.post("/callback")
def callback():
    # 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        print(f"[ERROR] signature parse: {e}")
        abort(400)

    for ev in events:
        if not isinstance(ev, MessageEvent):
            continue

        reply_token = ev.reply_token
        user_id = getattr(ev.source, "user_id", None)

        # テキスト
        if isinstance(ev.message, TextMessageContent):
            text = (ev.message.text or "").strip().lower()

            if text in ("開始", "start", "かいし"):
                EXPECTING[user_id] = "front"
                safe_reply(
                    reply_token,
                    "姿勢チェックを開始します。\n1) 「front」と入力 → 正面写真を送信\n2) 「side」と入力 → 側面写真を送信\n（結果は解析完了後にお送りします）",
                )
                continue

            if text == "front":
                EXPECTING[user_id] = "front"
                safe_reply(reply_token, "正面(front)の画像を送ってください。")
                continue

            if text == "side":
                EXPECTING[user_id] = "side"
                safe_reply(reply_token, "側面(side)の画像を送ってください。")
                continue

            safe_reply(
                reply_token,
                "使い方:\n1) 「開始」\n2) 「front」と入力→正面写真\n3) 「side」と入力→側面写真\n解析は完了後にお送りします。",
            )
            continue

        # 画像
        if isinstance(ev.message, ImageMessageContent):
            if not user_id:
                safe_reply(reply_token, "ユーザーIDを取得できませんでした。もう一度お試しください。")
                continue

            expecting = EXPECTING.get(user_id)
            if expecting not in ("front", "side"):
                safe_reply(
                    reply_token,
                    "まず「開始」と入力し、続けて「front」または「side」を入力してから画像を送ってください。",
                )
                continue

            # 画像バイト取得
            try:
                content_bytes = get_image_bytes(ev.message.id)
            except Exception as e:
                print(f"[ERROR] get_image_bytes: {e}")
                safe_reply(
                    reply_token,
                    "画像の取得に失敗しました。LINEアプリから“画像として”送信してください（共有URL不可）。",
                )
                continue

            key_front = f"{user_id}:front"
            key_side = f"{user_id}:side"

            if expecting == "front":
                app.config[key_front] = content_bytes
                EXPECTING[user_id] = "side"
                safe_reply(reply_token, "frontを受け取りました。次に「side」と入力→側面の画像を送ってください。")
                continue

            if expecting == "side":
                app.config[key_side] = content_bytes

                front_bytes = app.config.get(key_front)
                side_bytes = app.config.get(key_side)

                if not front_bytes:
                    EXPECTING[user_id] = "front"
                    safe_reply(reply_token, "front画像が未取得です。先に「front」と入力→正面画像を送ってください。")
                    continue

                # 即時応答（Webhookは詰まらせない）
                safe_reply(reply_token, "解析を開始しました。完了次第、結果をお送りします。")

                # バックグラウンドで解析→push
                Thread(
                    target=analyze_and_push,
                    args=(user_id, front_bytes, side_bytes),
                    daemon=True,
                ).start()

                # 後始末
                app.config.pop(key_front, None)
                app.config.pop(key_side, None)
                EXPECTING.pop(user_id, None)
                continue

    return "OK", 200


# -----------------------
# 開発用エントリ
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
