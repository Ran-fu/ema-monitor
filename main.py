from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os
import threading

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 (設定不變) ====================
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

def fetch_klines(instId, bar="30m", limit=150):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        r = requests.get(url, params={"instId": instId, "bar": bar, "limit": limit}, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        df = pd.DataFrame(data).iloc[:, :6]
        df.columns = ["ts", "o", "h", "l", "c", "vol"]
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c", "vol"]] = df[["o","h","l","c", "vol"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

# ==================== 策略核心 (加入 ATR + 量能 + RSI 過濾) ====================
def check_signal(instId):
    df = fetch_klines(instId, "30m", 150)
    if df is None or len(df) < 70: return

    # 1. 計算基礎指標 (EMA, ATR)
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()

    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift(1)).abs(), (df['l']-df['c'].shift(1)).abs()], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(window=14).mean()

    # 2. 計算 RSI (過濾高位震盪)
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    curr = df.iloc[-2] 
    prev = df.iloc[-3] 
    symbol = instId.replace("-USDT-SWAP", "")

    # 3. 基礎邏輯判定
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]
    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    # 4. 【新增過濾機制】
    # 量能過濾：吞沒陽線的成交量必須大於前一根陰線 (代表有資金支撐)
    vol_confirm = curr["vol"] > prev["vol"]
    
    # RSI 過度擴張過濾：RSI > 70 不做多，RSI < 30 不做空
    rsi_ok_long = curr["rsi"] < 70
    rsi_ok_short = curr["rsi"] > 30

    long_signal = bull_trend and bull_pullback and bull_engulf and vol_confirm and rsi_ok_long
    short_signal = bear_trend and bear_pullback and bear_engulf and vol_confirm and rsi_ok_short

    if not (long_signal or short_signal): return

    # 5. 4H 趨勢過濾
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_signal and not (e12_4h > e55_4h): return
        if short_signal and not (e12_4h < e55_4h): return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 6. 計算風控點位 (ATR 緩衝)
    entry = curr["c"]
    curr_atr = curr["atr"]
    atr_buffer = curr_atr * 1.5

    if long_signal:
        sl = min(curr["EMA55"], entry - atr_buffer)
        risk = entry - sl
        tp1, tp2 = entry + risk * 1.0, entry + risk * 1.5
        side = "🟢 多單 (Long)"
    else:
        sl = max(curr["EMA55"], entry + atr_buffer)
        risk = sl - entry
        tp1, tp2 = entry - risk * 1.0, entry - risk * 1.5
        side = "🔴 空單 (Short)"

    msg = (f"🎯 策略訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"📊 過濾指標：\n"
           f"• RSI: {curr['rsi']:.1f} (防超買賣)\n"
           f"• 量能: 突破確認 ✅\n"
           f"• 風控: ATR動態緩衝\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程與服務 (保持不變) ====================
def scan_all():
    print(f"[{datetime.now(tz)}] 啟動全幣種深度掃描...")
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        symbols = [i["instId"] for i in r.json().get("data", []) if i["instId"].endswith("-USDT-SWAP")]
        for s in symbols:
            check_signal(s)
            time.sleep(0.05)
    except Exception as e:
        print(f"掃描出錯: {e}")

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        scheduler.add_job(scan_all, "cron", minute="2,32", id="scan_job", misfire_grace_time=300)
        scheduler.add_job(lambda: send_telegram_message("✅ 策略監控中 (量價過濾 + ATR風控版)"), 
                          "interval", minutes=60, id="ping_job", misfire_grace_time=300)
        scheduler.start()
        send_telegram_message("🚀 專業版 EMA 策略機器人部署成功\n(已整合：量能過濾、RSI避熱、ATR動態風控)")

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home():
    return f"EMA Bot Pro-v1 Running. Time: {datetime.now(tz).strftime('%H:%M:%S')}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
