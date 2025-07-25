const functions = require("firebase-functions");
const axios = require("axios");

// LINEのチャネルアクセストークン（LINE Developersからコピーして貼ってね）
const LINE_ACCESS_TOKEN = "OPDo3K2o/qgIbbmefatWbzMOZhrA6i9a+HjqKeVKI0YmGCpoQzuwZKrTzWMziSEiEkP4GAoSuVyKW//dOTjVeAV0d8hEA1ZG+GRpdL4ixO1OY44RoovoxXC6F0D21ZgFQDXDCmOknnrTIrFK98Ba4gdB04t89/1O/w1cDnyilFU=";

// Webhookエンドポイント
exports.lineWebhook = functions.https.onRequest(async (req, res) => {
  const event = req.body.events?.[0];

  if (!event || event.type !== "message" || event.message.type !== "image") {
    return res.status(200).send("Not image");
  }

  const replyToken = event.replyToken;

  // 仮の返信（画像診断はまだ）
  try {
    await axios.post(
      "https://api.line.me/v2/bot/message/reply",
      {
        replyToken: replyToken,
        messages: [
          {
            type: "text",
            text: "📸 写真を受け取りました！体型診断を開始します🧠（※診断機能はこれから実装）",
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

    return res.status(200).send("OK");
  } catch (err) {
    console.error("LINEメッセージ送信失敗:", err);
    return res.status(500).send("Error");
  }
});
