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

# 儲存已發送訊號的字典
sent_signals = {}

# ==================== 工具函數 ====================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except:
        pass

def fetch_klines(instId, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        r = requests.get(url, params={"instId": instId, "bar": bar, "limit": limit}, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","vol","x1","x2","x3"])
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

# ==================== 策略核心 (完全對齊 TV) ====================
def check_signal(instId):
    # 抓取 30m 數據
    df = fetch_klines(instId, "30m")
    if df is None or len(df) < 60: return

    # 計算 EMA (與 TV 的 ta.ema 一致)
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()

    curr = df.iloc[-2] # 剛收盤 K
    prev = df.iloc[-3] # 前一根 K
    symbol = instId.replace("-USDT-SWAP", "")

    # 1. 趨勢判斷 (bullTrend / bearTrend)
    bull_trend = curr["EMA12"] > curr["EMA30"] and curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] and curr["EMA30"] < curr["EMA55"]

    # 2. 回踩判斷 (bullPullback / bearPullback)
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    # 3. 吞沒判斷 (與 TV v6 程式碼邏輯對齊)
    # bullEngulf: close > open and close[1] < open[1] and close >= open[1] and open <= close[1]
    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and 
                   curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    
    # bearEngulf: close < open and close[1] > open[1] and open >= close[1] and close <= open[1]
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and 
                   curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    # 4. 訊號成立
    long_signal = bull_trend and bull_pullback and bull_engulf
    short_signal = bear_trend and bear_pullback and bear_engulf

    if not (long_signal or short_signal): return

    # 5. 4H 趨勢過濾 (選配加強，確保跟 TV 策略在大趨勢同步)
    df4h = fetch_klines(instId, "4H", 20)
    if df4h is not None:
        df4h["E12"] = df4h["c"].ewm(span=12, adjust=False).mean()
        df4h["E55"] = df4h["c"].ewm(span=55, adjust=False).mean()
        h4 = df4h.iloc[-1]
        if long_signal and not (h4["E12"] > h4["E55"]): return
        if short_signal and not (h4["E12"] < h4["E55"]): return

    # 避免重複發送
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 6. 計算 TP/SL (對齊 TV 參數)
    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)
    
    tp1 = entry + risk * 1.0 if long_signal else entry - risk * 1.0
    tp2 = entry + risk * 1.5 if long_signal else entry - risk * 1.5

    side = "🟢 多單 (Long)" if long_signal else "🔴 空單 (Short)"
    
    msg = (
        f"📊 策略訊號: {symbol} {side}\n"
        f"價格: {entry:.4f}\n"
        f"--- 分批出場設定 ---\n"
        f"止損 (SL): {sl:.4f}\n"
        f"獲利 (TP1 1:1): {tp1:.4f} (平倉 50%)\n"
        f"獲利 (TP2 1:1.5): {tp2:.4f} (平倉 50%)\n"
        f"時間: {curr.name.strftime('%m/%d %H:%M')}"
    )
    send_telegram_message(msg)

# ==================== 排程與運行 ====================
def scan_all():
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        symbols = [i["instId"] for i in r.json().get("data", []) if i["instId"].endswith("-USDT-SWAP")]
        for s in symbols:
            check_signal(s)
            time.sleep(0.1)
    except:
        pass

scheduler = BackgroundScheduler(timezone=tz)
# 依照你的要求，保持原本的收盤後 2 分鐘掃描
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(lambda: send_telegram_message("✅ EMA 策略監控中"), "interval", minutes=60)
scheduler.start()

@app.route("/")
def home(): return "EMA Strategy Bot v6 is Running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    send_telegram_message("🤖 EMA 吞沒策略機器人部署成功\n(對齊 TV v6 版本)")
    app.run(host="0.0.0.0", port=port)
