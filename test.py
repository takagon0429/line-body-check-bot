import requests

url = "http://localhost:8000/analyze"
files = {'image': open('test.jpg', 'rb')}  # test.jpg の名前はアップロード画像に合わせて

response = requests.post(url, files=files)

print("ステータスコード:", response.status_code)
print("診断結果:\n", response.text)
