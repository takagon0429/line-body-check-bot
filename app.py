import os
import io
import json
import hmac
import hashlib
from typing import Dict

import requests
from flask import Flask, request, jsonify

# ====== 環境変数 ======
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")  # 署名検証に使う（任意）
ANALYZER_URL = os.environ.get("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

app = Flask(__name__)

# ユーザーごとの状態（超簡易。Render再起動で消えるがまずはOK）
USER_STATE: Dict[str, str] = {}  # userId -> "front" | "side" | ""

# ============== ユーティリティ ==============
def line_reply(reply_token: str, texts):
    """テキストで返信（複数可）"""
    if isinstance(texts, str):
        texts = [texts]
    messages = [{"type": "text", "text": t[:5000]} for t in texts]
    resp = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                 "Content-Type": "application/json"},
        data=json.dumps({"replyToken": reply_token, "messages": messages}),
        timeout=15,
    )
    return resp

def get_image_content(message_id: str) -> bytes:
    """画像バイナリを取得（LINEのコンテンツAPI）"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    r = requests.get(url, headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}, timeout=30)
    r.raise_for_status()
    return r.content  # bytes

def verify_signature(raw_body: bytes, signature: str) -> bool:
    """任意：署名検証（まずはFalseでも動くが本番ではTrue運用推奨）"""
    if not CHANNEL_SECRET:
        return True  # チャンネルシークレット未設定ならスキップ
    mac = hmac.new(CHANNEL_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    import base64
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ============== ヘルスチェック ==============
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# ============== 解析API（curlテスト用・任意） ==============
@app.post("/analyze")
def analyze_direct():
    """ローカル/検証用。front/side ファイルを受け取り、ダミーで返す"""
    if "front" not in request.files and "side" not in request.files:
        return jsonify({"status": "error", "message": "no files"}), 400
    return jsonify({"status": "ok", "message": "files received"}), 200

# ============== LINE Webhook ==============
@app.post("/callback")
def callback():
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    # まずは確実に動かすため、署名検証は pass にしてもOK
    if not verify_signature(raw_body, signature):
        return "signature error", 400

    body = request.get_json(silent=True) or {}
    events = body.get("events", [])

    for ev in events:
        etype = ev.get("type")
        reply_token = ev.get("replyToken", "")
        source = ev.get("source", {})
        user_id = source.get("userId", "")

        # フォロー時案内
        if etype == "follow":
            line_reply(reply_token,
                       "友だち追加ありがとうございます。\n「開始」と送る→「front」か「side」と送る→画像送信 の順で診断できます。")
            continue

        # テキストメッセージ
        if etype == "message" and ev.get("message", {}).get("type") == "text":
            text = ev["message"].get("text", "").strip().lower()
            if text in ("開始", "start"):
                USER_STATE[user_id] = ""
                line_reply(reply_token,
                           "診断を始めます。\nまず「front」か「side」と送ってから、該当の写真を1枚送ってください。")
            elif text in ("front", "side"):
                USER_STATE[user_id] = text
                line_reply(reply_token, f"了解。「{text}」の写真を送ってください。")
            else:
                line_reply(reply_token,
                           "コマンドが分かりません。\n「開始」→「front」or「side」→画像 の順で送ってください。")
            continue

        # 画像メッセージ
        if etype == "message" and ev.get("message", {}).get("type") in ("image", "file"):
            expect = USER_STATE.get(user_id, "")
            if expect not in ("front", "side"):
                line_reply(reply_token, "先に「front」または「side」と送ってから、該当の写真を送ってください。")
                continue

            message_id = ev["message"].get("id")
            if not message_id:
                line_reply(reply_token, "画像の取得に失敗しました（messageIdなし）。もう一度お試しください。")
                continue

            try:
                img_bytes = get_image_content(message_id)  # bytes
            except Exception as e:
                line_reply(reply_token, f"画像の取得に失敗しました：{e}\n別の画像でもお試しください。")
                continue

            # Analyzer へマルチパートPOST
            files = {expect: ("image.jpg", io.BytesIO(img_bytes), "image/jpeg")}
            try:
                res = requests.post(ANALYZER_URL, files=files, timeout=60)
                if res.status_code != 200:
                    line_reply(reply_token, f"解析APIエラー: HTTP {res.status_code}")
                    continue
                data = res.json()
            except Exception as e:
                line_reply(reply_token, f"解析API呼び出しで失敗しました：{e}")
                continue

            # 結果整形
            advice = data.get("advice", [])
            scores = data.get("scores", {})
            summary = []
            if scores:
                summary.append("【スコア】")
                summary.append(" / ".join(f"{k}:{v}" for k, v in scores.items()))
            if advice:
                summary.append("【アドバイス】")
                summary.extend(f"・{a}" for a in advice)

            if not summary:
                summary = ["解析が完了しました。詳細は後続バージョンで表示します。"]

            line_reply(reply_token, "\n".join(summary))

            # 1回でリセット（必要なら連続受付に変更可）
            USER_STATE[user_id] = ""
            continue

    return "OK", 200

if __name__ == "__main__":
    # ローカル起動用（RenderではProcfile→gunicornで起動）
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
