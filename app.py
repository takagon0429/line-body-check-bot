import os
import json
from flask import Flask, request, abort
from hmac import compare_digest

from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import (
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
import requests

# ==== 環境変数 ====
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL = os.getenv("ANALYZER_URL", "")  # 例: https://ai-body-check-analyzer.onrender.com/analyze

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")
if not ANALYZER_URL:
    raise RuntimeError("ANALYZER_URL を設定してください。")

# ==== Flask ====
app = Flask(__name__)

# ==== LINE SDK v3 初期化 ====
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# ユーザー毎の進行状況をメモリに保持（Render再起動で消えます。まずはMVP）
# state[userId] = {"front": "/tmp/xxx_front.jpg" or None, "side": "/tmp/xxx_side.jpg" or None}
state = {}

@app.get("/")
def health():
    return "OK", 200

@app.post("/callback")
def callback():
    # 署名検証
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        # 署名エラーやパース失敗など
        app.logger.error(f"callback handle error: {repr(e)}")
        return "NG", 400
    return "OK", 200

# ====== メッセージ（テキスト） ======
@handler.add(MessageEvent, message=TextMessageContent)
def on_text_message(event: MessageEvent):
    user_id = event.source.user_id
    text = event.message.text.strip()

    with ApiClient(config) as api_client:
        messaging_api = MessagingApi(api_client)

        if text in ["測定", "計測", "start", "スタート"]:
            # 状態リセット
            state[user_id] = {"front": None, "side": None}
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(text="測定を開始します。\n1) まず正面の全身写真を送ってください📷\n2) つづいて側面の全身写真も送ってください。\n（背景はできるだけシンプル、全身が入るようにお願いします）")
                    ],
                )
            )
            return

        # その他テキストはヘルプ
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text="「測定」と送ると、正面→側面の順で写真を受け付けて分析します🙆")
                ],
            )
        )

# ====== メッセージ（画像） ======
@handler.add(MessageEvent, message=ImageMessageContent)
def on_image_message(event: MessageEvent):
    user_id = event.source.user_id

    # 状態が無ければ初期化（測定コマンド無しでも受け取れるように）
    if user_id not in state:
        state[user_id] = {"front": None, "side": None}

    tmp_front = f"/tmp/{user_id}_front.jpg"
    tmp_side  = f"/tmp/{user_id}_side.jpg"

    # 画像データを取得
    with ApiClient(config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        content = blob_api.get_message_content(message_id=event.message.id)

        # 次に埋めるべきスロットを決める
        target_path = tmp_front if state[user_id]["front"] is None else (
            tmp_side if state[user_id]["side"] is None else None
        )

        if target_path is None:
            # すでに2枚そろっている → リセットして front に上書き
            state[user_id] = {"front": None, "side": None}
            target_path = tmp_front

        # 保存（content は urllib3.response.HTTPResponse）
        with open(target_path, "wb") as f:
            # chunk で書き込む
            chunk = content.read(1024 * 1024)
            while chunk:
                f.write(chunk)
                chunk = content.read(1024 * 1024)

    # 状態更新
    if target_path == tmp_front:
        state[user_id]["front"] = tmp_front
        next_msg = "正面を受け取りました。次は側面の全身写真を送ってください。"
    else:
        state[user_id]["side"] = tmp_side
        next_msg = "側面を受け取りました。分析を開始します…⏳"

    # 返信（受け取り確認）
    with ApiClient(config) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=next_msg)],
            )
        )

    # 2枚そろったら Analyzer へPOST
    if state[user_id]["front"] and state[user_id]["side"]:
        try:
            files = {
                "front": open(state[user_id]["front"], "rb"),
                "side":  open(state[user_id]["side"], "rb"),
            }
            resp = requests.post(ANALYZER_URL, files=files, timeout=60)
            files["front"].close()
            files["side"].close()

            if resp.status_code != 200:
                raise RuntimeError(f"Analyzer HTTP {resp.status_code}: {resp.text}")

            data = resp.json()
            # 期待するJSON例：
            # {
            #   "scores":{"overall":8.8,"posture":9.8,"balance":7.3,"muscle_fat":9.8,"fashion":8.8},
            #   "advice":["～～～", "～～～"],
            #   "front_metrics": {...},
            #   "side_metrics": {...}
            # }

            scores = data.get("scores", {})
            advice = data.get("advice", [])

            # 返信テキスト整形（軽量）
            score_lines = []
            for k in ["overall", "posture", "balance", "muscle_fat", "fashion"]:
                if k in scores:
                    score_lines.append(f"{k}: {scores[k]}")
            if not score_lines:
                score_lines.append("スコア取得に失敗しました。")

            adv_text = "\n".join(f"・{a}" for a in advice) if advice else "アドバイスは取得できませんでした。"

            result_text = "分析結果\n" + "\n".join(score_lines) + "\n\n" + adv_text

            # 結果をプッシュ（reply_tokenはもう使えないので push でもOKだが、ここでは簡単化で何度も reply しない）
            with ApiClient(config) as api_client:
                messaging_api = MessagingApi(api_client)
                # ユーザーへメッセージを送る（reply ではなく push が安全）
                # v3のpushは PushMessageRequest を使う
                from linebot.v3.messaging import PushMessageRequest
                messaging_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=result_text[:4900])]  # 文字数セーフティ
                    )
                )

        except Exception as e:
            app.logger.error(f"analyze error: {repr(e)}")
            with ApiClient(config) as api_client:
                messaging_api = MessagingApi(api_client)
                from linebot.v3.messaging import PushMessageRequest
                messaging_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text="分析でエラーが発生しました。時間をおいて再度お試しください。")]
                    )
                )
        finally:
            # 後片付け＆状態クリア
            try:
                if os.path.exists(tmp_front):
                    os.remove(tmp_front)
                if os.path.exists(tmp_side):
                    os.remove(tmp_side)
            except Exception:
                pass
            state[user_id] = {"front": None, "side": None}


if __name__ == "__main__":
    # Render の PORT を尊重（なければ 10000）
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
