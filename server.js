// 必要なモジュール読み込み
const express = require("express");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
require("dotenv").config();

const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = process.env.LINE_ACCESS_TOKEN;

// 🔽 画像保存関数（拡張子付きで保存）
const downloadImage = async (messageId, accessToken) => {
  const url = `https://api-data.line.me/v2/bot/message/${messageId}/content`;

  const response = await axios.get(url, {
    responseType: "arraybuffer",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  const contentType = response.headers["content-type"]; // 例: image/jpeg
  const extension = contentType.split("/")[1]; // jpeg / png など
  const filename = `${messageId}.${extension}`;
  const savePath = path.join(__dirname, "images", filename);

  fs.writeFileSync(savePath, response.data);
  console.log("✅ 画像を保存しました →", savePath);

  return savePath;
};

// 🔽 Webhookエンドポイント
app.post("/webhook", async (req, res) => {
  console.log("📩 Webhook受信内容:", JSON.stringify(req.body, null, 2));
  const events = req.body.events;

  for (const event of events) {
    // 画像メッセージの場合のみ処理
    if (event.message && event.message.type === "image") {
      const replyToken = event.replyToken;
      const messageId = event.message.id;

      try {
        // imagesフォルダがなければ作成
        const imageDir = path.join(__dirname, "images");
        if (!fs.existsSync(imageDir)) {
          fs.mkdirSync(imageDir);
        }

        // 画像を保存
        const imagePath = await downloadImage(messageId, LINE_ACCESS_TOKEN);

        // analyze.py をPythonで実行
        execFile("python3", ["analyze.py", imagePath], async (err, stdout, stderr) => {
          if (err) {
            console.error("❌ Pythonエラー:", err);
            return;
          }

          try {
            const result = JSON.parse(stdout.toString());
            console.log("📊 診断結果:", result);

            // LINE返信文の作成
            const replyText = `📸 写真を受け取りました！診断結果はこちら👇\n\n` +
              `【姿勢】${result["姿勢"]}\n` +
              `【重心】${result["重心"]}\n` +
              `【印象】${result["印象"]}`;

            // LINEへ返信
            await axios.post(
              "https://api.line.me/v2/bot/message/reply",
              {
                replyToken,
                messages: [
                  {
                    type: "text",
                    text: replyText,
                  },
                ],
              },
              {
                headers: {
                  "Content-Type": "application/json",
                  Authorization: `Bearer ${LINE_ACCESS_TOKEN}`,
                },
              }
            );

            console.log("✅ LINE返信完了");
          } catch (parseError) {
            console.error("❌ JSON解析エラー:", parseError);
          }
        });
      } catch (err) {
        console.error("❌ 処理中のエラー:", err);
      }
    }
  }

  res.status(200).send("OK");
});

// 🔽 サーバー起動
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Server is running on port ${PORT}`);
});
