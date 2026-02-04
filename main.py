from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

# ===== 狀態紀錄（防重複）=====
sent_signals = {}

# ===== Telegram 發送 =====
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram 發送失敗:", e)

# ===== 取得全 USDT 永續合約幣種 =====
def fetch_symbols():
    try:
        res = requests.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            timeout=10
        )
        data = res.json()
        symbols = []
        for d in data.get("data", []):
            instId = d["instId"]
            if instId.endswith("-USDT-SWAP"):
                symbols.append(instId.replace("-USDT-SWAP", ""))
        return symbols
    except:
        return []

# ===== 取得 K 線 =====
def fetch_klines(symbol, interval="30m", limit=100):
    try:
        url = (
            f"https://www.okx.com/api/v5/market/candles"
            f"?instId={symbol}-USDT-SWAP&bar={interval}&limit={limit}"
        )
        res = requests.get(url, timeout=10)
        data = res.json()
        if "data" not in data or not data["data"]:
            return None

        df = pd.DataFrame(
            data["data"],
            columns=["ts","o","h","l","c","vol","x1","x2","x3"]
        )
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)

        # === 關鍵：OKX UTC → Asia/Taipei ===
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df["ts"] = df["ts"].dt.tz_convert(tz)
        df.set_index("ts", inplace=True)

        return df.sort_index()
    except Exception as e:
        print(f"{symbol} K 線錯誤:", e)
        return None

# ===== EMA =====
def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

# ===== 吞沒判斷（跟 TV 一致）=====
def bullish_engulf(prev, curr):
    return (
        curr["c"] > curr["o"] and
        prev["c"] < prev["o"] and
        curr["c"] >= prev["o"] and
        curr["o"] <= prev["c"]
    )

def bearish_engulf(prev, curr):
    return (
        curr["c"] < curr["o"] and
        prev["c"] > prev["o"] and
        curr["o"] >= prev["c"] and
        curr["c"] <= prev["o"]
    )

# ===== 核心策略檢查 =====
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = add_ema(df)

    # === 只用已收盤 K 線（對齊 TV）===
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    # === 趨勢 ===
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]

    # === 回踩 EMA30（不碰 EMA55）===
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    # === 吞沒 ===
    bull_signal = bull_trend and bull_pullback and bullish_engulf(prev, curr)
    bear_signal = bear_trend and bear_pullback and bearish_engulf(prev, curr)

    if not bull_signal and not bear_signal:
        return

    # === 防同一根 K 重複 ===
    k_time = curr.name.floor("30T")
    key = f"{symbol}_{k_time}"
    if key in sent_signals:
        return
    sent_signals[key] = True

    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)

    tp1 = entry + risk if bull_signal else entry - risk
    tp2 = entry + risk * 1.5 if bull_signal else entry - risk * 1.5
    side = "多頭" if bull_signal else "空頭"

    msg = (
        f"📊 {symbol} {side}訊號\n"
        f"K線時間: {k_time.strftime('%Y-%m-%d %H:%M')}\n"
        f"進場: {entry:.4f}\n"
        f"止損 EMA55: {sl:.4f}\n"
        f"TP1 1:1: {tp1:.4f}\n"
        f"TP2 1:1.5: {tp2:.4f}"
    )
    send_telegram_message(msg)

# ===== 系統 Ping =====
def ping_system():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    count = len(fetch_symbols())
    send_telegram_message(
        f"✅ 系統在線中\n時間: {now}\n監控幣種數量: {count}"
    )

# ===== 排程 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(
    lambda: [check_signal(s) for s in fetch_symbols()],
    "cron",
    minute="0,30"
)
scheduler.add_job(ping_system, "interval", minutes=60)
scheduler.start()

# ===== 啟動即 Ping =====
ping_system()

@app.route("/")
def home():
    return "OKX EMA 回踩吞沒策略監控中 ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
