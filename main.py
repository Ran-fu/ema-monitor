from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, os, json

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "你的Bot Token")
CHAT_ID = os.getenv("CHAT_ID", "1634751416")

# === Bitunix API ===
BITUNIX_KLINE_URL = "https://contract.mapi.bitunix.com/contract/api/v1/market/kline"
BITUNIX_TICKER_URL = "https://www.bitunix.com/v1/market/tickers"

# === 全域變數 ===
sent_signals = {}
last_ping = time.time()

# === 時區設定 ===
TZ = ZoneInfo("Asia/Taipei")


# ========== Telegram 發送 ==========
def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    except:
        pass


# ========== Bitunix 抓取 K 線 ==========
def fetch_kline(symbol, interval):
    try:
        resp = requests.get(
            BITUNIX_KLINE_URL,
            params={"symbol": symbol, "interval": interval, "limit": 200},
            timeout=10
        ).json()

        if resp.get("code") != 0:
            return None

        data = resp["data"]["list"]
        df = pd.DataFrame(data, columns=[
            "ts", "open", "high", "low", "close", "volume"
        ])

        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)

        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
        df = df.sort_values("datetime")

        return df

    except Exception as e:
        print("Kline Error:", e)
        return None


# ========== Bitunix Top3（漲跌幅） ==========
def get_top3():
    try:
        resp = requests.get(BITUNIX_TICKER_URL, timeout=10).json()

        if "data" not in resp:
            return [], []

        items = resp["data"]

        df = pd.DataFrame(items)
        df["last"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["chg"] = (df["last"] - df["open"]) / df["open"] * 100

        top_gainers = df.sort_values("chg", ascending=False).head(3)
        top_losers = df.sort_values("chg").head(3)

        gain_txt = ", ".join([f"{r['symbol']} {r['chg']:.2f}%" for _, r in top_gainers.iterrows()])
        loss_txt = ", ".join([f"{r['symbol']} {r['chg']:.2f}%" for _, r in top_losers.iterrows()])

        return gain_txt, loss_txt

    except Exception as e:
        print("Top3 Error:", e)
        return "", ""


# ========== 吞沒 + EMA 策略 ==========
def check_signal(symbol, interval):
    df = fetch_kline(symbol, interval)
    if df is None or len(df) < 60:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema12 = close.ewm(span=12).mean()
    ema30 = close.ewm(span=30).mean()
    ema55 = close.ewm(span=55).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭排列
    bull = ema12.iloc[-1] > ema30.iloc[-1] > ema55.iloc[-1]

    # 空頭排列
    bear = ema12.iloc[-1] < ema30.iloc[-1] < ema55.iloc[-1]

    # 回踩
    touched_30 = last.low <= ema30.iloc[-1]
    touched_55 = last.low <= ema55.iloc[-1]

    # 吞沒
    bullish_engulf = last.close > last.open and prev.close < prev.open and last.close > prev.open
    bearish_engulf = last.close < last.open and prev.close > prev.open and last.close < prev.open

    signal = None

    if bull and touched_30 and not touched_55 and bullish_engulf:
        signal = f"📈 多頭吞沒 {symbol} ({interval}) 收盤價 {last.close}"

    if bear and touched_30 and not touched_55 and bearish_engulf:
        signal = f"📉 空頭吞沒 {symbol} ({interval}) 收盤價 {last.close}"

    return signal


# ========== 主掃描 ==========
def scan_all():
    global sent_signals, last_ping
    now = datetime.now(TZ)

    # 固定三檔
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    # Top3
    gain_txt, loss_txt = get_top3()

    for symbol in symbols:
        for tf in ["15", "30"]:
            key = f"{symbol}-{tf}-{now.date()}"

            signal = check_signal(symbol, tf)
            if signal and key not in sent_signals:
                sent_signals[key] = True
                send_message(signal)

    # 每日清空 & 推送 Top3
    if now.hour == 0 and now.minute == 1:
        sent_signals = {}
        send_message(f"📊 Bitunix Top3\n漲幅: {gain_txt}\n跌幅: {loss_txt}")

    # 掉線偵測
    if time.time() - last_ping > 300:
        send_message("⚠️ 監控可能掉線！")
    last_ping = time.time()


# ========== 排程 ==========
scheduler = BackgroundScheduler()
scheduler.add_job(scan_all, "cron", minute="*/2")  # 每2分鐘掃描
scheduler.start()

# 啟動即發訊號
send_message("🚀 Bitunix EMA 永續監控已啟動！")

@app.route("/")
def home():
    return "Bitunix EMA Monitor Running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
