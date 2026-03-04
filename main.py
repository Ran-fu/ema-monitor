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
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except:
        print(f"TG 發送失敗: {text[:20]}")

def fetch_klines(instId, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        # OKX API: 30m, 4H 分別對應其參數
        r = requests.get(url, params={"instId": instId, "bar": bar, "limit": limit}, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","vol","x1","x2","x3"])
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except Exception as e:
        return None

# ==================== 策略核心 (對齊 TV v6 + 4H 趨勢) ====================
def check_signal(instId):
    # 1. 抓取 30m 數據
    df = fetch_klines(instId, "30m", 100)
    if df is None or len(df) < 60: return

    # 計算 EMA (與 TV 的 ta.ema 一致)
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()

    curr = df.iloc[-2] # 剛收盤的那根 K 線
    prev = df.iloc[-3] # 前一根 K 線
    symbol = instId.replace("-USDT-SWAP", "")

    # 30m 趨勢與回踩邏輯 (TV v6 參數)
    bull_trend = curr["EMA12"] > curr["EMA30"] and curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] and curr["EMA30"] < curr["EMA55"]

    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]

    # 吞沒邏輯
    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and 
                   curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and 
                   curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    # 初步訊號判定
    long_signal = bull_trend and bull_pullback and bull_engulf
    short_signal = bear_trend and bear_pullback and bear_engulf

    if not (long_signal or short_signal): return

    # 2. 4H 趨勢過濾 (強化過濾器)
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        # 計算 4H 的 EMA12 與 EMA55
        df4h["EMA12_4h"] = df4h["c"].ewm(span=12, adjust=False).mean()
        df4h["EMA55_4h"] = df4h["c"].ewm(span=55, adjust=False).mean()
        h4_last = df4h.iloc[-1]
        
        if long_signal and not (h4_last["EMA12_4h"] > h4_last["EMA55_4h"]): return
        if short_signal and not (h4_last["EMA12_4h"] < h4_last["EMA55_4h"]): return

    # 3. 防止重複發送
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 4. 計算 TP/SL
    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)
    tp1 = entry + risk * 1.0 if long_signal else entry - risk * 1.0
    tp2 = entry + risk * 1.5 if long_signal else entry - risk * 1.5

    side = "🟢 多單 (Long)" if long_signal else "🔴 空單 (Short)"
    
    msg = (
        f"📊 策略訊號: {symbol} {side}\n"
        f"進場價: {entry:.4f}\n"
        f"--- 策略設定 (TV v6) ---\n"
        f"止損 (SL): {sl:.4f}\n"
        f"獲利 (TP1 1:1): {tp1:.4f}\n"
        f"獲利 (TP2 1:1.5): {tp2:.4f}\n"
        f"時間: {curr.name.strftime('%m/%d %H:%M')}\n"
        f"註: 已通過 4H 大趨勢過濾"
    )
    send_telegram_message(msg)

# ==================== 排程任務 ====================
def scan_all():
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        data = r.json().get("data", [])
        symbols = [i["instId"] for i in data if i["instId"].endswith("-USDT-SWAP")]
        for s in symbols:
            try:
                check_signal(s)
                time.sleep(0.1) # 頻率保護
            except:
                continue
    except Exception as e:
        print(f"掃描出錯: {e}")

scheduler = BackgroundScheduler(timezone=tz)
# 原設定：收盤後 2 分鐘執行
scheduler.add_job(scan_all, "cron", minute="2,32")
# 原設定：每 60 分鐘 Ping 一次
scheduler.add_job(lambda: send_telegram_message("✅ EMA 策略監控中 (OKX/TV版)"), "interval", minutes=60)
scheduler.start()

# ==================== Flask 服務 ====================
@app.route("/")
def home():
    return "EMA Bot v6 is Running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # 啟動通知
    send_telegram_message("🤖 EMA 吞沒策略機器人部署成功\n(OKX對接 + TV v6 邏輯 + 4H 過濾)")
    app.run(host="0.0.0.0", port=port)
