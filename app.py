import os
import tempfile
import requests
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.messaging.api.messaging_api_blob import MessagingApiBlob
from linebot.v3.webhooks import MessageEvent, ImageMessageContent, TextMessageContent

app = Flask(__name__)

# LINEチャネル情報
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
messaging_api = MessagingApi(config)
blob_api = MessagingApiBlob(config)
handler = WebhookHandler(CHANNEL_SECRET)

# Analyzer URL（環境変数から）
ANALYZER_URL = os.getenv("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com/analyze")

# ユーザーごとの画像保持用（簡易）
user_images = {}

@app.route("/", methods=["GET"])
def root():
    return "LINE Bot is running.", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK", 200


@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id

    # 画像を一時保存
    saved_path = download_line_image_to_temp(message_id)

    # 1枚目か2枚目かを判定
    if user_id not in user_images:
        user_images[user_id] = {"front": saved_path, "side": None}
        reply_text(event.reply_token, "正面の写真を受け取りました。次に横向きの写真を送ってください。")
    else:
        if user_images[user_id]["side"] is None:
            user_images[user_id]["side"] = saved_path
            reply_text(event.reply_token, "横向きの写真を受け取りました。分析を開始します…")
            analyze_and_reply(user_id, event.reply_token)
        else:
            # リセットして再スタート
            user_images[user_id] = {"front": saved_path, "side": None}
            reply_text(event.reply_token, "新しい正面の写真を受け取りました。次に横向きの写真を送ってください。")


@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event):
    text = event.message.text.strip()
    if text.lower() in ["reset", "リセット"]:
        user_images.pop(event.source.user_id, None)
        reply_text(event.reply_token, "データをリセットしました。正面の写真を送ってください。")
    else:
        reply_text(event.reply_token, "正面の写真 → 横向きの写真 の順に送信してください。")


def download_line_image_to_temp(message_id):
    """MessagingApiBlob を使ってLINE画像を取得・一時保存"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
        content = blob_api.get_message_content(message_id)
        for chunk in content.iter_content():
            tf.write(chunk)
        return tf.name


def analyze_and_reply(user_id, reply_token):
    """Analyzer に画像2枚を送り、結果をLINEに返信"""
    front_path = user_images[user_id]["front"]
    side_path = user_images[user_id]["side"]

    if not os.path.exists(front_path) or not os.path.exists(side_path):
        reply_text(reply_token, "画像ファイルが見つかりませんでした。もう一度送信してください。")
        return

    try:
        with open(front_path, "rb") as f1, open(side_path, "rb") as f2:
            resp = requests.post(
                ANALYZER_URL,
                files={"front": f1, "side": f2},
                timeout=60
            )
        resp.raise_for_status()
    except Exception as e:
        reply_text(reply_token, f"分析サーバーへの接続に失敗しました: {e}")
        return

    try:
        data = resp.json()
    except Exception:
        reply_text(reply_token, "分析結果の形式が不正です。")
        return

    # 結果を成形
    scores = data.get("scores", {})
    advice_list = data.get("advice", [])
    advice_text = "\n".join(f"- {a}" for a in advice_list)

    result_text = (
        "📊 診断結果\n"
        f"全体スコア: {scores.get('overall', 'N/A')}\n"
        f"姿勢: {scores.get('posture', 'N/A')}\n"
        f"バランス: {scores.get('balance', 'N/A')}\n"
        f"筋肉/脂肪: {scores.get('muscle_fat', 'N/A')}\n"
        f"ファッション映え: {scores.get('fashion', 'N/A')}\n\n"
        f"💡 アドバイス:\n{advice_text or '特になし'}"
    )

    reply_text(reply_token, result_text)

    # 使い終わったらリセット
    user_images.pop(user_id, None)


def reply_text(reply_token, text):
    """テキスト返信"""
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        print(f"Reply error: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
