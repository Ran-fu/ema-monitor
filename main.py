import requests
import pandas as pd
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
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

# 加入瀏覽器偽裝 Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
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
        # 加入 HEADERS 避開阻擋
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        
        # 檢查是否回傳正確的 JSON
        if r.status_code != 200:
            print(f"API 錯誤 {symbol}: {r.status_code}")
            return None
            
        res = r.json()
        if res.get("code") != 0 or not res.get("data"): return None
        
        df = pd.DataFrame(res["data"], columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except Exception as e:
        print(f"K線獲取失敗 {symbol}: {e}")
        return None

def check_signal(symbol):
    df = fetch_klines(symbol, "30m")
    if df is None or len(df) < 60: return
    
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    curr, prev = df.iloc[-2], df.iloc[-3]

    # TV v6 核心邏輯
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]
    bull_pb = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pb = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]
    bull_eg = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_eg = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    long_signal = bull_trend and bull_pb and bull_eg
    short_signal = bear_trend and bear_pb and bear_eg

    if not (long_signal or short_signal): return

    # 4H 趨勢過濾
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

def scan_all():
    now_str = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] 開始掃描 Bitunix...")
    try:
        # 獲取幣種列表也要加上 Headers
        url = "https://api.bitunix.com/api/v1/futures/market/tickers"
        r = requests.get(url, headers=HEADERS, timeout=15)
        
        if r.status_code != 200:
            print(f"無法取得幣種列表，HTTP {r.status_code}")
            return
            
        data = r.json()
        symbols = [i["symbol"] for i in data["data"] if i["symbol"].endswith("USDT")]
        
        for s in symbols:
            check_signal(s)
            time.sleep(0.5) # 稍微調慢一點，防止被鎖
            
        print(f"[{datetime.now(tz)}] 掃描完成。")
    except Exception as e:
        print(f"掃描主流程出錯: {e}")

# ==================== Flask & Scheduler (保持原有設定) ====================
@app.route("/")
def home():
    return "<h1>Bitunix Bot 運行中</h1><p>下次掃描: 02分 / 32分</p>"

@app.route("/test")
def test():
    send_telegram_message("🧪 執行手動測試...")
    scan_all()
    return "測試中，請看 TG 或 Log"

scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(lambda: send_telegram_message("✅ Bitunix 監控中"), "interval", minutes=60)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    send_telegram_message("🤖 Bitunix 機器人已修正連線問題並重啟")
    app.run(host="0.0.0.0", port=port)
