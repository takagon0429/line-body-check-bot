import os
from io import BytesIO
from threading import Thread
from typing import Dict, Any, List

from flask import Flask, request, abort
from dotenv import load_dotenv
import requests

# ---- LINE v3 SDK ----
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)

load_dotenv()

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.getenv("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

app = Flask(__name__)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("[WARN] LINE env not set. Bot features will not work.")

# LINE clients
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
msg_api = MessagingApi(api_client)
blob_api = MessagingApiBlob(api_client)
parser = WebhookParser(CHANNEL_SECRET)

# 次に期待する画像状態
EXPECTING: Dict[str, str] = {}

# ---------- health ----------
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# ---------- 日本語整形 ----------
def _fmt_deg(v):
    try:
        if isinstance(v, (int, float)):
            return f"{v:.1f}°"
        s = str(v)
        return s if "°" in s else f"{s}°"
    except Exception:
        return str(v)

def _fmt_cm(v):
    try:
        if isinstance(v, (int, float)):
            return f"{v:.1f}cm"
        s = str(v)
        return s if s.endswith("cm") else f"{s}cm"
    except Exception:
        return str(v)

def format_analyzer_result_jp(result: Dict[str, Any]) -> str:
    scores = result.get("scores", {}) or {}
    jp_scores = {
        "バランス": scores.get("balance"),
        "ファッション映え度": scores.get("fashion"),
        "筋肉・脂肪のつき方": scores.get("muscle_fat"),
        "全体印象": scores.get("overall"),
        "姿勢": scores.get("posture"),
    }
    score_lines: List[str] = []
    for k, v in jp_scores.items():
        if v is not None:
            try:
                v_num = float(v)
                score_lines.append(f"- {k}：{v_num:.1f}")
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
        return "解析結果の整形に失敗しました。もう一度お試しください。"

    return "\n\n".join(parts)

# ---------- analyzer 呼び出し ----------
def post_to_analyzer(front_bytes: bytes | None, side_bytes: bytes | None, timeout=(5, 55)) -> Dict[str, Any]:
    files = {}
    if front_bytes:
        files["front"] = ("front.jpg", front_bytes, "image/jpeg")
    if side_bytes:
        files["side"] = ("side.jpg", side_bytes, "image/jpeg")
    resp = requests.post(ANALYZER_URL, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def get_image_bytes(message_id: str) -> bytes:
    # v3はbytesが返る実装。将来の変更に備えて念のため両対応
    content = blob_api.get_message_content(message_id)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if hasattr(content, "iter_content"):
        buf = BytesIO()
        for chunk in content.iter_content(1024 * 1024):
            if chunk:
                buf.write(chunk)
        return buf.getvalue()
    if hasattr(content, "read"):
        return content.read()
    raise TypeError(f"Unsupported blob content: {type(content)}")

def safe_reply(reply_token: str, text: str):
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])]
            )
        )
    except Exception as e:
        print(f"[WARN] reply failed: {e}")

def analyze_and_push(user_id: str, front_bytes: bytes, side_bytes: bytes):
    # cold start対策（hibernation回避）
    try:
        healthz = ANALYZER_URL.replace("/analyze", "/healthz")
        requests.get(healthz, timeout=2)
    except Exception:
        pass

    out_text = "解析に失敗しました。時間をおいて再試行してください。"
    try:
        result = post_to_analyzer(front_bytes, side_bytes)
        out_text = format_analyzer_result_jp(result)
    except requests.Timeout:
        out_text = "解析サーバが混み合っています。時間をおいて再試行してください。"
    except requests.RequestException as e:
        print(f"[ERROR] analyzer request: {e}")
    except Exception as e:
        print(f"[ERROR] formatting: {e}")

    try:
        msg_api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=out_text[:5000])]
            )
        )
    except Exception as e:
        print(f"[ERROR] push failed: {e}")

# ---------- webhook ----------
@app.post("/callback")
def callback():
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
                if user_id:
                    EXPECTING[user_id] = "front"
                safe_reply(reply_token, "姿勢チェックを始めます。\n「front」と入力して正面写真→続けて「side」と入力して側面写真を送ってください。")
                continue
            if text == "front":
                if user_id:
                    EXPECTING[user_id] = "front"
                safe_reply(reply_token, "正面(front)の写真を送ってください。")
                continue
            if text == "side":
                if user_id:
                    EXPECTING[user_id] = "side"
                safe_reply(reply_token, "側面(side)の写真を送ってください。")
                continue
            safe_reply(reply_token, "使い方:\n1) 「開始」\n2) 「front」と入力→正面写真\n3) 「side」と入力→側面写真\n解析完了後に結果をお送りします。")
            continue

        # 画像
        if isinstance(ev.message, ImageMessageContent):
            if not user_id:
                safe_reply(reply_token, "ユーザーIDの取得に失敗しました。もう一度お試しください。")
                continue
            expecting = EXPECTING.get(user_id)
            if expecting not in ("front", "side"):
                safe_reply(reply_token, "まず「開始」と入力し、その後「front」または「side」を入力してから画像を送ってください。")
                continue

            try:
                content_bytes = get_image_bytes(ev.message.id)
            except Exception as e:
                print(f"[ERROR] blob: {e}")
                safe_reply(reply_token, "画像の取得に失敗しました。LINEから“画像として”送信してください（共有URL不可）。")
                continue

            k_front = f"{user_id}:front"
            k_side  = f"{user_id}:side"

            if expecting == "front":
                app.config[k_front] = content_bytes
                EXPECTING[user_id] = "side"
                safe_reply(reply_token, "front を受け取りました。次に「side」と入力→側面の写真を送ってください。")
                continue

            if expecting == "side":
                app.config[k_side] = content_bytes
                front_bytes = app.config.get(k_front)
                side_bytes  = app.config.get(k_side)
                if not front_bytes:
                    EXPECTING[user_id] = "front"
                    safe_reply(reply_token, "front画像が未取得です。先に「front」と入力→正面写真を送ってください。")
                    continue

                safe_reply(reply_token, "解析を開始しました。完了次第、結果をお送りします。")
                Thread(target=analyze_and_push, args=(user_id, front_bytes, side_bytes), daemon=True).start()

                # 後始末
                app.config.pop(k_front, None)
                app.config.pop(k_side, None)
                EXPECTING.pop(user_id, None)
                continue

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
