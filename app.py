import os
import hashlib
import random
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

def _seeded_float(seed_bytes, a, b):
    r = random.Random(seed_bytes)
    return a + (b - a) * r.random()

def _seeded_choice(seed_bytes, choices):
    r = random.Random(seed_bytes)
    return r.choice(choices)

@app.post("/analyze")
def analyze():
    front = request.files.get("front")
    side = request.files.get("side")

    if not front and not side:
        return jsonify({"status": "error", "message": "no files"}), 400

    front_bytes = front.read() if front else b""
    side_bytes  = side.read()  if side  else b""

    # 同じ画像→同じ結果、違う画像→違う結果 になるようにハッシュを種にする
    h  = hashlib.sha1(front_bytes + b"|" + side_bytes).digest()
    hf = hashlib.sha1(front_bytes).digest()
    hs = hashlib.sha1(side_bytes).digest()

    scores = {
        "balance":     round(_seeded_float(h,       5.0, 9.5), 1),
        "fashion":     round(_seeded_float(h[::-1], 5.0, 9.5), 1),
        "muscle_fat":  round(_seeded_float(hf,      5.0, 9.5), 1),
        "overall":     round(_seeded_float(hs,      5.0, 9.5), 1),
        "posture":     round(_seeded_float(h[::2],  4.0, 9.0), 1),
    }

    front_metrics = {
        "pelvis_tilt":    f"{round(_seeded_float(hf,       170.0, 190.0), 1)}°",
        "shoulder_angle": f"{round(_seeded_float(hf[::-1], 170.0, 190.0), 1)}°",
    }

    forward_cm = round(_seeded_float(hs, 0.0, 5.0), 1)
    kyphosis = _seeded_choice(hs[::-1], ["軽度", "中等度", "重度"])

    side_metrics = {
        "forward_head": f"{forward_cm}cm",
        "kyphosis": kyphosis,
    }

    adv_pool = [
        "肩の高さ差に注意。片側だけ荷物を持たない。",
        "胸椎伸展ストレッチと顎引きエクササイズを1日2回。",
        "股関節周りの可動域を意識して大股歩行を。",
        "デスクワーク時は30分ごとに立ち上がって伸びを。",
        "片脚立ちで体幹を刺激。1日左右各60秒×2セット。",
    ]
    r = random.Random(h)
    advice = r.sample(adv_pool, k=2)

    return jsonify({
        "status": "ok",
        "message": "files received",
        "front_filename": front.filename if front else None,
        "side_filename": side.filename if side else None,
        "scores": scores,
        "front_metrics": front_metrics,
        "side_metrics": side_metrics,
        "advice": advice,
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
