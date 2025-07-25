# analyze.py

import sys
import json

def dummy_analysis(image_path):
    # ★ここを後でMediapipeなどに差し替える
    result = {
        "姿勢": "やや猫背",
        "重心": "下半身に寄り気味",
        "印象": "清潔感あり。肩周りにやや丸み"
    }
    return result

if __name__ == "__main__":
    image_path = sys.argv[1]  # 画像パス
    result = dummy_analysis(image_path)
    print(json.dumps(result))  # JSON形式で出力
