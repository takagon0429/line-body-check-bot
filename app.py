import os
import io
import logging
from typing import Dict, Literal

from flask import Flask, request

# LINE v3 SDK（v3.18.1 で確認）
from linebot.v3.webhook import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    ApiException,  # 例外は messaging から
)
import requests

# ====== 基本設定 ======
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ANALYZER_URL = os.getenv("ANALYZER_URL")  # 例: https://ai-body-check-analyzer.onrender.com/analyze

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise ValueError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

if not ANALYZER_URL:
    app.logger.warning("ANALYZER_URL が未設定です。/analyze 連携は失敗します。")

parser = WebhookParser(CHANNEL_SECRET)
msg_api = MessagingApi(channel_access_token=CHANNEL_ACCESS_TOKEN)
blob_api = MessagingApiBlob(channel_access_token=CHANNEL_ACCESS_TOKEN)

# ユーザーごとの期待ショット(front/side)を保持（メモリ）
EXPECTING: Dict[str, Literal["front", "side"]] = {}

# ====== ヘルスチェック ======
@app.get("/")
def index():
    return "LINE Bot is running. Health: /healthz", 200

@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return "ok", 200

# ====== Webhook ======
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True) or ""

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        return "invalid signature", 400
    except Exception as e:
        app.logger.exception(f"parse error: {e}")
        return "parse error", 400

    for ev in events:
        etype = getattr(ev, "type", "")
        user_id = getattr(getattr(ev, "source", None), "user_id", None)
        reply_token = getattr(ev, "reply_token", None)

        # テキスト
        if etype == "message" and getattr(ev, "message", None) and getattr(ev.message, "type", "") == "text":
            text = (ev.message.text or "").strip().lower()
            if text in ("開始", "start"):
                EXPECTING[user_id] = "front"
                _reply(reply_token, "OK! まず「正面（front）」の写真を送ってください。")
            elif text in ("front", "正面"):
                EXPECTING[user_id] = "front"
                _reply(reply_token, "正面写真を送ってください。")
            elif text in ("side", "側面", "横"):
                EXPECTING[user_id] = "side"
                _reply(reply_token, "側面写真を送ってください。")
            else:
                need = EXPECTING.get(user_id)
                if need:
                    _reply(reply_token, f"「{need}」の写真を送ってください。front / side も指定できます。")
                else:
                    _reply(reply_token, "「開始」と送ると、解析フローを案内します。")
            continue

        # 画像
        if etype == "message" and getattr(ev, "message", None) and getattr(ev.message, "type", "") == "image":
            # 期待ショットを決定
            need = EXPECTING.get(user_id)
            if not need:
                need = "front"  # デフォルトで front から
                EXPECTING[user_id] = "front"

            try:
                # 画像バイトを取得（StreamingBody → bytes）
                stream = blob_api.get_message_content(message_id=ev.message.id)
                data = io.BytesIO()
                for chunk in stream.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        data.write(chunk)
                img_bytes = data.getvalue()

                if not img_bytes:
                    raise ValueError("empty image bytes")

                # Analyzer へ送信（bytes で渡す：('name', bytes, mime)）
                if not ANALYZER_URL:
                    _reply(reply_token, "サーバ設定不足：ANALYZER_URL が未設定です。")
                    continue

                files = {
                    need: (f"{need}.jpg", img_bytes, "image/jpeg"),
                }
                # もう片方が揃っている場合は state を見て後段で再送…などもできるが
                # まずは単発解析で返す
                res = requests.post(ANALYZER_URL, files=files, timeout=60)
                if res.status_code != 200:
                    raise RuntimeError(f"analyzer status {res.status_code}: {res.text[:300]}")

                j = {}
                try:
                    j = res.json()
                except Exception:
                    pass

                # 返却整形
                msg = _format_analyze_result(j) if j else "解析OK（レスポンスのJSON解釈に失敗しました）"
                _reply(reply_token, msg)

                # 次の期待ショットを更新
                EXPECTING[user_id] = "side" if need == "front" else "front"

            except requests.exceptions.ConnectTimeout:
                _reply(reply_token, "解析サーバへの接続がタイムアウトしました。しばらくして再試行してください。")
            except requests.exceptions.ReadTimeout:
                _reply(reply_token, "解析サーバの応答がタイムアウトしました。しばらくして再試行してください。")
            except ApiException as e:
                app.logger.exception(f"LINE Blob API error: {e}")
                _reply(reply_token, "画像の取得に失敗しました。もう一度、写真として直接送ってください。")
            except Exception as e:
                app.logger.exception(f"image handling error: {e}")
                _reply(
                    reply_token,
                    "画像の取得に失敗：処理中にエラーが発生しました。\n"
                    "・LINEから“写真として”直接送信（URL共有は不可）\n"
                    "・別の画像で再試行もお願いします"
                )
            continue

        # その他イベントは無視
        continue

    return "OK", 200


# ====== ユーティリティ ======
def _reply(reply_token: str, text: str) -> None:
    try:
        msg_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except ApiException as e:
        app.logger.exception(f"reply error: {e}")

def _format_analyze_result(j: dict) -> str:
    # Analyzer 側のサンプルJSONに合わせて整形
    # 例:
    # {
    #   "status":"ok","message":"files received",
    #   "front_metrics":{"shoulder_angle":"178.3°","pelvis_tilt":"179.9°"},
    #   "side_metrics":{"forward_head":"2.9cm","kyphosis":"軽度"},
    #   "scores":{"overall":7.3,"posture":6.0,...},
    #   "advice":[...]
    # }
    parts = []
    s = j.get("scores") or {}
    fm = j.get("front_metrics") or {}
    sm = j.get("side_metrics") or {}
    adv = j.get("advice") or []

    if s:
        parts.append(f"総合: {s.get('overall','-')} / 姿勢: {s.get('posture','-')}")
    if fm:
        parts.append(f"[正面] 肩角度: {fm.get('shoulder_angle','-')} 骨盤: {fm.get('pelvis_tilt','-')}")
    if sm:
        parts.append(f"[側面] ストレートネック: {sm.get('forward_head','-')} 胸椎: {sm.get('kyphosis','-')}")
    if adv:
        parts.append("アドバイス:\n- " + "\n- ".join(adv))

    return "\n".join(parts) if parts else "解析結果を受け取りました。"
