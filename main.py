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

# 這是你的密鑰與頻道，請妥善保管
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

# 紀錄已發送訊號與清理時間
sent_signals = {}
last_cleanup_day = datetime.now(tz).day

# ================== Telegram (增加重試機制) ==================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(3):  # 最多重試 3 次，確保通知不漏接
        try:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
            if r.status_code == 200:
                return
        except Exception as e:
            print(f"TG 發送失敗 (第{i+1}次):", e)
            time.sleep(2)

# ================== 安全時間轉換 ==================
def safe_ts(x):
    try:
        x = int(float(x))
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
def fetch_klines(symbol, bar="30m", limit=120):
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

# ================== 技術指標與形態 ==================
def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

def bull_engulf(prev, curr):
    return (curr["c"] > curr["o"] and prev["c"] < prev["o"] and 
            curr["c"] >= prev["o"] and curr["o"] <= prev["c"])

def bear_engulf(prev, curr):
    return (curr["c"] < curr["o"] and prev["c"] > prev["o"] and 
            curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

# ================== 核心策略邏輯 ==================
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = add_ema(df)
    prev = df.iloc[-3]
    curr = df.iloc[-2]

    # 強制 30 分鐘對齊
    if curr.name.minute not in (0, 30):
        return

    # EMA 多空排列
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]

    # 第一次回踩 EMA30 且未碰 EMA55
    bull_pullback = (curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"] and prev["l"] > prev["EMA30"])
    bear_pullback = (curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"] and prev["h"] < prev["EMA30"])

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
    tp1 = entry + (risk if long_signal else -risk)
    tp2 = entry + (risk * 1.5 if long_signal else -risk * 1.5)

    side = "🔴 空單" if short_signal else "🟢 多單"
    msg = (
        f"📊 {symbol} {side}\n"
        f"時間: {curr.name.strftime('%Y-%m-%d %H:%M')}\n"
        f"進場參考: {entry:.4f}\n"
        f"止損 EMA55: {sl:.4f}\n"
        f"盈虧比 1:1 : {tp1:.4f}\n"
        f"盈虧比 1:1.5 : {tp2:.4f}"
    )
    send_telegram_message(msg)

# ================== 掃描 (增加清理與溫控邏輯) ==================
def scan_all():
    global last_cleanup_day, sent_signals
    
    # 每日清理過期訊號，避免內存占用
    now = datetime.now(tz)
    if now.day != last_cleanup_day:
        sent_signals = {}
        last_cleanup_day = now.day
        print(f"[{now}] 系統已清理緩存紀錄")

    symbols = fetch_symbols()
    for s in symbols:
        try:
            time.sleep(0.1) # 增加小延遲防止 API 限流
            check_signal(s)
        except Exception as e:
            print(f"掃描錯誤 {s}: {e}")

# ================== 系統監控 ==================
def ping_system():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    send_telegram_message(f"✅ 系統在線監控中\n目前時間: {now}")

# ================== 排程設定 ==================
scheduler = BackgroundScheduler(timezone=tz)
# 設在 2 分與 32 分掃描，確保 K 線已收盤並產生
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(ping_system, "interval", minutes=120) # 每 2 小時報平安
scheduler.start()

# ================== Flask 入口 ==================
@app.route("/")
def home():
    return f"OKX EMA 策略運作中 - 最後更新時間: {datetime.now(tz)}"

if __name__ == "__main__":
    # 初次啟動先發送一次在線通知
    ping_system()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
