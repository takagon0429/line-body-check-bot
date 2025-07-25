const express = require("express");
const axios = require("axios");
const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = "OPDo3K2o/qgIbbmefatWbzMOZhrA6i9a+HjqKeVKI0YmGCpoQzuwZKrTzWMziSEiEkP4GAoSuVyKW//dOTjVeAV0d8hEA1ZG+GRpdL4ixO1OY44RoovoxXC6F0D21ZgFQDXDCmOknnrTIrFK98Ba4gdB04t89/1O/w1cDnyilFU=";

// Webhookエンドポイント
app.post("/webhook", async (req, res) => {
  const events = req.body.events;

  // 複数イベントに対応（LINEの仕様）
  for (const event of events) {
    const replyToken = event.replyToken;

    // ✅ 画像が送られた場合だけ返信
    if (event.message && event.message.type === "image") {
      try {
        await axios.post(
          "https://api.line.me/v2/bot/message/reply",
          {
            replyToken: replyToken,
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
      } catch (err) {
        console.error("画像メッセージの返信失敗:", err);
      }
    }

    // ❌ 画像以外（テキスト等）は無視して返信しない
  }

  res.status(200).send("OK");
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Server is running on port ${PORT}`);
});
