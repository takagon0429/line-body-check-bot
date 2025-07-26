from flask import Flask, request, abort
import requests
import os
from io import BytesIO
from dotenv import load_dotenv

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, ImageMessage, TextSendMessage

load_dotenv()

app = Flask(__name__)
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    message_id = event.message.id
    content = line_bot_api.get_message_content(message_id)
    image_data = BytesIO(content.content)

    response = requests.post(
        "http://localhost:8001/analyze",  # 内部連携URL
        files={"image": ("image.jpg", image_data, "image/jpeg")}
    )

    if response.status_code == 200:
        result = response.json()
        reply_text = (
            f"【AI体型診断】\n"
            f"📏 姿勢スコア：{result.get('姿勢スコア', 'N/A')}\n"
            f"⚖️ ボディバランススコア：{result.get('ボディバランススコア', 'N/A')}\n"
            f"💪 筋肉脂肪スコア：{result.get('筋肉脂肪スコア', 'N/A')}\n"
            f"👗 ファッション映えスコア：{result.get('ファッション映えスコア', 'N/A')}\n"
            f"🌟 全体印象スコア：{result.get('全体印象スコア', 'N/A')}"
        )
    else:
        reply_text = "診断中にエラーが発生しました。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
