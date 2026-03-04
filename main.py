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

# 儲存已發送訊號
sent_signals = {}

# ==================== 工具函數 ====================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except:
        pass

def fetch_bitunix_symbols():
    """取得 Bitunix 所有 USDT 永續合約"""
    try:
        url = "https://api.bitunix.com/api/v1/futures/market/tickers"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("code") == 0:
            # 篩選 USDT 結算的交易對
            return [i["symbol"] for i in data["data"] if i["symbol"].endswith("USDT")]
        return []
    except:
        return []

def fetch_klines(symbol, interval="30m", limit=100):
    """取得 Bitunix K線數據"""
    try:
        # Bitunix 參數: 1m, 5m, 15m, 30m, 1h, 4h, 1d
        url = "https://api.bitunix.com/api/v1/futures/market/candles"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        r = requests.get(url, params=params, timeout=10)
        res = r.json()
        if res.get("code") != 0 or not res.get("data"):
            return None
            
        # Bitunix 格式: [timestamp, open, high, low, close, volume]
        df = pd.DataFrame(res["data"], columns=["ts", "o", "h", "l", "c", "v"])
        
        # Bitunix 返回的是秒級或毫秒級，需根據實測調整，通常是毫秒
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
        
        return df.sort_values("ts").set_index("ts")
    except:
        return None

# ==================== 策略核心 (完全對齊 TV) ====================
def check_signal(symbol):
    # 1. 抓取 30m 數據
    df = fetch_klines(symbol, "30m")
    if df is None or len(df) < 60: return

    # EMA 計算
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()

    curr = df.iloc[-2] # 剛收盤 K
    prev = df.iloc[-3] # 前一根 K

    # 趨勢與回踩判斷
    bull_trend = curr["EMA12"] > curr["EMA30"] and curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] and curr["EMA30"] < curr["EMA55"]
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    # 吞沒邏輯
    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and 
                   curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and 
                   curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    long_signal = bull_trend and bull_pullback and bull_engulf
    short_signal = bear_trend and bear_pullback and bear_engulf

    if not (long_signal or short_signal): return

    # 2. 4H 趨勢過濾
    df4h = fetch_klines(symbol, "4h", 60)
    if df4h is not None:
        df4h["EMA12_4h"] = df4h["c"].ewm(span=12, adjust=False).mean()
        df4h["EMA55_4h"] = df4h["c"].ewm(span=55, adjust=False).mean()
        h4_last = df4h.iloc[-1]
        if long_signal and not (h4_last["EMA12_4h"] > h4_last["EMA55_4h"]): return
        if short_signal and not (h4_last["EMA12_4h"] < h4_last["EMA55_4h"]): return

    # 避免重複
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 3. 計算點位
    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)
    tp1 = entry + (risk * 1.0) if long_signal else entry - (risk * 1.0)
    tp2 = entry + (risk * 1.5) if long_signal else entry - (risk * 1.5)

    side = "🟢 多單 (Long)" if long_signal else "🔴 空單 (Short)"
    msg = (
        f"🎯 Bitunix 訊號: {symbol} {side}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔹 進場: {entry:.4f}\n"
        f"🔻 止損: {sl:.4f}\n"
        f"✅ TP1 (1:1): {tp1:.4f}\n"
        f"🚀 TP2 (1:1.5): {tp2:.4f}\n"
        f"━━━━━━━━━━━━━━\n"
        f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}\n"
        f"💡 已過濾 4H 趨勢"
    )
    send_telegram_message(msg)

# ==================== 排程與運行 ====================
def scan_all():
    symbols = fetch_bitunix_symbols()
    for s in symbols:
        try:
            check_signal(s)
            time.sleep(0.2) # Bitunix API 限流較嚴，稍微加長等待
        except:
            continue

scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, "cron", minute="2,32")
scheduler.add_job(lambda: send_telegram_message("✅ Bitunix 監控中"), "interval", minutes=60)
scheduler.start()

@app.route("/")
def home():
    return "Bitunix EMA Bot is Running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    send_telegram_message("🤖 Bitunix 機器人部署成功\n(對齊 TV v6 標籤 + 4H 過濾)")
    app.run(host="0.0.0.0", port=port)
