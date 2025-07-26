from flask import Flask, request, jsonify
import cv2
import numpy as np
import mediapipe as mp
import tempfile

app = Flask(__name__)

# ---------------------------
# 姿勢スコア計算関数
# ---------------------------
def analyze_posture(image):
    mp_pose = mp.solutions.pose
    with mp_pose.Pose(static_image_mode=True) as pose:
        results = pose.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        if not results.pose_landmarks:
            return {"error": "姿勢の検出に失敗しました"}

        landmarks = results.pose_landmarks.landmark

        # 肩と腰のy座標の差を使った「猫背傾向」評価
        left_shoulder_y = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].y
        left_hip_y = landmarks[mp_pose.PoseLandmark.LEFT_HIP].y
        shoulder_to_hip_ratio = round(abs(left_shoulder_y - left_hip_y), 3)

        # 仮のスコア設定（今後拡張可能）
        posture_score = round(max(0, min(10, 10 - shoulder_to_hip_ratio * 25)), 1)
        balance_score = 6.0
        fat_score = 5.5
        fashion_score = 7.0
        impression_score = 7.0

        return {
            "姿勢スコア": posture_score,
            "ボディバランススコア": balance_score,
            "筋肉脂肪スコア": fat_score,
            "ファッション映えスコア": fashion_score,
            "全体印象スコア": impression_score,
            "肩-腰比率": shoulder_to_hip_ratio
        }

# ---------------------------
# Flask エンドポイント
# ---------------------------
@app.route("/analyze", methods=["POST"])
def analyze():
    if 'image' not in request.files:
        return jsonify({"error": "画像が送信されていません"}), 400

    file = request.files['image']
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        file.save(tmp.name)
        image = cv2.imread(tmp.name)

    if image is None:
        return jsonify({"error": "画像の読み込みに失敗しました"}), 400

    result = analyze_posture(image)
    return jsonify(result)

# ---------------------------
# 起動コマンド
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
