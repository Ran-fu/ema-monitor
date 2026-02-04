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

# ===== 防重複 =====
sent_signals = {}

# ===== Telegram =====
def send_telegram_message(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)

# ===== 幣種 =====
def fetch_symbols():
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            timeout=10
        )
        return [
            d["instId"].replace("-USDT-SWAP", "")
            for d in r.json().get("data", [])
            if d["instId"].endswith("-USDT-SWAP")
        ]
    except:
        return []

# ===== K 線（防 overflow）=====
def fetch_klines(symbol, interval="30m", limit=120):
    try:
        url = (
            "https://www.okx.com/api/v5/market/candles"
            f"?instId={symbol}-USDT-SWAP&bar={interval}&limit={limit}"
        )
        r = requests.get(url, timeout=10)
        data = r.json().get("data", [])
        if not data:
            return None

        df = pd.DataFrame(
            data,
            columns=["ts","o","h","l","c","vol","x1","x2","x3"]
        )
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)

        # ---- 關鍵修正：過濾異常時間 ----
        df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
        df = df[
            (df["ts"] > 946684800000) &     # 2000-01-01
            (df["ts"] < 4102444800000)      # 2100-01-01
        ]

        df["ts"] = pd.to_datetime(
            df["ts"], unit="ms", utc=True, errors="coerce"
        )
        df = df.dropna(subset=["ts"])
        df["ts"] = df["ts"].dt.tz_convert(tz)
        df.set_index("ts", inplace=True)

        return df.sort_index()
    except Exception as e:
        print(f"{symbol} K線錯誤:", e)
        return None

# ===== EMA =====
def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

# ===== 吞沒 =====
def bull_engulf(p, c):
    return (
        c["c"] > c["o"] and
        p["c"] < p["o"] and
        c["c"] >= p["o"] and
        c["o"] <= p["c"]
    )

def bear_engulf(p, c):
    return (
        c["c"] < c["o"] and
        p["c"] > p["o"] and
        c["o"] >= p["c"] and
        c["c"] <= p["o"]
    )

# ===== 核心策略（對齊 TV）=====
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = add_ema(df)

    # 只用已收盤 K
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]

    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    long_sig = bull_trend and bull_pullback and bull_engulf(prev, curr)
    short_sig = bear_trend and bear_pullback and bear_engulf(prev, curr)

    if not long_sig and not short_sig:
        return

    k_time = curr.name.floor("30T")
    key = f"{symbol}_{k_time}"
    if key in sent_signals:
        return
    sent_signals[key] = True

    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)

    tp1 = entry + risk if long_sig else entry - risk
    tp2 = entry + risk * 1.5 if long_sig else entry - risk * 1.5
    side = "多頭" if long_sig else "空頭"

    msg = (
        f"📊 {symbol} {side}訊號\n"
        f"K線時間: {k_time:%Y-%m-%d %H:%M}\n"
        f"進場: {entry:.4f}\n"
        f"止損 EMA55: {sl:.4f}\n"
        f"TP1 1:1: {tp1:.4f}\n"
        f"TP2 1:1.5: {tp2:.4f}"
    )
    send_telegram_message(msg)

# ===== Ping =====
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

# 啟動即 Ping
ping_system()

@app.route("/")
def home():
    return "OKX EMA 回踩吞沒策略運行中 ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
