from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"
sent_signals = {}

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except:
        pass

def fetch_klines(symbol, interval="30m", limit=100):
    try:
        url = "https://api.bitunix.com/api/v1/futures/market/candles"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        res = r.json()
        if res.get("code") != 0 or not res.get("data"): return None
        df = pd.DataFrame(res["data"], columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

def check_signal(symbol):
    df = fetch_klines(symbol, "30m")
    if df is None or len(df) < 60: return
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    curr, prev = df.iloc[-2], df.iloc[-3]

    long_signal = (curr["EMA12"] > curr["EMA30"] > curr["EMA55"]) and (curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]) and (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    short_signal = (curr["EMA12"] < curr["EMA30"] < curr["EMA55"]) and (curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]) and (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    if not (long_signal or short_signal): return

    df4h = fetch_klines(symbol, "4h", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_signal and not (e12_4h > e55_4h): return
        if short_signal and not (e12_4h < e55_4h): return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    entry, sl = curr["c"], curr["EMA55"]
    risk = abs(entry - sl)
    tp1, tp2 = (entry + risk, entry + risk*1.5) if long_signal else (entry - risk, entry - risk*1.5)
    msg = f"🎯 Bitunix: {symbol} {'🟢多' if long_signal else '🔴空'}\n進場: {entry:.4f} | 止損: {sl:.4f}\nTP1: {tp1:.4f} | TP2: {tp2:.4f}"
    send_telegram_message(msg)

# ==================== 掃描主邏輯 ====================
def scan_all():
    now_str = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] 開始掃描 Bitunix...")
    try:
        r = requests.get("https://api.bitunix.com/api/v1/futures/market/tickers", timeout=10)
        symbols = [i["symbol"] for i in r.json()["data"] if i["symbol"].endswith("USDT")]
        for s in symbols:
            check_signal(s)
            time.sleep(0.3)
        print(f"[{datetime.now(tz)}] 掃描完成。共檢查 {len(symbols)} 個幣種。")
    except Exception as e:
        print(f"掃描出錯: {e}")

# ==================== Flask 路由 ====================
@app.route("/")
def home():
    return "<h1>Bitunix Bot 運行中</h1><p>下次掃描: 02分 / 32分</p>"

@app.route("/test")
def test():
    # 開啟手動測試路徑
    send_telegram_message("🧪 正在執行手動測試掃描...")
    scan_all()
    return "已觸發手動掃描，請查看 Telegram 或 Render 日誌。"

# ==================== 排程 ====================
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(lambda: send_telegram_message("✅ Bitunix 監控中"), "interval", minutes=60)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    send_telegram_message("🤖 Bitunix 機器人重啟（已加入手動測試功能）")
    app.run(host="0.0.0.0", port=port)
