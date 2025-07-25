require("dotenv").config();
const express = require("express");
const line = require("@line/bot-sdk");
const axios = require("axios");
const fs = require("fs");
const path = require("path");
const { v4: uuidv4 } = require("uuid");

const app = express();

const config = {
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN,
  channelSecret: process.env.LINE_CHANNEL_SECRET,
};
const client = new line.Client(config);

// 画像保存ディレクトリ
const IMAGE_DIR = path.join(__dirname, "images");
if (!fs.existsSync(IMAGE_DIR)) fs.mkdirSync(IMAGE_DIR);

app.post("/webhook", line.middleware(config), async (req, res) => {
  Promise.all(req.body.events.map(handleEvent)).then(() => res.end());
});

async function handleEvent(event) {
  if (event.type !== "message" || event.message.type !== "image") {
    return client.replyMessage(event.replyToken, {
      type: "text",
      text: "画像を送信してください📷",
    });
  }

  // 画像を一時保存
  const messageId = event.message.id;
  const filename = `${uuidv4()}.jpg`;
  const filepath = path.join(IMAGE_DIR, filename);
  const stream = fs.createWriteStream(filepath);

  try {
    const streamData = await client.getMessageContent(messageId);
    streamData.pipe(stream);

    await new Promise((resolve, reject) => {
      stream.on("finish", resolve);
      stream.on("error", reject);
    });

    // ReplitのAPIに画像を送信
    const formData = new FormData();
    formData.append("image", fs.createReadStream(filepath));

    const response = await axios.post(
      "https://YOUR_REPLIT_URL.analyze.repl.co/analyze", // ← ReplitのURLに置き換え
      formData,
      { headers: formData.getHeaders() }
    );

    const result = response.data;

    const message = formatResult(result);

    return client.replyMessage(event.replyToken, {
      type: "text",
      text: message,
    });
  } catch (error) {
    console.error("❌ エラー:", error);
    return client.replyMessage(event.replyToken, {
      type: "text",
      text: "診断に失敗しました。画像が正しく読み込めませんでした。",
    });
  }
}

function formatResult(res) {
  return (
    `■ 姿勢：${res["姿勢スコア"]} / 10\n` +
    `■ ボディバランス：${res["ボディバランススコア"]} / 10\n` +
    `■ 筋肉・脂肪のつき方：${res["筋肉脂肪スコア"]} / 10\n` +
    `■ ファッション映え度：${res["ファッション映えスコア"]} / 10\n` +
    `■ 全体印象：${res["全体印象スコア"]} / 10\n\n` +
    `✨改善アドバイス：姿勢を整えれば印象UP！`
  );
}

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Server is running on port ${PORT}`);
});
