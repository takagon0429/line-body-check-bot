const express = require("express");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
require("dotenv").config();

const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = process.env.LINE_ACCESS_TOKEN;

// 画像保存関数（拡張子付きで保存）
const downloadImage = async (messageId, accessToken) => {
  const url = `https://api-data.line.me/v2/bot/message/${messageId}/content`;

  const response = await axios.get(url, {
    responseType: "arraybuffer",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  const contentType = response.headers["content-type"]; // e.g. "image/jpeg"
  const extension = contentType.split("/")[1]; // jpeg / png
  const filename = `${messageId}.${extension}`;
  const savePath = path.join(__dirname, "images", filename);

  fs.writeFileSync(savePath, response.data);
  console.log("✅ 画像を保存しました →", savePath);

  return savePath;
};

// Webhookエンドポイント
app.post("/webhook", async (req, res) => {
  console.log("📩 Webhook受信内容:", JSON.stringify(req.body, null, 2)); // 👈 追加
  const events = req.body.events;

  for (const event of events) {
    if (event.message && event.message.type === "image") {
      const replyToken = event.replyToken;
      const messageId = event.message.id;

      try {
        // 画像を保存
        const imageDir = path.join(__dirname, "images");
        if (!fs.existsSync(imageDir)) {
          fs.mkdirSync(imageDir);
        }
        const imagePath = await downloadImage(messageId, LINE_ACCESS_TOKEN);

        // LINEに返信
        await axios.post(
          "https://api.line.me/v2/bot/message/reply",
          {
            replyToken,
            messages: [
              {
                type: "text",
                text: "📸 写真を受け取りました！診断を開始します🧠✨",
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

        // （次のステップ）ここで analyze.py など診断処理を呼び出す想定
        // const result = await runAnalyze(imagePath);
        // await replyResult(replyToken, result);

      } catch (err) {
        console.error("❌ エラー:", err);
      }
    }
  }

  res.status(200).send("OK");
});

// サーバー起動
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Server is running on port ${PORT}`);
});
