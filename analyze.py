# analyze.py

import mediapipe as mp
import cv2
import json
import sys

def calculate_score(val, ideal, tolerance):
    diff = abs(val - ideal)
    score = max(0, 10 - (diff / tolerance) * 10)
    return round(score, 1)

def analyze_cat_posture(image_path):
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(static_image_mode=True)

    image = cv2.imread(image_path)
    if image is None:
        return {"error": "画像の読み込みに失敗しました"}

    results = pose.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    if not results.pose_landmarks:
        return {"error": "姿勢が検出できませんでした。全身が写っている画像を使用してください。"}

    lm = {mp_pose.PoseLandmark[k].name: results.pose_landmarks.landmark[v.value]
          for k, v in mp_pose.PoseLandmark.__members__.items()}

    def y(name): return lm[name].y

    shoulder_y = (y("LEFT_SHOULDER") + y("RIGHT_SHOULDER")) / 2
    hip_y = (y("LEFT_HIP") + y("RIGHT_HIP")) / 2
    posture_ratio = abs(shoulder_y - hip_y)

    score = calculate_score(posture_ratio, 0.25, 0.1)

    result = {
        "姿勢（猫背傾向）スコア": score,
        "肩-腰の比率": round(posture_ratio, 3)
    }
    return result

if __name__ == "__main__":
    print("🚀 Python started", flush=True)
    path = sys.argv[1]
    res = analyze_cat_posture(path)
    print(json.dumps(res, ensure_ascii=False))
