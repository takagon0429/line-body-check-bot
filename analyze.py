import sys
import json
import mediapipe as mp
import cv2
import numpy as np

def analyze_posture(image_path):
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(static_image_mode=True)
    image = cv2.imread(image_path)

    if image is None:
        return {"error": "画像が読み込めませんでした"}

    # Mediapipeで処理
    results = pose.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

    if not results.pose_landmarks:
        return {"error": "姿勢ランドマークが検出できませんでした"}

    landmarks = results.pose_landmarks.landmark

    def get_coord(name):
        return landmarks[mp_pose.PoseLandmark[name].value]

    # ✨ 猫背判定：肩と耳の位置から判断（肩より耳が前に出ていたら猫背気味）
    shoulder = get_coord("LEFT_SHOULDER")
    ear = get_coord("LEFT_EAR")
    nekoze = abs(ear.x - shoulder.x) > 0.05  # 閾値は調整可
    nekoze_result = "やや猫背" if nekoze else "良好"

    # ✨ 肥満傾向（簡易版）：腰と肩の幅を比較（胴体が広めなら肥満傾向）
    l_shoulder = get_coord("LEFT_SHOULDER")
    r_shoulder = get_coord("RIGHT_SHOULDER")
    l_hip = get_coord("LEFT_HIP")
    r_hip = get_coord("RIGHT_HIP")

    shoulder_width = abs(r_shoulder.x - l_shoulder.x)
    hip_width = abs(r_hip.x - l_hip.x)
    hip_ratio = hip_width / shoulder_width if shoulder_width != 0 else 0
    fat_result = "やや肥満傾向" if hip_ratio > 1.05 else "標準"

    # ✨ 姿勢バランス：左右肩・腰の高さ差から傾きを推定
    shoulder_diff = abs(r_shoulder.y - l_shoulder.y)
    hip_diff = abs(r_hip.y - l_hip.y)
    balance_result = "左右バランスにやや差あり" if shoulder_diff > 0.03 or hip_diff > 0.03 else "良好"

    return {
        "姿勢": nekoze_result,
        "体型": fat_result,
        "バランス": balance_result,
    }

if __name__ == "__main__":
    image_path = sys.argv[1]
    result = analyze_posture(image_path)
    print(json.dumps(result, ensure_ascii=False))
