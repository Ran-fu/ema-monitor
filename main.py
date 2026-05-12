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

def fetch_klines(instId, bar="30m", limit=200):
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
        top_gainers = [x["id"] for x in sorted_t[:10]]
        top_losers = [x["id"] for x in sorted_t[-10:]]
        
        monitoring_list = list(dict.fromkeys(top_gainers + top_losers))
        last_update_day = datetime.now(tz).strftime("%Y-%m-%d")
        
        msg = f"🔥 監控清單刷新 (回調模式)\n📈 領漲: {', '.join([s.split('-')[0] for s in top_gainers[:5]])}...\n📉 領跌: {', '.join([s.split('-')[0] for s in top_losers[:5]])}..."
        send_telegram_message(msg)
    except Exception as e:
        print(f"Update Error: {e}")

# ==================== 核心邏輯修改：回調模式 ====================
def check_signal(instId):
    # 1. 4H 大週期過濾
    df4h = fetch_klines(instId, "4H", 100)
    if df4h is None or len(df4h) < 60: return

    df4h["E12"] = df4h["c"].ewm(span=12, adjust=False).mean()
    df4h["E30"] = df4h["c"].ewm(span=30, adjust=False).mean()
    df4h["E55"] = df4h["c"].ewm(span=55, adjust=False).mean()
    curr4h = df4h.iloc[-1]

    h4_long_trend = curr4h["c"] > curr4h["E55"] and curr4h["E12"] > curr4h["E30"]
    h4_short_trend = curr4h["c"] < curr4h["E55"] and curr4h["E12"] < curr4h["E30"]

    if not (h4_long_trend or h4_short_trend): return

    # 2. 30M 進場判定
    df30 = fetch_klines(instId, "30m", 150)
    if df30 is None or len(df30) < 70: return

    df30 = calculate_adx(df30)
    df30["E12"] = df30["c"].ewm(span=12, adjust=False).mean()
    df30["E30"] = df30["c"].ewm(span=30, adjust=False).mean()
    df30["E55"] = df30["c"].ewm(span=55, adjust=False).mean()
    df30["MA20_Vol"] = df30["vol"].rolling(window=20).mean()
    
    curr30 = df30.iloc[-2] # 已收盤 K 線
    prev30 = df30.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    # --- 邏輯 A: 均線多空排列 ---
    m30_ema_ok = (curr30["E12"] > curr30["E30"] > curr30["E55"]) if h4_long_trend else (curr30["E12"] < curr30["E30"] < curr30["E55"])
    
    # --- 邏輯 B: 回踩 EMA 30 (當前最低/最高觸及 30 與 55 之間) ---
    pullback_ok = (curr30["l"] <= curr30["E30"] and curr30["l"] >= curr30["E55"]) if h4_long_trend else \
                  (curr30["h"] >= curr30["E30"] and curr30["h"] <= curr30["E55"])

    # --- 邏輯 C: 爆量吞沒 (成交量需 > MA20) ---
    m30_bull_eg = (curr30["c"] > curr30["o"] and prev30["c"] < prev30["o"] and curr30["c"] > prev30["o"])
    m30_bear_eg = (curr30["c"] < curr30["o"] and prev30["c"] > prev30["o"] and curr30["c"] < prev30["o"])
    m30_vol_ok = curr30["vol"] > curr30["MA20_Vol"]

    # 最終訊號
    long_signal = h4_long_trend and m30_ema_ok and pullback_ok and m30_bull_eg and m30_vol_ok and curr30['adx'] > 20
    short_signal = h4_short_trend and m30_ema_ok and pullback_ok and m30_bear_eg and m30_vol_ok and curr30['adx'] > 20

    if not (long_signal or short_signal): return

    key = f"{symbol}_{curr30.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # --- 邏輯 D: 止損點固定為 EMA 55 ---
    entry = curr30["c"]
    sl = curr30["E55"]
    
    if long_signal:
        risk = entry - sl
        tp1, tp2 = entry + risk, entry + risk * 1.5
        side = "🟢 回調做多 (EMA30 回踩)"
    else:
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - risk * 1.5
        side = "🔴 回調做空 (EMA30 回踩)"

    # Telegram 通知
    msg = (f"🎯 V6 Pullback: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"【訊號確認】\n"
           f"• 回踩 EMA30: ✅\n"
           f"• 爆量吞沒: ✅ (Vol > MA20)\n"
           f"• ADX 強度: {curr30['adx']:.1f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場點: {entry:.4f}\n"
           f"止損 (EMA55): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"⏰ 時間: {curr30.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程系統保持不變 ====================
def scan_dynamic():
    if not monitoring_list or datetime.now(tz).strftime("%Y-%m-%d") != last_update_day:
        update_monitoring_list()
    print(f"[{datetime.now(tz)}] 正在掃描 V6 Pullback 信號...")
    for s in monitoring_list:
        check_signal(s)
        time.sleep(0.3)

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        scheduler.add_job(scan_dynamic, "cron", minute="2,32", id="v6_pullback_scan")
        scheduler.add_job(update_monitoring_list, "cron", hour=0, minute=1)
        scheduler.add_job(lambda: send_telegram_message("💓 V6 回調系統運行中..."), "interval", minutes=120)
        scheduler.start()
        update_monitoring_list()

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home(): 
    return {
        "status": "Active",
        "strategy": "V6 Pullback (SL=EMA55)",
        "monitoring": [s.split('-')[0] for s in monitoring_list],
        "server_time": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
