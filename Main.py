from flask import Flask, render_template_string, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "你的 BOT TOKEN"
TELEGRAM_CHAT_ID = "你的 CHAT ID"

# === 已發送訊號紀錄 ===
sent_signals = set()

# === 發送訊息到 Telegram ===
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

# === 取得 OKX K 線資料 ===
def fetch_ohlcv(symbol="BTC-USDT", bar="30m", limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
    r = requests.get(url)
    data = r.json()["data"]
    df = pd.DataFrame(data, columns=[
        "ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"
    ])
    df = df.astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)
    return df

# === 策略邏輯（含容錯 + 等於 ema55 算觸碰） ===
def apply_strategy(df):
    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()
    df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()

    # 容錯 (0.01%)
    tolerance = df["close"] * 0.0001

    # 是否觸碰過 EMA55（包含等於）
    df["touched_ema55"] = (
        (df["low"] <= df["ema55"] + tolerance) &
        (df["high"] >= df["ema55"] - tolerance)
    )

    # === 吞沒型態 ===
    df["engulfing_bull"] = (
        (df["close"].shift(1) < df["open"].shift(1)) &
        (df["close"] > df["open"]) &
        (df["close"] > df["open"].shift(1)) &
        (df["open"] < df["close"].shift(1))
    )
    df["engulfing_bear"] = (
        (df["close"].shift(1) > df["open"].shift(1)) &
        (df["close"] < df["open"]) &
        (df["close"] < df["open"].shift(1)) &
        (df["open"] > df["close"].shift(1))
    )

    # === 多頭條件 ===
    df["bullish_signal"] = (
        (df["ema12"] > df["ema30"]) &
        (df["ema30"] > df["ema55"]) &
        (df["close"] > df["ema30"]) &
        (~df["touched_ema55"]) &
        (df["engulfing_bull"])
    )

    # === 空頭條件 ===
    df["bearish_signal"] = (
        (df["ema12"] < df["ema30"]) &
        (df["ema30"] < df["ema55"]) &
        (df["close"] < df["ema30"]) &
        (~df["touched_ema55"]) &
        (df["engulfing_bear"])
    )

    return df

# === 檢查訊號 ===
def check_signals():
    symbols = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    for symbol in symbols:
        df = fetch_ohlcv(symbol)
        df = apply_strategy(df)

        last = df.iloc[-1]
        ts_key = last["ts"].floor("30T")
        signal_key = f"{symbol}_{ts_key}"

        if last["bullish_signal"] and signal_key not in sent_signals:
            msg = f"📈 {symbol} 看漲吞沒\n收盤價: {last['close']:.2f}"
            send_telegram_message(msg)
            sent_signals.add(signal_key)

        if last["bearish_signal"] and signal_key not in sent_signals:
            msg = f"📉 {symbol} 看跌吞沒\n收盤價: {last['close']:.2f}"
            send_telegram_message(msg)
            sent_signals.add(signal_key)

# === APScheduler：每根 K 線收盤後 2 分鐘檢查 ===
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(check_signals, "cron", minute="2,32")
scheduler.start()

@app.route("/")
def index():
    return render_template_string("<h2>✅ EMA 策略監控執行中</h2>")

# === Render / Replit 健康檢查用 ===
@app.route("/ping")
def ping():
    return "pong"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
