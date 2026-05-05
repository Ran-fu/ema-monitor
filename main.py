import requests
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import threading

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

monitoring_list = []
last_update_day = ""
sent_signals = {}

# ==================== 工具函數 ====================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try: 
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except: 
        pass

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
    except: return None

def calculate_indicators(df):
    # 僅保留 EMA 與 ATR 點位計算
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift(1)).abs(), (df['l']-df['c'].shift(1)).abs()], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean()
    
    return df

# ==================== 動態更新榜單 (Top 10) ====================
def update_monitoring_list():
    global monitoring_list, last_update_day
    try:
        url = "https://www.okx.com/api/v5/market/tickers"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        data = r.json().get("data", [])
        tickers = []
        for t in data:
            if t["instId"].endswith("-USDT-SWAP"):
                chg = (float(t["last"]) - float(t["open24h"])) / float(t["open24h"])
                tickers.append({"id": t["instId"], "chg": chg})
        
        sorted_t = sorted(tickers, key=lambda x: x["chg"], reverse=True)
        top_gainers = [x["id"] for x in sorted_t[:10]]
        top_losers = [x["id"] for x in sorted_t[-10:]]
        
        monitoring_list = list(dict.fromkeys(top_gainers + top_losers))
        last_update_day = datetime.now(tz).strftime("%Y-%m-%d")
        
        msg = f"🔥 監控榜單刷新 (Top 10)\n📈 漲幅榜: {', '.join([s.split('-')[0] for s in top_gainers])}\n📉 跌幅榜: {', '.join([s.split('-')[0] for s in top_losers])}"
        send_telegram_message(msg)
    except: pass

# ==================== 策略邏輯 (已去除 ADX, RSI) ====================
def check_signal(instId):
    # 1. 獲取 4H 數據並檢查 4H 爆量吞沒 (過濾大方向)
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is None or len(df4h) < 21: return
    
    curr4h, prev4h = df4h.iloc[-1], df4h.iloc[-2]
    vol_ma20_4h = df4h['vol'].rolling(20).mean().iloc[-1]
    
    # 4H 吞沒判斷
    f4h_long = (curr4h["c"] > curr4h["o"] and prev4h["c"] < prev4h["o"] and curr4h["c"] >= prev4h["o"]) and (curr4h["vol"] > vol_ma20_4h)
    f4h_short = (curr4h["c"] < curr4h["o"] and prev4h["c"] > prev4h["o"] and curr4h["c"] <= prev4h["o"]) and (curr4h["vol"] > vol_ma20_4h)
    
    if not (f4h_long or f4h_short): return

    # 2. 獲取 30M 數據並檢查 EMA 趨勢與 30M 信號
    df_raw = fetch_klines(instId, "30m")
    if df_raw is None or len(df_raw) < 70: return
    df = calculate_indicators(df_raw)
    
    curr, prev = df.iloc[-2], df.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    # EMA 趨勢過濾
    bull_trend = curr["E12"] > curr["E30"] > curr["E55"]
    bear_trend = curr["E12"] < curr["E30"] < curr["E55"]
    
    # 30M 吞沒與成交量放大
    bull_eg_30m = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"])
    bear_eg_30m = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["c"] <= prev["o"])
    vol_confirm = curr['vol'] > prev['vol']
    
    # 最終結合邏輯 (去除 ADX, RSI)
    long_signal = f4h_long and bull_trend and bull_eg_30m and vol_confirm
    short_signal = f4h_short and bear_trend and bear_eg_30m and vol_confirm

    if not (long_signal or short_signal): return

    # 防止重複
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 點位計算 (ATR 1.5倍)
    entry = curr["c"]
    atr_val = curr['atr'] * 1.5
    if long_signal:
        sl = min(curr["E55"], entry - atr_val)
        tp1, tp2 = entry + (entry - sl), entry + (entry - sl) * 1.5
        side = "🟢 多單共振 (Long)"
    else:
        sl = max(curr["E55"], entry + atr_val)
        tp1, tp2 = entry - (sl - entry), entry - (sl - entry) * 1.5
        side = "🔴 空單共振 (Short)"

    msg = (f"🎯 V6 Pro+ 價格行為訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場: {entry:.4f}\n"
           f"止損: {sl:.4f}\n"
           f"獲利1: {tp1:.4f}\n"
           f"獲利2: {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"📊 過濾狀態：\n"
           f"• 4H 爆量確認: ✅\n"
           f"• EMA 趨勢確認: ✅\n"
           f"• 30M 量能確認: ✅\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程與健康 Ping ====================
def scan_dynamic():
    if not monitoring_list or datetime.now(tz).strftime("%Y-%m-%d") != last_update_day:
        update_monitoring_list()
    for s in monitoring_list:
        check_signal(s)
        time.sleep(0.2)

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        scheduler.add_job(scan_dynamic, "cron", minute="2,32", id="v6_scan")
        scheduler.add_job(update_monitoring_list, "cron", hour=0, minute=1)
        # 每小時 Ping 確保不掉線
        scheduler.add_job(lambda: send_telegram_message("✅ V6 Pro+ (PA+EMA) 監控中..."), "interval", minutes=60)
        scheduler.start()
        update_monitoring_list()

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home(): return {"status": "running", "targets": len(monitoring_list)}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
