from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os

# ================== 基本設定 ==================
app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# 建議將 Token 放入環境變數，這裡暫留你的設定
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

sent_signals = {}

# ================== 工具函數 ==================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("TG 發送失敗:", e)

def safe_ts(x):
    try:
        return pd.to_datetime(int(float(x)), unit="ms", utc=True).tz_convert(tz)
    except:
        return pd.NaT

def fetch_symbols():
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        data = r.json()
        return [i["instId"].replace("-USDT-SWAP", "") for i in data.get("data", []) if i["instId"].endswith("-USDT-SWAP")]
    except:
        return []

def fetch_klines(symbol, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": f"{symbol}-USDT-SWAP", "bar": bar, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","vol","x1","x2","x3"])
        df["ts"] = df["ts"].apply(safe_ts)
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        df = df.sort_values("ts").set_index("ts")
        return df
    except:
        return None

def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

# ================== 核心邏輯 ==================
def check_signal(symbol):
    # 1. 取得 30m K 線並計算指標
    df_30m = fetch_klines(symbol, bar="30m")
    if df_30m is None or len(df_30m) < 60: return
    df_30m = add_ema(df_30m)

    curr = df_30m.iloc[-2]  # 剛收盤的 K 線
    prev = df_30m.iloc[-3]  # 前一根 K 線

    # 2. 基礎策略條件 (EMA 排列 + 回踩 + 吞沒)
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]

    # 修正回踩：最低價碰觸但收盤價站穩
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["c"] > curr["EMA30"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["c"] < curr["EMA30"]

    # 吞沒形態
    is_bull_engulf = curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"]
    is_bear_engulf = curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"]

    # 成交量過濾 (當前成交量 > 前一根)
    vol_confirm = curr["vol"] > prev["vol"]

    # 3. 大週期 (4H) 趨勢確認 (提高勝率關鍵)
    long_signal = bull_trend and bull_pullback and is_bull_engulf and vol_confirm
    short_signal = bear_trend and bear_pullback and is_bear_engulf and vol_confirm

    if not (long_signal or short_signal): return

    # 抓取 4H 數據檢查大方向
    df_4h = fetch_klines(symbol, bar="4H", limit=20)
    if df_4h is not None:
        df_4h = add_ema(df_4h)
        h4_curr = df_4h.iloc[-1]
        # 做多必須 4H EMA12 > EMA55; 做空必須 4H EMA12 < EMA55
        if long_signal and not (h4_curr["EMA12"] > h4_curr["EMA55"]): return
        if short_signal and not (h4_curr["EMA12"] < h4_curr["EMA55"]): return

    # 4. 發送信號
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    entry = curr["c"]
    sl = curr["EMA55"]
    risk = abs(entry - sl)
    tp1 = entry + risk if long_signal else entry - risk
    tp2 = entry + (risk * 1.5) if long_signal else entry - (risk * 1.5)

    msg = (
        f"🚀 高勝率信號: {symbol} {'多單' if long_signal else '空單'}\n"
        f"週期: 30m (已通過 4H 趨勢過濾)\n"
        f"進場: {entry:.4f}\n"
        f"止損: {sl:.4f}\n"
        f"TP1 (1:1): {tp1:.4f}\n"
        f"TP2 (1:1.5): {tp2:.4f}"
    )
    send_telegram_message(msg)

# ================== 排程設定 (維持原樣) ==================
def scan_all():
    symbols = fetch_symbols()
    for s in symbols:
        try:
            check_signal(s)
            time.sleep(0.1) # 增加小延遲防止 API 限流
        except:
            continue

def ping_system():
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    send_telegram_message(f"✅ 策略監控中\n時間: {now}")

scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, "cron", minute="2,32") # 收盤後 2 分鐘掃描
scheduler.add_job(ping_system, "interval", minutes=60)
scheduler.start()

# ================== Flask 運行 ==================
@app.route("/")
def home(): return "OKX EMA MTF Strategy Running ✅"

if __name__ == "__main__":
    ping_system()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
