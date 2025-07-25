const express = require("express");
const axios = require("axios");
const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = "OPDo3K2o/qgIbbmefatWbzMOZhrA6i9a+HjqKeVKI0YmGCpoQzuwZKrTzWMziSEiEkP4GAoSuVyKW//dOTjVeAV0d8hEA1ZG+GRpdL4ixO1OY44RoovoxXC6F0D21ZgFQDXDCmOknnrTIrFK98Ba4gdB04t89/1O/w1cDnyilFU=";

app.post("/webhook", async (req, res) => {
  const event = req.body.events?.[0];
  if (!event) {
    return res.status(200).send("No event");
  }

  const replyToken = event.replyToken;

  // ✅ 画像（写真）のときだけ応答する
  if (event.message?.type === "image") {
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

      return res.status(200).send("Image reply sent");
    } catch (err) {
      console.error("Error replying to image:", err);
      return res.status(500).send("Error");
    }
  }

  // 💬 画像以外は応答しない（無視）
  return res.status(200).send("Non-image message ignored");
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
