# 先頭付近の import 群にこれを追加
import hmac, hashlib, base64

# 既存の /callback をこの版に置き換え
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # --- 追加: 受信ログ（必ず出る）---
    print("[/callback] got request",
          "sig_len=", len(signature),
          "body_len=", len(body))

    # --- 追加: 自前で HMAC-SHA256 署名を検証（どこでズレるか確認用）---
    try:
        mac = hmac.new(CHANNEL_SECRET.encode("utf-8"),
                       body.encode("utf-8"),
                       hashlib.sha256).digest()
        computed = base64.b64encode(mac).decode("utf-8")
        # 先頭10文字だけ比較ログ（全量は出さない）
        print("[/callback] signature recv=", signature[:10], "...",
              "calc=", computed[:10], "...")
        if not hmac.compare_digest(signature, computed):
            print("[/callback ERROR] SignatureMismatch")
            # handler.handle に投げる前に 400 を返す
            abort(400, "Invalid signature (pre-check)")
    except Exception as e:
        print("[/callback ERROR] PreCheck", type(e).__name__, str(e))
        abort(400, f"precheck error: {e}")

    # --- ここから SDK 標準の検証（ハンドラ） ---
    try:
        handler.handle(body, signature)
    except Exception as e:
        print("[/callback ERROR] Handler", type(e).__name__, str(e))
        print("[/callback BODY]", body[:500])
        print("[/callback SIGNATURE]", signature[:30], "...")
        abort(400, str(e))
    return "OK"
