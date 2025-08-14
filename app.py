# app.py（LINE Bot 側）

import os
import io
import json
import logging
from typing import Tuple, Optional

import numpy as np
import cv2
import requests
from flask import Flask, request, abort, jsonify

from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    MessagingApi, MessagingApiBlob, Configuration, ApiClient,
)
from linebot.v3.webhooks import (
    ImageMessageContent, MessageEvent, TextMessageContent,
)

# ---- 環境変数 ----
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
ANALYZER_URL   = os.getenv("ANALYZER_URL", "https://ai-body-check-analyzer.onrender.com")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ---- LINE API クライアント ----
line_config = Configuration(access_token=CHANNEL_TOKEN)
parser = WebhookParser(CHANNEL_SECRET)

def _gather_bytes_from_line_content(resp) -> bytes:
    """
    line-bot-sdk v3 の get_message_content() の戻り値差異を吸収して bytes にする。
    - resp には以下のいずれかが来る実装差がある:
      - resp.body: bytes
      - resp.iter_content(): generator of bytes
      - resp: file-like (read() を持つ)
    """
    # 1) body 属性に bytes があるパターン
    body = getattr(resp, "body", None)
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)

    # 2) iter_content を持つパターン
    if hasattr(resp, "iter_content"):
        return b"".join(chunk for chunk in resp.iter_content() if chunk)

    # 3) file-like を想定
    if hasattr(resp, "read"):
        return resp.read()

    # 4) それ以外は bytes() を試す
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)

    raise ValueError("Unknown content type from LINE SDK (cannot convert to bytes).")

def _download_line_image_as_cv2(message_id: str) -> np.ndarray:
    """
    LINEの message_id から画像を取得し、cv2画像（BGR）で返す。
    """
    with ApiClient(line_config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        # v3: get_message_content(message_id) でバイナリレスポンス
        resp = blob_api.get_message_content(message_id)

    data = _gather_bytes_from_line_content(resp)
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to decode image (cv2.imdecode returned None).")
    return img

def _encode_cv2_to_jpeg_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        raise ValueError("Failed to encode image to JPEG.")
    return buf.tobytes()

def _post_to_analyzer(front_img: Optional[np.ndarray], side_img: Optional[np.ndarray]) -> Tuple[int, dict]:
    """
    Analyzer へ multipart/form-data で送る。
    front / side はどちらかが None の場合もある（段階的アップロードを想定）。
    """
    files = {}
    if front_img is not None:
        files["front"] = ("front.jpg", _encode_cv2_to_jpeg_bytes(front_img), "image/jpeg")
    if side_img is not None:
        files["side"] = ("side.jpg", _encode_cv2_to_jpeg_bytes(side_img), "image/jpeg")

    if not files:
        return 400, {"error": "no files to analyze"}

    url = f"{ANALYZER_URL}/analyze"
    try:
        r = requests.post(url, files=files, timeout=25)
        return r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else {"text": r.text})
    except requests.Timeout:
        return 504, {"error": "analyzer timeout"}
    except Exception as e:
        return 500, {"error": f"analyzer error: {e}"}

# ヘルスチェック
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def root():
    return "LINE Bot is running. Health: /healthz", 200

# LINE Webhook
@app.post("/callback")
def callback():
    signature = request.headers.get("x-line-signature")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception as e:
        app.logger.exception("Webhook parse error")
        abort(400)

    # セッション的に front/side を順に受けたい場合、超簡易に Text 指示で切替
    # 本格的にはユーザーIDごとに Redis などで状態管理してください
    # ここではデモとして "front" / "side" というテキストを受けた直後の1枚をその向き扱いにします
    # （簡易メモリ: プロセス内 dict）
    global _expected_pose
    if "_expected_pose" not in globals():
        _expected_pose = {}  # user_id -> "front"|"side"|None

    with ApiClient(line_config) as api_client:
        msg_api = MessagingApi(api_client)

        for ev in events:
            if not isinstance(ev, MessageEvent):
                continue

            user_id = ev.source.user_id if hasattr(ev.source, "user_id") else "unknown"

            # テキストでモード切替
            if isinstance(ev.message, TextMessageContent):
                text = (ev.message.text or "").strip().lower()
                if text in ("front","side"):
                    _expected_pose[user_id] = text
                    msg_api.reply_message(
                        reply_token=ev.reply_token,
                        messages=[{"type":"text","text":f"{text} の画像を送ってください"}]
                    )
                else:
                    msg_api.reply_message(
                        reply_token=ev.reply_token,
                        messages=[{"type":"text","text":"front か side と送ってから、該当の姿勢写真を送ってください。"}]
                    )
                continue

            # 画像受信
            if isinstance(ev.message, ImageMessageContent):
                pose = _expected_pose.get(user_id)
                if pose not in ("front","side"):
                    msg_api.reply_message(
                        reply_token=ev.reply_token,
                        messages=[{"type":"text","text":"まずは front または side と送ってください。"}]
                    )
                    continue

                try:
                    img = _download_line_image_as_cv2(ev.message.id)
                except Exception as e:
                    app.logger.exception("image download/decode failed")
                    msg_api.reply_message(
                        reply_token=ev.reply_token,
                        messages=[{"type":"text","text":(
                            "画像の取得に失敗しました。\n"
                            "・LINEアプリから“直接”画像を送ってください（共有URL不可）\n"
                            "・アルバム/クラウド共有経由は不可\n"
                            "・もう一度撮って送るか、別の画像でもお試しください"
                        )}]
                    )
                    continue

                # ユーザー毎に front/side を揃えてから Analyzer へ送る（メモリに保持）
                global _pending_imgs
                if "_pending_imgs" not in globals():
                    _pending_imgs = {}  # user_id -> {"front": np.ndarray | None, "side": np.ndarray | None}
                if user_id not in _pending_imgs:
                    _pending_imgs[user_id] = {"front": None, "side": None}

                _pending_imgs[user_id][pose] = img
                _expected_pose[user_id] = None  # 消す

                # 両方そろったら投げる
                if _pending_imgs[user_id]["front"] is not None and _pending_imgs[user_id]["side"] is not None:
                    status, result = _post_to_analyzer(
                        _pending_imgs[user_id]["front"], _pending_imgs[user_id]["side"]
                    )
                    # 使い終わったら捨てる
                    _pending_imgs[user_id] = {"front": None, "side": None}

                    if status == 200:
                        # 好みで整形
                        summary = json.dumps(result, ensure_ascii=False)
                        msg_api.reply_message(
                            reply_token=ev.reply_token,
                            messages=[{"type":"text","text":f"診断完了:\n{summary}"}]
                        )
                    elif status in (502, 503, 504):
                        msg_api.reply_message(
                            reply_token=ev.reply_token,
                            messages=[{"type":"text","text":"サーバが混み合っています。少し時間をおいて再度お試しください。"}]
                        )
                    else:
                        msg_api.reply_message(
                            reply_token=ev.reply_token,
                            messages=[{"type":"text","text":f"診断でエラーが発生しました: {result}"}]
                        )
                else:
                    # もう片方を促す
                    missing = "side" if pose == "front" else "front"
                    msg_api.reply_message(
                        reply_token=ev.reply_token,
                        messages=[{"type":"text","text":f"{pose} を受け取りました。次に {missing} の画像を送ってください。"}]
                    )

    return "ok", 200
