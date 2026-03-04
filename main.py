import os
import time
import requests
import pandas as pd
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"
BOOT_TIME = datetime.now() # 記錄啟動時間
sent_signals = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json"
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

def scan_all():
    # 安全鎖：啟動後 90 秒內不執行自動掃描，確保 Flask 優先穩定啟動
    if datetime.now() < BOOT_TIME + timedelta(seconds=90):
        print("系統啟動中，跳過本次排程掃描以確保 Port 綁定成功。")
        return

    print(f"[{datetime.now(tz)}] 開始 Bitunix 掃描...")
    try:
        url = "https://api.bitunix.com/api/v1/futures/market/tickers"
        r = requests.get(url, headers=HEADERS, timeout=15)
        symbols = [i["symbol"] for i in r.json()["data"] if i["symbol"].endswith("USDT")]
        
        for s in symbols:
            try:
                # 策略邏輯部分 (同前版)
                df = fetch_klines(s, "30m")
                if df is None or len(df) < 60: continue
                df["E12"], df["E30"], df["E55"] = df["c"].ewm(span=12).mean(), df["c"].ewm(span=30).mean(), df["c"].ewm(span=55).mean()
                curr, prev = df.iloc[-2], df.iloc[-3]
                
                # 訊號條件
                long = (curr["E12"] > curr["E30"] > curr["E55"]) and (curr["l"] <= curr["E30"] > curr["E55"]) and (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"])
                short = (curr["E12"] < curr["E30"] < curr["E55"]) and (curr["h"] >= curr["E30"] < curr["E55"]) and (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"])
                
                if long or short:
                    # 4H 過濾
                    df4h = fetch_klines(s, "4h", 60)
                    if df4h is not None:
                        e12_4h, e55_4h = df4h["c"].ewm(span=12).mean().iloc[-1], df4h["c"].ewm(span=55).mean().iloc[-1]
                        if long and not (e12_4h > e55_4h): continue
                        if short and not (e12_4h < e55_4h): continue
                    
                    key = f"{s}_{curr.name}"
                    if key not in sent_signals:
                        sent_signals[key] = True
                        side = "🟢多" if long else "🔴空"
                        send_telegram_message(f"🎯 Bitunix: {s} {side}\n進場: {curr['c']:.4f}\n止損: {curr['E55']:.4f}")
                
                time.sleep(0.4) # 稍微加快一點但保持防護
            except:
                continue
        print(f"[{datetime.now(tz)}] 掃描完成。")
    except Exception as e:
        print(f"主掃描出錯: {e}")

# ==================== Flask 網頁 ====================
@app.route("/")
def home():
    return "OK", 200

@app.route("/test")
def manual_test():
    scan_all()
    return "Manual scan triggered"

# ==================== 排程 ====================
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(lambda: send_telegram_message("✅ 系統運行中"), "interval", minutes=60)
scheduler.start()

if __name__ == "__main__":
    # 強制 Flask 優先啟動
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
