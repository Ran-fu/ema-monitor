from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 監控幣種 ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# === 發送 Telegram 訊息 ===
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"[Telegram] 發送失敗：{e}")

# === 取得 Bitunix K 線資料 (透過 Proxy) ===
def get_kline(symbol):
    try:
        proxy_url = f"https://api.allorigins.win/get?url=https://api.bitunix.io/api/v1/klines?symbol={symbol}&interval=30m&limit=100"
        resp = requests.get(proxy_url, timeout=15)
        if resp.status_code != 200:
            print(f"[{symbol}] Proxy 錯誤：{resp.status_code}")
            return None
        data = resp.json().get("contents")
        if not data:
            print(f"[{symbol}] Proxy 回傳內容異常")
            return None
        raw = eval(data)
        if "data" not in raw:
            print(f"[{symbol}] 無法解析 K 線資料")
            return None
        df = pd.DataFrame(raw["data"], columns=[
            "ts", "open", "high", "low", "close", "volume", "turnover", "confirm", "ignore"
        ])
        df = df.astype(float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"[{symbol}] K線抓取失敗：{e}")
        return None

# === 計算 EMA ===
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# === 檢查吞沒形態 ===
def check_engulf(df):
    engulf_type = None
    prev_open = df.iloc[-2]["open"]
    prev_close = df.iloc[-2]["close"]
    curr_open = df.iloc[-1]["open"]
    curr_close = df.iloc[-1]["close"]

    if curr_close > curr_open and prev_close < prev_open and curr_close >= prev_open and curr_open <= prev_close:
        engulf_type = "bullish"
    elif curr_close < curr_open and prev_close > prev_open and curr_close <= prev_open and curr_open >= prev_close:
        engulf_type = "bearish"
    return engulf_type

# === 主要策略 ===
def analyze_symbol(symbol):
    df = get_kline(symbol)
    if df is None or len(df) < 60:
        print(f"[{symbol}] 無法取得足夠資料")
        return None

    df["EMA12"] = ema(df["close"], 12)
    df["EMA30"] = ema(df["close"], 30)
    df["EMA55"] = ema(df["close"], 55)

    is_bull = df.iloc[-1]["EMA12"] > df.iloc[-1]["EMA30"] > df.iloc[-1]["EMA55"]
    is_bear = df.iloc[-1]["EMA12"] < df.iloc[-1]["EMA30"] < df.iloc[-1]["EMA55"]

    engulf = check_engulf(df)
    if engulf == "bullish" and is_bull:
        msg = f"📈 [{symbol}] 看漲吞沒（多頭排列）\n收盤價：{df.iloc[-1]['close']:.4f}"
        send_telegram_message(msg)
        print(msg)
    elif engulf == "bearish" and is_bear:
        msg = f"📉 [{symbol}] 看跌吞沒（空頭排列）\n收盤價：{df.iloc[-1]['close']:.4f}"
        send_telegram_message(msg)
        print(msg)

# === 今日成交量 Top3 ===
def get_top3_symbols():
    volumes = {}
    for symbol in SYMBOLS:
        df = get_kline(symbol)
        if df is not None and "volume" in df.columns:
            today = df[df["ts"].dt.date == datetime.utcnow().date()]
            if not today.empty:
                volumes[symbol] = today["volume"].sum()
    if not volumes:
        print("[系統] 無法取得成交量，使用預設清單")
        return SYMBOLS
    top3 = sorted(volumes, key=volumes.get, reverse=True)[:3]
    print(f"[系統] 今日成交量 Top3: {top3}")
    return top3

# === 任務排程 ===
def job():
    print(f"\n=== 開始檢查 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} ===")
    top3 = get_top3_symbols()
    for symbol in top3:
        analyze_symbol(symbol)

# === Flask 與排程啟動 ===
@app.route("/")
def home():
    return "EMA 吞沒監控系統運行中"

scheduler = BackgroundScheduler()
scheduler.add_job(job, "cron", minute="2,32")  # 每30分鐘檢查一次（收盤2分鐘後）
scheduler.start()

if __name__ == "__main__":
    send_telegram_message("✅ 系統已啟動（使用 Proxy 模式）")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
