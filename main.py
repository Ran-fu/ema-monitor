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

# ==================== 配置 (請妥善保管 TOKEN) ====================
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

def fetch_klines(instId, bar="30m", limit=200):
    """獲取 K 線數據，增加 limit 以確保均線與指標計算穩定"""
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

def calculate_adx(df, period=14):
    df = df.copy()
    df['up'] = df['h'] - df['h'].shift(1)
    df['down'] = df['l'].shift(1) - df['l']
    df['plus_dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
    df['minus_dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift(1)).abs(), (df['l']-df['c'].shift(1)).abs()], axis=1).max(axis=1)
    tr_s = df['tr'].rolling(window=period).sum()
    p_dm_s = df['plus_dm'].rolling(window=period).sum()
    m_dm_s = df['minus_dm'].rolling(window=period).sum()
    df['p_di'] = 100 * (p_dm_s / (tr_s + 1e-9))
    df['m_di'] = 100 * (m_dm_s / (tr_s + 1e-9))
    df['dx'] = 100 * (df['p_di'] - df['m_di']).abs() / (df['p_di'] + df['m_di'] + 1e-9)
    df['adx'] = df['dx'].rolling(window=period).mean()
    return df

# ==================== 動態榜單更新 ====================
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
        top_gainers = [x["id"] for x in sorted_t[:5]]
        top_losers = [x["id"] for x in sorted_t[-5:]]
        
        monitoring_list = list(dict.fromkeys(top_gainers + top_losers))
        last_update_day = datetime.now(tz).strftime("%Y-%m-%d")
        
        msg = f"🔥 監控榜單刷新\n📈 漲幅: {', '.join([s.split('-')[0] for s in top_gainers])}\n📉 跌幅: {', '.join([s.split('-')[0] for s in top_losers])}"
        send_telegram_message(msg)
    except Exception as e:
        print(f"Update Error: {e}")

# ==================== 核心策略判定 ====================
def check_signal(instId):
    # --- 第一階段：4小時 (4H) 大週期過濾 ---
    df4h_raw = fetch_klines(instId, "4H", 100)
    if df4h_raw is None or len(df4h_raw) < 60: return

    df4h = df4h_raw.copy()
    df4h["E12"] = df4h["c"].ewm(span=12, adjust=False).mean()
    df4h["E30"] = df4h["c"].ewm(span=30, adjust=False).mean()
    df4h["E55"] = df4h["c"].ewm(span=55, adjust=False).mean()
    df4h["MA20_Vol"] = df4h["vol"].rolling(window=20).mean()

    curr4h = df4h.iloc[-1]  # 最近一根 4H
    prev4h = df4h.iloc[-2]

    # 4H 判定邏輯
    h4_bull_eg = (curr4h["c"] > curr4h["o"] and prev4h["c"] < prev4h["o"] and curr4h["c"] >= prev4h["o"] and curr4h["o"] <= prev4h["c"])
    h4_bear_eg = (curr4h["c"] < curr4h["o"] and prev4h["c"] > prev4h["o"] and curr4h["o"] >= prev4h["c"] and curr4h["c"] <= prev4h["o"])
    h4_bull_ema = curr4h["E12"] > curr4h["E30"] > curr4h["E55"]
    h4_bear_ema = curr4h["E12"] < curr4h["E30"] < curr4h["E55"]
    h4_vol_ok = curr4h["vol"] > curr4h["MA20_Vol"]

    h4_long_condition = h4_bull_eg and h4_bull_ema and h4_vol_ok
    h4_short_condition = h4_bear_eg and h4_bear_ema and h4_vol_ok

    if not (h4_long_condition or h4_short_condition): return

    # --- 第二階段：30分鐘 (30M) 進場判定 ---
    df30_raw = fetch_klines(instId, "30m", 150)
    if df30_raw is None or len(df30_raw) < 70: return

    df30 = calculate_adx(df30_raw)
    df30["E12"] = df30["c"].ewm(span=12, adjust=False).mean()
    df30["E30"] = df30["c"].ewm(span=30, adjust=False).mean()
    df30["E55"] = df30["c"].ewm(span=55, adjust=False).mean()
    df30['atr'] = df30['tr'].rolling(14).mean()
    
    curr30 = df30.iloc[-2] # 已結算的 K 線
    prev30 = df30.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    # 30M 判定邏輯
    m30_bull_eg = (curr30["c"] > curr30["o"] and prev30["c"] < prev30["o"] and curr30["c"] >= prev30["o"] and curr30["o"] <= prev30["c"])
    m30_bear_eg = (curr30["c"] < curr30["o"] and prev30["c"] > prev30["o"] and curr30["o"] >= prev30["c"] and curr30["c"] <= prev30["o"])
    m30_bull_ema = curr30["E12"] > curr30["E30"] > curr30["E55"]
    m30_bear_ema = curr30["E12"] < curr30["E30"] < curr30["E55"]

    # 最終結合 (4H 共振 + 30M 指標)
    long_signal = h4_long_condition and m30_bull_eg and m30_bull_ema and curr30['adx'] > 25
    short_signal = h4_short_condition and m30_bear_eg and m30_bear_ema and curr30['adx'] > 25

    if not (long_signal or short_signal): return

    # 防止重複發送
    key = f"{symbol}_{curr30.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 點位計算
    entry = curr30["c"]
    atr_val = curr30['atr'] * 1.5
    if long_signal:
        sl = min(curr30["E55"], entry - atr_val)
        tp1, tp2 = entry + (entry - sl), entry + (entry - sl) * 1.5
        side = "🟢 強力多單 (4H+30M共振)"
    else:
        sl = max(curr30["E55"], entry + atr_val)
        tp1, tp2 = entry - (sl - entry), entry - (sl - entry) * 1.5
        side = "🔴 強力空單 (4H+30M共振)"

    # --- Telegram 通知 ---
    msg = (f"🎯 V6 Pro+ 共振模組: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"【4H 趨勢背景】\n"
           f"• 形態: 爆量吞沒 ✅\n"
           f"• 均線: 多空排列 ✅\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"📊 ADX強度: {curr30['adx']:.1f}\n"
           f"⏰ 時間: {curr30.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程系統 ====================
def scan_dynamic():
    if not monitoring_list or datetime.now(tz).strftime("%Y-%m-%d") != last_update_day:
        update_monitoring_list()
    print(f"[{datetime.now(tz)}] 正在掃描共振信號...")
    for s in monitoring_list:
        check_signal(s)
        time.sleep(0.2) # 稍微延遲避免頻繁請求

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        # 每 30 分鐘的第 2 分鐘執行 (配合 30M 線收盤)
        scheduler.add_job(scan_dynamic, "cron", minute="2,32", id="v6_resonate_scan")
        scheduler.add_job(update_monitoring_list, "cron", hour=0, minute=1)
        scheduler.add_job(lambda: send_telegram_message("✅ V6 共振系統運行中..."), "interval", minutes=120)
        scheduler.start()
        update_monitoring_list()

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home(): 
    return {
        "status": "Active",
        "monitoring": [s.split('-')[0] for s in monitoring_list],
        "last_update": last_update_day
    }

if __name__ == "__main__":
    # 使用環境變數中的 Port，若無則預設 5000
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
