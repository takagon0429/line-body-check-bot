import sys
import json
import cv2
import mediapipe as mp

def analyze_posture(image_path):
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(static_image_mode=True)
    image = cv2.imread(image_path)

    if image is None:
        return {"error": "画像の読み込みに失敗しました。"}

    # 画像の色空間を変換（BGR → RGB）
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    result = pose.process(image_rgb)

    if not result.pose_landmarks:
        return {"error": "姿勢検出できませんでした。"}

    # 例：肩と腰の位置から「猫背」判定
    landmarks = result.pose_landmarks.landmark
    left_shoulder = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value]
    left_hip = landmarks[mp_pose.PoseLandmark.LEFT_HIP.value]

    slope = abs(left_shoulder.y - left_hip.y)

    posture = "良好" if slope < 0.1 else "やや猫背"

    return {
        "姿勢": posture,
        "検出ポイント": "肩と腰の位置差",
        "印象": "Mediapipeで解析済み"
    }

if __name__ == "__main__":
    image_path = sys.argv[1]
    result = analyze_posture(image_path)
    print(json.dumps(result, ensure_ascii=False))
