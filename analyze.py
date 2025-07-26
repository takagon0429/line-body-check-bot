from flask import Flask, request, jsonify
import mediapipe as mp
import cv2
import json
import math
import numpy as np
from io import BytesIO
from PIL import Image

app = Flask(__name__)

def calculate_score(val, ideal, tolerance):
    diff = abs(val - ideal)
    score = max(0, 10 - (diff / tolerance) * 10)
    return round(score, 1)

def analyze_posture_image(image):
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(static_image_mode=True)

    results = pose.process(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not results.pose_landmarks:
        return {"error": "姿勢が検出できませんでした。全身が写っている画像を使用してください。"}

    lm = results.pose_landmarks.landmark
    get_y = lambda i: lm[i].y
    get_x = lambda i: lm[i].x

    shoulder_y = (get_y(mp_pose.PoseLandmark.LEFT_SHOULDER) + get_y(mp_pose.PoseLandmark.RIGHT_SHOULDER)) / 2
    hip_y = (get_y(mp_pose.PoseLandmark.LEFT_HIP) + get_y(mp_pose.PoseLandmark.RIGHT_HIP)) / 2
    knee_y = (get_y(mp_pose.PoseLandmark.LEFT_KNEE) + get_y(mp_pose.PoseLandmark.RIGHT_KNEE)) / 2

    posture_ratio = abs(shoulder_y - hip_y)
    posture_score = calculate_score(posture_ratio, 0.25, 0.1)

    balance_ratio = abs(hip_y - knee_y)
    balance_score = calculate_score(balance_ratio, 0.25, 0.1)

    hip_x = (get_x(mp_pose.PoseLandmark.LEFT_HIP) + get_x(mp_pose.PoseLandmark.RIGHT_HIP)) / 2
    knee_x = (get_x(mp_pose.PoseLandmark.LEFT_KNEE) + get_x(mp_pose.PoseLandmark.RIGHT_KNEE)) / 2
    waist_ratio = abs(hip_x - knee_x)
    fat_score = calculate_score(waist_ratio, 0.10, 0.05)

    eye_y = (get_y(mp_pose.PoseLandmark.LEFT_EYE) + get_y(mp_pose.PoseLandmark.RIGHT_EYE)) / 2
    neck_length = abs(shoulder_y - eye_y)
    fashion_score = calculate_score(neck_length, 0.15, 0.05)

    total_average = (posture_score + balance_score + fat_score + fashion_score) / 4
    impression_score = round(min(10, total_average + 1), 1)

    return {
        "姿勢スコア": posture_score,
        "ボディバランススコア": balance_score,
        "筋肉脂肪スコア": fat_score,
        "ファッション映えスコア": fashion_score,
        "全体印象スコア": impression_score
    }

@app.route("/analyze", methods=["POST"])
def analyze_api():
    if 'image' not in request.files:
        return jsonify({"error": "画像が含まれていません。"}), 400

    file = request.files['image']
    image = Image.open(BytesIO(file.read()))
    image_np = np.array(image)

    result = analyze_posture_image(image_np)
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
