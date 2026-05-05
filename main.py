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

# ==================== 配置 (已更新 Token 與 ID) ====================
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
    except Exception as e:
        print(f"Telegram Send Error: {e}")

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

# ==================== 核心指標計算 ====================
def calculate_adx(df, period=14):
    df = df.copy()
    df['up'] = df['h'] - df['h'].shift(1)
    df['down'] = df['l'].shift(1) - df['l']
    df['plus_dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
    df['minus_dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift(1)).abs(), (df['l']-df['c'].shift(1)).abs()], axis=1).max(axis=1)
    
    # 使用平滑移動平均計算精確 ADX
    tr_s = df['tr'].ewm(alpha=1/period, adjust=False).mean()
    p_dm_s = df['plus_dm'].ewm(alpha=1/period, adjust=False).mean()
    m_dm_s = df['minus_dm'].ewm(alpha=1/period, adjust=False).mean()
    
    df['p_di'] = 100 * (p_dm_s / tr_s)
    df['m_di'] = 100 * (m_dm_s / tr_s)
    df['dx'] = 100 * (df['p_di'] - df['m_di']).abs() / (df['p_di'] + df['m_di'])
    df['adx'] = df['dx'].rolling(window=period).mean()
    return df

# ==================== 動態更新榜單 (前10) ====================
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
        
        msg = (f"🔥 監控榜單已刷新 (Top 10)\n"
               f"📈 漲幅榜: {', '.join([s.split('-')[0] for s in top_gainers])}\n"
               f"📉 跌幅榜: {', '.join([s.split('-')[0] for s in top_losers])}")
        send_telegram_message(msg)
    except Exception as e:
        print(f"Update List Error: {e}")

# ==================== 策略判定邏輯 ====================
def check_signal(instId):
    # --- 1. 4H 大週期過濾 (必須爆量吞沒) ---
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is None or len(df4h) < 21: return
    
    curr4h = df4h.iloc[-1]
    prev4h = df4h.iloc[-2]
    vol_ma20_4h = df4h['vol'].rolling(20).mean().iloc[-1]
    
    bull_eg_4h = (curr4h["c"] > curr4h["o"] and prev4h["c"] < prev4h["o"] and curr4h["c"] >= prev4h["o"] and curr4h["o"] <= prev4h["c"])
    bear_eg_4h = (curr4h["c"] < curr4h["o"] and prev4h["c"] > prev4h["o"] and curr4h["o"] >= prev4h["c"] and curr4h["c"] <= prev4h["o"])
    vol_confirm_4h = curr4h['vol'] > vol_ma20_4h
    
    f4h_long = bull_eg_4h and vol_confirm_4h
    f4h_short = bear_eg_4h and vol_confirm_4h
    
    if not (f4h_long or f4h_short): return

    # --- 2. 30M 進場執行 ---
    df_raw = fetch_klines(instId, "30m")
    if df_raw is None or len(df_raw) < 70: return

    df = calculate_adx(df_raw)
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    df['atr'] = df['tr'].rolling(14).mean()
    
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))

    curr = df.iloc[-2]
    prev = df.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    trend_strong = curr['adx'] > 25
    bull_trend = curr["E12"] > curr["E30"] > curr["E55"]
    bear_trend = curr["E12"] < curr["E30"] < curr["E55"]
    
    bull_eg_30m = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_eg_30m = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    # 最終信號組合
    long_signal = f4h_long and bull_trend and bull_eg_30m and curr['vol'] > prev['vol'] and curr['rsi'] < 70 and trend_strong
    short_signal = f4h_short and bear_trend and bear_eg_30m and curr['vol'] > prev['vol'] and curr['rsi'] > 30 and trend_strong

    if not (long_signal or short_signal): return

    # 防止重複發送
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 點位計算 (ATR 1.5倍)
    entry = curr["c"]
    atr_val = curr['atr'] * 1.5
    if long_signal:
        sl = min(curr["E55"], entry - atr_val)
        tp1, tp2 = entry + (entry - sl), entry + (entry - sl) * 1.5
        side = "🟢 4H/30M 多單共振"
    else:
        sl = max(curr["E55"], entry + atr_val)
        tp1, tp2 = entry - (sl - entry), entry - (sl - entry) * 1.5
        side = "🔴 4H/30M 空單共振"

    msg = (f"🎯 V6 Pro+ 共振訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"📊 關鍵數據：\n"
           f"• 4H 爆量確認: ✅ (Vol > MA20)\n"
           f"• ADX 強度: {curr['adx']:.1f}\n"
           f"• RSI 指標: {curr['rsi']:.1f}\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程系統 ====================
def scan_dynamic():
    if not monitoring_list or datetime.now(tz).strftime("%Y-%m-%d") != last_update_day:
        update_monitoring_list()
    print(f"[{datetime.now(tz)}] 正在掃描 {len(monitoring_list)} 隻熱門幣種...")
    for s in monitoring_list:
        check_signal(s)
        time.sleep(0.2)

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        # 1. 每 30 分鐘掃描一次 (K線收盤後 2 分鐘執行)
        scheduler.add_job(scan_dynamic, "cron", minute="2,32", id="v6_scan")
        # 2. 每天凌晨刷新榜單
        scheduler.add_job(update_monitoring_list, "cron", hour=0, minute=1)
        # 3. 每小時 Ping 一次確保程序在線 (原本的 Ping 設定)
        scheduler.add_job(lambda: send_telegram_message("✅ V6 Pro+ 系統運行中 (4H/30M 共振模組)"), "interval", minutes=60)
        
        scheduler.start()
        # 啟動時先跑一次榜單更新
        update_monitoring_list()

# 使用 Thread 啟動防止阻塞 Flask
threading.Thread(target=init_scheduler, daemon=True).start()

# ==================== Flask 網頁服務 ====================
@app.route("/")
def home():
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "status": "Active",
        "last_check": now_str,
        "monitoring_count": len(monitoring_list),
        "target": [s.split('-')[0] for s in monitoring_list]
    }

if __name__ == "__main__":
    # 環境變數獲取 Port，適配雲端部署
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
