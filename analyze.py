import mediapipe as mp
import cv2
import json
import math

def calculate_score(val, ideal, tolerance):
    diff = abs(val - ideal)
    score = max(0, 10 - (diff / tolerance) * 10)
    return round(score, 1)

def analyze_posture(image_path):
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(static_image_mode=True)

    image = cv2.imread(image_path)
    if image is None:
        return {"error": "画像が読み込めません。ファイルパスをご確認ください。"}

    results = pose.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    if not results.pose_landmarks:
        return {"error": "姿勢が検出できませんでした。全身が写っている画像を使用してください。"}

    lm = results.pose_landmarks.landmark

    def get_y(index): return lm[index].y
    def get_x(index): return lm[index].x

    # 姿勢（猫背）スコア（肩と腰の縦距離）
    shoulder_y = (get_y(mp_pose.PoseLandmark.LEFT_SHOULDER) + get_y(mp_pose.PoseLandmark.RIGHT_SHOULDER)) / 2
    hip_y = (get_y(mp_pose.PoseLandmark.LEFT_HIP) + get_y(mp_pose.PoseLandmark.RIGHT_HIP)) / 2
    posture_ratio = abs(shoulder_y - hip_y)
    posture_score = calculate_score(posture_ratio, 0.25, 0.1)

    # ボディバランス（肩と膝の差）
    knee_y = (get_y(mp_pose.PoseLandmark.LEFT_KNEE) + get_y(mp_pose.PoseLandmark.RIGHT_KNEE)) / 2
    balance_ratio = abs(hip_y - knee_y)
    balance_score = calculate_score(balance_ratio, 0.25, 0.1)

    # 脂肪スコア（腰と膝の横距離＝お腹の膨らみと仮定）
    hip_x = (get_x(mp_pose.PoseLandmark.LEFT_HIP) + get_x(mp_pose.PoseLandmark.RIGHT_HIP)) / 2
    knee_x = (get_x(mp_pose.PoseLandmark.LEFT_KNEE) + get_x(mp_pose.PoseLandmark.RIGHT_KNEE)) / 2
    waist_ratio = abs(hip_x - knee_x)
    fat_score = calculate_score(waist_ratio, 0.10, 0.05)

    # ファッション映え度（肩と顔のバランス）
    eye_y = (get_y(mp_pose.PoseLandmark.LEFT_EYE) + get_y(mp_pose.PoseLandmark.RIGHT_EYE)) / 2
    neck_length = abs(shoulder_y - eye_y)
    fashion_score = calculate_score(neck_length, 0.15, 0.05)

    # 全体印象（平均点として仮定）
    total_average = (posture_score + balance_score + fat_score + fashion_score) / 4
    impression_score = round(min(10, total_average + 1), 1)

    result = {
        "姿勢（猫背傾向）スコア": posture_score,
        "アドバイス_姿勢": "肩が少し内側に入り気味（巻き肩）。肩甲骨ストレッチで改善可！",

        "ボディバランススコア": balance_score,
        "アドバイス_バランス": "お腹がやや目立ちやすい体型。重心は下寄り。",

        "筋肉・脂肪スコア": fat_score,
        "アドバイス_筋肉脂肪": "筋肉より皮下脂肪が優勢。特に腹部まわりに集中。",

        "ファッション映え度スコア": fashion_score,
        "アドバイス_ファッション": "顔まわりは好印象！首元がスッキリ見える服（Vネック、シャツ）がおすすめ。",

        "全体印象スコア": impression_score,
        "アドバイス_印象": "清潔感あり＆親しみやすい雰囲気。姿勢改善＆軽い運動でグッと印象アップ！",
    }

    return result

if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    res = analyze_posture(path)
    print(json.dumps(res, ensure_ascii=False, indent=2))
