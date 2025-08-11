import os
import hmac
import hashlib
import base64
import json
import time
from io import BytesIO

import requests
from flask import Flask, request, abort

# ====== 環境変数 ======
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.environ.get("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

# ====== Flask ======
app = Flask(__name__)

@app.get("/")
def root():
    return "OK Bot", 200

# ====== 簡易セッション（ユーザーごとに front を一時保存）======
# 注意: Render Free は再起動で消えます。実運用は Redis 等に置き換え推奨。
USER_STATE = {}  # userId -> {"front": bytes | None, "t": epoch}

STATE_TTL_SEC = 20 * 60  # 20分で期限切れ

def _now():
    return int(time.time())

def _reply_text(reply_token: str, text: str):
    """LINE Messaging APIへテキスト返信"""
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=headers, json=body, timeout=15)
    if r.status_code >= 300:
        print("[reply ERROR]", r.status_code, r.text)

def _download_image_content(message_id: str) -> bytes:
    """画像バイナリを LINE のデータAPIから取得"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.content

def _analyze(front_bytes: bytes, side_bytes: bytes) -> dict:
    """Analyzer API に2枚送って結果JSONを受け取る"""
    files = {
        "front": ("front.jpg", BytesIO(front_bytes), "image/jpeg"),
        "side": ("side.jpg", BytesIO(side_bytes), "image/jpeg"),
    }
    r = requests.post(ANALYZER_URL, files=files, timeout=40)
    # Analyzer は 200/JSON を返す想定
    r.raise_for_status()
    return r.json()

def _format_result(res: dict) -> str:
    """Analyzer のJSONを、LINE返信用の日本語テキストに整形"""
    scores = res.get("scores", {})
    advice = res.get("advice", [])
    front = res.get("front_metrics", {})
    side = res.get("side_metrics", {})

    s_overall = scores.get("overall")
    s_posture = scores.get("posture")
    s_balance = scores.get("balance")
    s_mf = scores.get("muscle_fat")
    s_fashion = scores.get("fashion")

    lines = []
    lines.append("🧍‍♂️ AI姿勢・体バランスチェック 結果")
    if s_overall is not None:
        lines.append(f"総合: {s_overall:.1f} / 10")
    if s_posture is not None:
        lines.append(f"姿勢: {s_posture:.1f} / 10")
    if s_balance is not None:
        lines.append(f"左右バランス: {s_balance:.1f} / 10")
    if s_mf is not None:
        lines.append(f"筋肉/体脂肪: {s_mf:.1f} / 10")
    if s_fashion is not None:
        lines.append(f"見た目印象: {s_fashion:.1f} / 10")

    # 軽い指標を1～2個
    if "trunk_angle" in side:
        lines.append(f"体幹角度（横）: {side['trunk_angle']:.1f}°")
    if "pelvic_angle" in side:
        lines.append(f"骨盤角度（横）: {side['pelvic_angle']:.1f}°")

    if advice:
        lines.append("")
        lines.append("📌 アドバイス:")
        # 2～3個に抑える
        for a in advice[:3]:
            lines.append(f"・{a}")

    lines.append("")
    lines.append("※簡易診断です。正確な評価はジムでの対面チェックで行います。")
    return "\n".join(lines)

# ====== /callback（署名前検証 + 最小ハンドリング）======
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 受信ログ
    print("[/callback] got request", "sig_len=", len(signature), "body_len=", len(body))

    # --- 署名の事前検証 ---
    mac = hmac.new(CHANNEL_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    computed = base64.b64encode(mac).decode("utf-8")
    print("[/callback] signature recv=", signature[:10], "...", "calc=", computed[:10], "...")
    if not hmac.compare_digest(signature, computed):
        print("[/callback ERROR] SignatureMismatch")
        abort(400, "Invalid signature")

    # --- イベントを処理 ---
    try:
        data = json.loads(body)
        events = data.get("events", [])
        for ev in events:
            if ev.get("type") != "message":
                continue
            reply_token = ev.get("replyToken")
            if not reply_token:
                continue

            source = ev.get("source", {})
            user_id = source.get("userId", "unknown")
            msg = ev.get("message", {})
            mtype = msg.get("type")

            # テキスト
            if mtype == "text":
                text = msg.get("text", "").strip()
                if text in ("リセット", "reset", "クリア"):
                    USER_STATE.pop(user_id, None)
                    _reply_text(reply_token, "状態をリセットしました。正面→横の順で画像を送ってください。")
                else:
                    _reply_text(reply_token, f"受け取りました：{text}\n正面→横の順で画像を送ってください。")
                continue

            # 画像
            if mtype == "image":
                # 画像を取得
                message_id = msg.get("id")
                try:
                    img_bytes = _download_image_content(message_id)
                except Exception as e:
                    print("[download ERROR]", type(e).__name__, str(e))
                    _reply_text(reply_token, "画像の取得に失敗しました。もう一度お送りください。")
                    continue

                # TTL 超過はクリア
                st = USER_STATE.get(user_id)
                if st and _now() - st.get("t", 0) > STATE_TTL_SEC:
                    USER_STATE.pop(user_id, None)
                    st = None

                if not st or not st.get("front"):
                    # まだ正面がない → これを正面として保持
                    USER_STATE[user_id] = {"front": img_bytes, "t": _now()}
                    _reply_text(reply_token, "正面の画像を受け取りました。次に**横**の画像を送ってください。")
                else:
                    # すでに正面あり → 今回は横として解析へ
                    front_bytes = st["front"]
                    side_bytes = img_bytes
                    # 使い終わったらクリア
                    USER_STATE.pop(user_id, None)

                    try:
                        res = _analyze(front_bytes, side_bytes)
                        text = _format_result(res)
                        _reply_text(reply_token, text)
                    except requests.HTTPError as he:
                        print("[analyze HTTPError]", he.response.status_code, he.response.text[:200])
                        _reply_text(reply_token, "解析サーバーが混み合っています。少し時間をおいて再度お試しください。")
                    except Exception as e:
                        print("[analyze ERROR]", type(e).__name__, str(e))
                        _reply_text(reply_token, "解析に失敗しました。画像は「正面→横」の順で、全身が写るように送ってください。")

        return "OK", 200

    except Exception as e:
        print("[/callback ERROR] Handler", type(e).__name__, str(e))
        print("[/callback BODY]", body[:500])
        print("[/callback SIGNATURE]", signature[:30], "...")
        abort(400, str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
