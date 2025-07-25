const express = require("express");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
require("dotenv").config();

const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = process.env.LINE_ACCESS_TOKEN;

// 画像保存関数
const downloadImage = async (messageId, accessToken) => {
  const url = `https://api-data.line.me/v2/bot/message/${messageId}/content`;

  const response = await axios.get(url, {
    responseType: "arraybuffer",
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  const contentType = response.headers["content-type"];
  const extension = contentType.split("/")[1];
  const filename = `${messageId}.${extension}`;
  const savePath = path.join(__dirname, "images", filename);

  fs.writeFileSync(savePath, response.data);
  console.log("✅ 画像を保存しました →", savePath);

  return savePath;
};

// Webhook受信
app.post("/webhook", async (req, res) => {
  console.log("📩 Webhook受信内容:", JSON.stringify(req.body, null, 2));
  const events = req.body.events;

  for (const event of events) {
    if (event.message && event.message.type === "image") {
      const replyToken = event.replyToken;
      const messageId = event.message.id;

      try {
        const imageDir = path.join(__dirname, "images");
        if (!fs.existsSync(imageDir)) {
          fs.mkdirSync(imageDir);
        }

        const imagePath = await downloadImage(messageId, LINE_ACCESS_TOKEN);

        execFile("python3", ["analyze.py", imagePath], async (err, stdout, stderr) => {
          if (err) {
            console.error("❌ Pythonエラー:", err);
            if (stderr) {
              console.error("🐍 stderr:", stderr.toString());
            }
            return;
          }

          if (!stdout) {
            console.error("❌ Pythonの出力が空です");
            return;
          }

          try {
            const result = JSON.parse(stdout.toString());
            console.log("📊 診断結果:", result);

            // エラーメッセージが含まれていたらそのまま返信
            if (result.error) {
              await axios.post("https://api.line.me/v2/bot/message/reply", {
                replyToken,
                messages: [{ type: "text", text: `⚠️ ${result.error}` }],
              }, {
                headers: {
                  "Content-Type": "application/json",
                  Authorization: `Bearer ${LINE_ACCESS_TOKEN}`,
                },
              });
              return;
            }

            const replyText =
              `📸 写真を受け取りました！

■ 姿勢：${result["姿勢スコア"]} / 10
${result["姿勢コメント"]}

■ ボディバランス：${result["ボディバランススコア"]} / 10
${result["バランスコメント"]}

■ 筋肉・脂肪のつき方：${result["筋肉脂肪スコア"]} / 10
${result["脂肪コメント"]}

■ ファッション映え度：${result["ファッションスコア"]} / 10
${result["ファッションコメント"]}

■ 全体印象：${result["印象スコア"]} / 10
${result["印象コメント"]}`;

            await axios.post("https://api.line.me/v2/bot/message/reply", {
              replyToken,
              messages: [{ type: "text", text: replyText }],
            }, {
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${LINE_ACCESS_TOKEN}`,
              },
            });

            console.log("✅ LINE返信完了");
          } catch (parseErr) {
            console.error("❌ JSONパースエラー:", parseErr);
            console.error("📦 出力:", stdout.toString());
          }
        });
      } catch (err) {
        console.error("❌ 外部エラー:", err);
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
