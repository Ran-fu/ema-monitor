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
    try: requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except: pass

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
    tr_s = df['tr'].rolling(window=period).sum()
    p_dm_s = df['plus_dm'].rolling(window=period).sum()
    m_dm_s = df['minus_dm'].rolling(window=period).sum()
    df['p_di'] = 100 * (p_dm_s / tr_s)
    df['m_di'] = 100 * (m_dm_s / tr_s)
    df['dx'] = 100 * (df['p_di'] - df['m_di']).abs() / (df['p_di'] + df['m_di'])
    df['adx'] = df['dx'].rolling(window=period).mean()
    return df

# ==================== 動態更新榜單 ====================
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
        
        msg = f"🔥 監控榜單已刷新\n📈 漲幅榜: {', '.join([s.split('-')[0] for s in top_gainers])}\n📉 跌幅榜: {', '.join([s.split('-')[0] for s in top_losers])}"
        send_telegram_message(msg)
    except Exception as e:
        print(f"Update Error: {e}")

# ==================== 策略判定 ====================
def check_signal(instId):
    df_raw = fetch_klines(instId, "30m")
    if df_raw is None or len(df_raw) < 70: return

    df = calculate_adx(df_raw)
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    df['atr'] = df['tr'].rolling(14).mean()
    
    # RSI
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))

    curr = df.iloc[-2]
    prev = df.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    # 1. 保留所有原設定：ADX > 25, 量能, RSI, 趨勢
    trend_strong = curr['adx'] > 25
    bull_trend = curr["E12"] > curr["E30"] > curr["E55"]
    bear_trend = curr["E12"] < curr["E30"] < curr["E55"]
    
    bull_pb = curr["l"] <= curr["E30"] and curr["l"] > curr["E55"]
    bear_pb = curr["h"] >= curr["E30"] and curr["h"] < curr["E55"]
    
    bull_eg = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_eg = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    long_signal = bull_trend and bull_pb and bull_eg and curr['vol'] > prev['vol'] and curr['rsi'] < 70 and trend_strong
    short_signal = bear_trend and bear_pb and bear_eg and curr['vol'] > prev['vol'] and curr['rsi'] > 30 and trend_strong

    if not (long_signal or short_signal): return

    # 2. 保留 4H 過濾
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_signal and not (e12_4h > e55_4h): return
        if short_signal and not (e12_4h < e55_4h): return

    # 3. 防止重複
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 4. 保留 ATR 1.5倍動態點位
    entry = curr["c"]
    atr_val = curr['atr'] * 1.5
    if long_signal:
        sl = min(curr["E55"], entry - atr_val)
        tp1, tp2 = entry + (entry - sl), entry + (entry - sl) * 1.5
        side = "🟢 多單 (Long)"
    else:
        sl = max(curr["E55"], entry + atr_val)
        tp1, tp2 = entry - (sl - entry), entry - (sl - entry) * 1.5
        side = "🔴 空單 (Short)"

    msg = (f"🎯 V6 Pro+ 動態榜單訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"📊 避震數據：\n"
           f"• ADX強度: {curr['adx']:.1f} (✅ > 25)\n"
           f"• RSI指標: {curr['rsi']:.1f}\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程與健康監控 ====================
def scan_dynamic():
    if not monitoring_list or datetime.now(tz).strftime("%Y-%m-%d") != last_update_day:
        update_monitoring_list()
    print(f"[{datetime.now(tz)}] 掃描中: {monitoring_list}")
    for s in monitoring_list:
        check_signal(s)
        time.sleep(0.1)

scheduler = BackgroundScheduler(timezone=tz)
def init_scheduler():
    if not scheduler.running:
        scheduler.add_job(scan_dynamic, "cron", minute="2,32", id="v6_scan")
        scheduler.add_job(update_monitoring_list, "cron", hour=0, minute=1)
        # 每小時 Ping 確保不掉線
        scheduler.add_job(lambda: send_telegram_message("✅ V6 Pro+ 雙向監控中..."), "interval", minutes=60)
        scheduler.start()
        update_monitoring_list()

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home(): return f"V6 Dynamic Active. Target: {monitoring_list}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
