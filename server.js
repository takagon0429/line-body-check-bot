const express = require("express");
const axios = require("axios");
const app = express();
app.use(express.json());

const LINE_ACCESS_TOKEN = "OPDo3K2o/qgIbbmefatWbzMOZhrA6i9a+HjqKeVKI0YmGCpoQzuwZKrTzWMziSEiEkP4GAoSuVyKW//dOTjVeAV0d8hEA1ZG+GRpdL4ixO1OY44RoovoxXC6F0D21ZgFQDXDCmOknnrTIrFK98Ba4gdB04t89/1O/w1cDnyilFU=";

app.post("/webhook", async (req, res) => {
  const event = req.body.events?.[0];
  if (!event || event.message?.type !== "image") {
    return res.status(200).send("Not an image message");
  }

  const replyToken = event.replyToken;

  try {
    await axios.post(
      "https://api.line.me/v2/bot/message/reply",
      {
        replyToken: replyToken,
        messages: [
          {
            type: "text",
            text: "📸 写真を受け取りました！体型診断を開始します🧠（診断処理は後ほど実装）",
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
  console.log(`Server is running on port ${PORT}`);
});
