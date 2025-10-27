from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import os

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = 1634751416

# === 監控幣種 ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# === EMA 週期 ===
EMA_SHORT = 12
EMA_MID = 30
EMA_LONG = 55

# === 訊號紀錄，避免重複發送 ===
sent_signals = {}

app = Flask(__name__)
scheduler = BackgroundScheduler()

# ---------------- Telegram 發送訊息 ----------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.get(url, params={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"Telegram 發送失敗: {e}")

# ---------------- 取得 BitUnix K 線資料 ----------------
def fetch_klines(symbol, interval="30m", limit=100):
    try:
        url = f"https://bitunix.com/api/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume","ignore1","ignore2","ignore3"])
        df["close"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"[{symbol}] K線抓取失敗: {e}")
        return None

# ---------------- 判斷 EMA 訊號 ----------------
def check_signal(df):
    df["EMA12"] = df["close"].ewm(span=EMA_SHORT, adjust=False).mean()
    df["EMA30"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=EMA_LONG, adjust=False).mean()
    signal = None

    # 最後一根 K 線
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭排列
    if last["EMA12"] > last["EMA30"] > last["EMA55"]:
        # 回踩 EMA30
        if last["low"] <= last["EMA30"] and last["low"] > last["EMA55"]:
            # 看漲吞沒
            if last["close"] > last["open"] and prev["close"] < prev["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
                signal = "多頭-看漲吞沒"
    # 空頭排列
    elif last["EMA12"] < last["EMA30"] < last["EMA55"]:
        # 回踩 EMA30
        if last["high"] >= last["EMA30"] and last["high"] < last["EMA55"]:
            # 看跌吞沒
            if last["close"] < last["open"] and prev["close"] > prev["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]:
                signal = "空頭-看跌吞沒"
    return signal, last["close"]

# ---------------- 定時任務 ----------------
def monitor():
    now = datetime.utcnow()
    for symbol in SYMBOLS:
        df = fetch_klines(symbol)
        if df is None or len(df) < EMA_LONG:
            continue
        df = df.iloc[-EMA_LONG-1:]  # 取足夠資料
        signal, price = check_signal(df)
        signal_key = f"{symbol}_{df['ts'].iloc[-1]}"
        if signal and signal_key not in sent_signals:
            message = f"{symbol} {signal} 收盤價: {price}"
            send_telegram(message)
            sent_signals[signal_key] = True

# ---------------- Flask 路由 ----------------
@app.route("/")
def home():
    return "EMA 監控服務運行中", 200

@app.route("/ping")
def ping():
    return "pong", 200

# ---------------- 啟動排程 ----------------
scheduler.add_job(monitor, "cron", minute="2,32")  # 每根 30 分鐘 K 收盤後 2 分鐘執行
scheduler.start()

# ---------------- Render Web Service ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
