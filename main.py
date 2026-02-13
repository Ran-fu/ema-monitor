from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os

# ================== 基本設定 ==================
app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

TELEGRAM_BOT_TOKEN = "你的TOKEN"
TELEGRAM_CHAT_ID = "1634751416"

sent_signals = {}

# ================== Telegram ==================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("TG 發送失敗:", e)

# ================== 安全時間轉換 ==================
def safe_ts(x):
    try:
        x = int(float(x))
        if x > 1e13:
            return pd.NaT
        return pd.to_datetime(x, unit="ms", utc=True).tz_convert(tz)
    except:
        return pd.NaT

# ================== 取得全 USDT 永續 ==================
def fetch_symbols():
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        data = r.json()
        return [
            i["instId"].replace("-USDT-SWAP", "")
            for i in data.get("data", [])
            if i["instId"].endswith("-USDT-SWAP")
        ]
    except Exception as e:
        print("fetch_symbols 錯誤:", e)
        return []

# ================== 取得 K 線 ==================
def fetch_klines(symbol, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {
            "instId": f"{symbol}-USDT-SWAP",
            "bar": bar,
            "limit": limit
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        if not data:
            return None

        df = pd.DataFrame(
            data,
            columns=["ts","o","h","l","c","vol","x1","x2","x3"]
        )

        df["ts"] = df["ts"].apply(safe_ts)
        df = df.dropna(subset=["ts"])
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)

        df = df.sort_values("ts")
        df.set_index("ts", inplace=True)

        return df
    except Exception as e:
        print(f"{symbol} K 線錯誤:", e)
        return None

# ================== EMA ==================
def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

# ================== 吞沒 ==================
def bull_engulf(prev, curr):
    return (
        curr["c"] > curr["o"] and
        prev["c"] < prev["o"] and
        curr["c"] >= prev["o"] and
        curr["o"] <= prev["c"]
    )

def bear_engulf(prev, curr):
    return (
        curr["c"] < curr["o"] and
        prev["c"] > prev["o"] and
        curr["o"] >= prev["c"] and
        curr["c"] <= prev["o"]
    )

# ================== 核心策略（已修正對齊 TV） ==================
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = add_ema(df)

    # 🔥 改成使用「已收盤K」
    prev = df.iloc[-3]
    curr = df.iloc[-2]

    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]

    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    long_signal = bull_trend and bull_pullback and bull_engulf(prev, curr)
    short_signal = bear_trend and bear_pullback and bear_engulf(prev, curr)

    if not long_signal and not short_signal:
        return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals:
        return
    sent_signals[key] = True

    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)

    tp1 = entry + risk if long_signal else entry - risk
    tp2 = entry + risk * 1.5 if long_signal else entry - risk * 1.5

    side = "多單" if long_signal else "空單"

    msg = (
        f"📊 {symbol} {side}\n"
        f"時間: {curr.name.strftime('%Y-%m-%d %H:%M')}\n"
        f"進場: {entry:.4f}\n"
        f"止損 EMA55: {sl:.4f}\n"
        f"TP1 1:1: {tp1:.4f}\n"
        f"TP2 1:1.5: {tp2:.4f}"
    )

    send_telegram_message(msg)

# ================== 掃描 ==================
def scan_all():
    symbols = fetch_symbols()
    for s in symbols:
        try:
            check_signal(s)
        except Exception as e:
            print("掃描錯誤:", s, e)

# ================== 自動 Ping ==================
def ping_system():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    count = len(fetch_symbols())
    send_telegram_message(f"✅ 系統在線中\n時間: {now}\n監控幣種: {count}")

# ================== Scheduler ==================
scheduler = BackgroundScheduler(timezone=tz)

# 🔥 改成收盤後 2 分鐘
scheduler.add_job(scan_all, "cron", minute="2,32")

scheduler.add_job(ping_system, "interval", minutes=60)

scheduler.start()

ping_system()

# ================== Flask ==================
@app.route("/")
def home():
    return "OKX EMA TV 對齊策略監控中 ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
