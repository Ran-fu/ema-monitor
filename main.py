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

# ==================== 核心計算：ADX 指標 ====================
def calculate_adx(df, period=14):
    df = df.copy()
    df['up'] = df['h'] - df['h'].shift(1)
    df['down'] = df['l'].shift(1) - df['l']
    
    df['plus_dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
    df['minus_dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
    
    df['tr'] = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift(1)).abs(), (df['l']-df['c'].shift(1)).abs()], axis=1).max(axis=1)
    
    tr_smooth = df['tr'].rolling(window=period).sum()
    plus_dm_smooth = df['plus_dm'].rolling(window=period).sum()
    minus_dm_smooth = df['minus_dm'].rolling(window=period).sum()
    
    df['plus_di'] = 100 * (plus_dm_smooth / tr_smooth)
    df['minus_di'] = 100 * (minus_dm_smooth / tr_smooth)
    df['dx'] = 100 * (df['plus_di'] - df['minus_di']).abs() / (df['plus_di'] + df['minus_di'])
    df['adx'] = df['dx'].rolling(window=period).mean()
    return df

# ==================== 策略核心 (V6 Pro+ 避震強化版) ====================
def check_signal(instId):
    df_raw = fetch_klines(instId, "30m", 150)
    if df_raw is None or len(df_raw) < 70: return

    # 1. 計算所有指標
    df = calculate_adx(df_raw)
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    # ATR 計算 (14週期的 TR 平均)
    df['atr'] = df['tr'].rolling(window=14).mean()
    
    # RSI 計算
    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))

    curr = df.iloc[-2] 
    prev = df.iloc[-3] 
    symbol = instId.replace("-USDT-SWAP", "")

    # 2. V6 Pro+ 過濾條件
    # ADX 強度門檻 (25)
    trend_strong = curr['adx'] > 25
    
    # 趨勢與回踩
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["l"] > curr["EMA55"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["h"] < curr["EMA55"]
    
    # 吞沒邏輯
    bull_engulf = (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    bear_engulf = (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])

    # 3. 綜合判定
    long_signal = bull_trend and bull_pullback and bull_engulf and curr['vol'] > prev['vol'] and curr['rsi'] < 70 and trend_strong
    short_signal = bear_trend and bear_pullback and bear_engulf and curr['vol'] > prev['vol'] and curr['rsi'] > 30 and trend_strong

    if not (long_signal or short_signal): return

    # 4. 4H 趨勢過濾
    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_signal and not (e12_4h > e55_4h): return
        if short_signal and not (e12_4h < e55_4h): return

    # 5. 防止重複
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    # 6. 動態點位 (ATR 1.5倍緩衝)
    entry = curr["c"]
    atr_buffer = curr['atr'] * 1.5
    if long_signal:
        sl = min(curr["EMA55"], entry - atr_buffer)
        tp1, tp2 = entry + (entry - sl), entry + (entry - sl) * 1.5
        side = "🟢 多單 (Long)"
    else:
        sl = max(curr["EMA55"], entry + atr_buffer)
        risk = sl - entry
        tp1, tp2 = entry - risk, entry - risk * 1.5
        side = "🔴 空單 (Short)"

    msg = (f"🎯 V6 Pro+ 訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"進場價: {entry:.4f}\n"
           f"止損 (SL): {sl:.4f}\n"
           f"獲利 (TP1): {tp1:.4f}\n"
           f"獲利 (TP2): {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"📊 避震數據：\n"
           f"• ADX強度: {curr['adx']:.1f} (✅ > 25)\n"
           f"• RSI指標: {curr['rsi']:.1f}\n"
           f"• 量能確認: 突破 ✅\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

# ==================== 排程與健康檢查 (維持 Ping 喚醒) ====================
def scan_all():
    print(f"[{datetime.now(tz)}] V6 Pro+ 深度掃描中...")
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
        # 每小時 Ping 訊息，確保 Render 不斷線
        scheduler.add_job(lambda: send_telegram_message("✅ V6 Pro+ 監控中 (已啟動 ADX 避震)"), 
                          "interval", minutes=60, id="ping_job", misfire_grace_time=300)
        scheduler.start()
        send_telegram_message("🚀 V6 Pro+ 機器人部署成功\n(ADX避震、RSI過濾、ATR動態風控已全面啟動)")

threading.Thread(target=init_scheduler, daemon=True).start()

@app.route("/")
def home():
    return f"V6 Pro+ Running. Server Time: {datetime.now(tz).strftime('%H:%M:%S')}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
