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

  // 🔒 写真だったら何も返信しないで終了
  if (event.message?.type === "image") {
    console.log("📷 画像受信 → 応答なしで処理終了");
    return res.status(200).send("Image received. No reply.");
  }

  // ✅ テキストなどには返信する（例）
  const replyToken = event.replyToken;
  const userMessage = event.message?.text || "（メッセージ不明）";

  try {
    await axios.post(
      "https://api.line.me/v2/bot/message/reply",
      {
        replyToken: replyToken,
        messages: [
          {
            type: "text",
            text: `あなたのメッセージ「${userMessage}」を受け取りました！`,
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

    res.status(200).send("Success");
  } catch (err) {
    console.error("Error replying:", err);
    res.status(500).send("Error");
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
