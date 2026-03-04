import os
import time
import requests
import pandas as pd
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from zoneinfo import ZoneInfo
import threading

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 (已驗證) ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"
sent_signals = {}

# 穩定連線頭 (防止被 Bitunix 阻擋)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

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
        r = requests.get(url, params=params, headers=HEADERS, timeout=12)
        if r.status_code != 200: return None
        res = r.json()
        if res.get("code") != 0: return None
        df = pd.DataFrame(res["data"], columns=["ts", "o", "h", "l", "c", "v"])
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

def check_signal(symbol):
    df = fetch_klines(symbol, "30m")
    if df is None or len(df) < 60: return
    
    # EMA 計算 (與 TV 同步)
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    curr, prev = df.iloc[-2], df.iloc[-3]
    
    # 策略條件
    long_cond = (curr["E12"] > curr["E30"] > curr["E55"]) and (curr["l"] <= curr["E30"] > curr["E55"]) and (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"])
    short_cond = (curr["E12"] < curr["E30"] < curr["E55"]) and (curr["h"] >= curr["E30"] < curr["E55"]) and (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"])
    
    if not (long_cond or short_cond): return

    # 4H 趨勢過濾
    df4h = fetch_klines(symbol, "4h", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_cond and not (e12_4h > e55_4h): return
        if short_cond and not (e12_4h < e55_4h): return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    entry, sl = curr["c"], curr["E55"]
    risk = abs(entry - sl)
    tp1, tp2 = (entry + risk, entry + risk*1.5) if long_cond else (entry - risk, entry - risk*1.5)
    
    msg = (f"🎯 Bitunix: {symbol} {'🟢多' if long_cond else '🔴空'}\n"
           f"進場: {entry:.4f} | 止損: {sl:.4f}\n"
           f"TP1: {tp1:.4f} | TP2: {tp2:.4f}")
    send_telegram_message(msg)

def scan_all():
    print(f"[{datetime.now(tz)}] 啟動全幣種掃描...")
    try:
        r = requests.get("https://api.bitunix.com/api/v1/futures/market/tickers", headers=HEADERS, timeout=15)
        symbols = [i["symbol"] for i in r.json()["data"] if i["symbol"].endswith("USDT")]
        for s in symbols:
            check_signal(s)
            time.sleep(0.4)
        print(f"掃描完成。")
    except Exception as e:
        print(f"掃描出錯: {e}")

# ==================== 服務啟動邏輯 ====================
@app.route("/")
def home():
    return "EMA Strategy Bot is Running", 200

@app.route("/test")
def manual_test():
    # 使用線程執行，避免網頁超時
    threading.Thread(target=scan_all).start()
    return "已觸發背景掃描測試，符合條件將會發送 TG 訊號。"

def start_scheduler():
    time.sleep(15) # 延遲啟動，給 Flask 握手時間
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(scan_all, "cron", minute="2,32")
    scheduler.add_job(lambda: send_telegram_message("✅ Bitunix 監控運作中"), "interval", minutes=60)
    scheduler.start()

if __name__ == "__main__":
    threading.Thread(target=start_scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
