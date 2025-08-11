import os
import hmac
import hashlib
import base64
import json
import requests
from flask import Flask, request, abort, jsonify

# ====== 環境変数 ======
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.environ.get("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

# ====== Flask アプリ ======
app = Flask(__name__)

@app.get("/")
def root():
    return "OK Bot", 200

def _reply_text(reply_token: str, text: str):
    """LINE Messaging API を直接叩いてテキスト返信"""
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post(url, headers=headers, json=body, timeout=10)
    if r.status_code >= 300:
        print("[/callback ERROR] reply failed:", r.status_code, r.text)

@app.post("/callback")
def callback():
    # 署名と本文を取得
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 受信ログ（短縮表示）
    print("[/callback] got request", "sig_len=", len(signature), "body_len=", len(body))

    # --- HMAC-SHA256 による事前署名検証 ---
    try:
        mac = hmac.new(CHANNEL_SECRET.encode("utf-8"),
                       body.encode("utf-8"),
                       hashlib.sha256).digest()
        computed = base64.b64encode(mac).decode("utf-8")
        print("[/callback] signature recv=", signature[:10], "...", "calc=", computed[:10], "...")
        if not hmac.compare_digest(signature, computed):
            print("[/callback ERROR] SignatureMismatch")
            abort(400, "Invalid signature (pre-check)")
    except Exception as e:
        print("[/callback ERROR] PreCheck", type(e).__name__, str(e))
        abort(400, f"precheck error: {e}")

    # --- イベント処理（最小実装）---
    try:
        data = json.loads(body)
        events = data.get("events", [])
        for ev in events:
            etype = ev.get("type")
            reply_token = ev.get("replyToken")
            # メッセージイベントのみ処理
            if etype == "message" and reply_token:
                msg = ev.get("message", {})
                mtype = msg.get("type")
                if mtype == "text":
                    user_text = msg.get("text", "")
                    _reply_text(reply_token, f"受け取りました：{user_text}")
                elif mtype == "image":
                    # まずは確認用の応答だけ（後で解析フローを追加）
                    _reply_text(reply_token, "画像を受け取りました。正面と横を順番に送ってください。")

        return "OK", 200

    except Exception as e:
        print("[/callback ERROR] Handler", type(e).__name__, str(e))
        print("[/callback BODY]", body[:500])
        print("[/callback SIGNATURE]", signature[:30], "...")
        abort(400, str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
