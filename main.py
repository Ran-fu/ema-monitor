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

# ==================== 配置 (與 TV 一致) ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

sent_signals = {}

# ==================== 工具函數 ====================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if res.status_code != 200:
            print(f"TG API 錯誤: {res.text}")
    except Exception as e:
        print(f"TG 發送異常: {e}")

def fetch_klines(instId, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        r = requests.get(url, params={"instId": instId, "bar": bar, "limit": limit}, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        # OKX 返回 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        df = pd.DataFrame(data)
        df = df.iloc[:, :6]
        df.columns = ["ts", "o", "h", "l", "c", "vol"]
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

# ==================== 策略核心 ====================
def check_signal(instId):
    df = fetch_klines(instId, "30m", 100)
    if df is None or len(df) < 60: return

    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()

    curr = df.iloc[-2] 
    prev = df.iloc[-3] 
    symbol = instId.replace("-USDT-SWAP", "")

    bull_trend = curr["EMA12"] > curr["EMA30"] and curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] and curr["EMA30"] < curr["EMA55"]

    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and 
                   curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and 
                   curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    long_signal = bull_trend and bull_pullback and bull_engulf
    short_signal = bear_trend and bear_pullback and bear_engulf

    if not (long_signal or short_signal): return

    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        df4h["EMA12_4h"] = df4h["c"].ewm(span=12, adjust=False).mean()
        df4h["EMA55_4h"] = df4h["c"].ewm(span=55, adjust=False).mean()
        h4_last = df4h.iloc[-1]
        if long_signal and not (h4_last["EMA12_4h"] > h4_last["EMA55_4h"]): return
        if short_signal and not (h4_last["EMA12_4h"] < h4_last["EMA55_4h"]): return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)
    tp1 = entry + risk * 1.0 if long_signal else entry - risk * 1.0
    tp2 = entry + risk * 1.5 if long_signal else entry - risk * 1.5

    side = "🟢 多單 (Long)" if long_signal else "🔴 空單 (Short)"
    msg = (f"📊 策略訊號: {symbol} {side}\n進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n獲利 (TP1): {tp1:.4f}\n獲利 (TP2): {tp2:.4f}\n"
           f"時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程任務 ====================
def scan_all():
    print(f"[{datetime.now(tz)}] 開始全幣種掃描...")
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        symbols = [i["instId"] for i in r.json().get("data", []) if i["instId"].endswith("-USDT-SWAP")]
        for s in symbols:
            check_signal(s)
            time.sleep(0.05)
    except Exception as e:
        print(f"掃描出錯: {e}")

# --- 重點修正：確保排程器在全域啟動 ---
scheduler = BackgroundScheduler(timezone=tz)

def init_scheduler():
    if not scheduler.running:
        # 1. 策略掃描
        scheduler.add_job(scan_all, "cron", minute="2,32", id="scan_job")
        # 2. 每小時監測訊息 (改為 interval 確保不受 cron 邊界影響)
        scheduler.add_job(lambda: send_telegram_message("✅ EMA 策略監控中 (OKX/TV版)"), 
                          "interval", minutes=60, id="ping_job")
        scheduler.start()
        print("Scheduler Started.")
        # 部署成功立即通知
        send_telegram_message("🤖 EMA 吞沒策略機器人部署成功\n(OKX對接 + TV v6 邏輯 + 4H 過濾)")

# 呼叫啟動
init_scheduler()

# ==================== Flask 服務 ====================
@app.route("/")
def home():
    return f"EMA Bot v6 is Running. Last check: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"

if __name__ == "__main__":
    # 僅在本地執行 python app.py 時生效
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
