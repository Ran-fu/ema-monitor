from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# ===== OKX 設定 =====
OKX_BASE = "https://www.okx.com/api/v5/market"

# ===== 已發訊號記錄 =====
sent_signals = set()

# ===== Telegram 發訊函數 =====
def send_telegram(msg):
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(TELEGRAM_URL, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        print("Telegram 發訊錯誤:", e)
        return None

# ===== K 線抓取 =====
def get_klines(symbol, interval="30m", limit=200):
    symbol_api = symbol.replace("USDT","-USDT-SWAP")
    url = f"{OKX_BASE}/candles?instId={symbol_api}&bar={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "data" not in data:
            print(f"{symbol} Kline Error:", data)
            return None
        df = pd.DataFrame(data["data"], columns=[
            "timestamp","open","high","low","close","volume","unknown"
        ])
        df["close"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        print(f"{symbol} Kline抓取錯誤:", e)
        return None

# ===== EMA + 吞沒策略判斷 =====
def ema_strategy(df):
    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭排列 + 看漲吞沒
    if last["EMA12"] > last["EMA30"] > last["EMA55"]:
        if prev["low"] > last["EMA55"] and prev["close"] < prev["open"] \
           and last["close"] > last["open"] and last["close"] > prev["open"] \
           and last["open"] < prev["close"]:
            return "看漲吞沒"

    # 空頭排列 + 看跌吞沒
    elif last["EMA12"] < last["EMA30"] < last["EMA55"]:
        if prev["high"] < last["EMA55"] and prev["close"] > prev["open"] \
           and last["close"] < last["open"] and last["close"] < prev["open"] \
           and last["open"] > prev["close"]:
            return "看跌吞沒"

    return None

# ===== Top3 漲跌幅 =====
def get_top3():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        tickers = r.json().get("data", [])
        df = pd.DataFrame(tickers)
        df["changeRate"] = df["changeRate"].astype(float)
        df = df[df['instId'].str.endswith('USDT-SWAP')]  # 過濾 USDT 永續
        top_up = df.sort_values("changeRate", ascending=False).head(3)
        top_down = df.sort_values("changeRate").head(3)
        msg = "📈 Top3 漲幅榜:\n" + "\n".join([f"{r['instId']} {r['changeRate']*100:.2f}%" for _, r in top_up.iterrows()])
        msg += "\n\n📉 Top3 跌幅榜:\n" + "\n".join([f"{r['instId']} {r['changeRate']*100:.2f}%" for _, r in top_down.iterrows()])
        send_telegram(msg)
    except Exception as e:
        print("Top3 抓取錯誤:", e)

# ===== 心跳函數 =====
def heartbeat():
    send_telegram("💓 系統心跳正常")

# ===== 主排程函數 =====
def job():
    print("=== 執行 EMA + 吞沒判斷 ===", datetime.now(tz))
    get_top3()
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]  # 固定四幣種
    for sym in symbols:
        df = get_klines(sym, interval="30m")
        if df is None or df.empty:
            continue
        signal = ema_strategy(df)
        if signal:
            signal_key = f"{sym}_{signal}_{df.index[-1]}"
            if signal_key not in sent_signals:
                send_telegram(f"{sym} {signal} 收盤價 {df['close'].iloc[-1]}")
                sent_signals.add(signal_key)

# ===== APScheduler 排程設定 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(job, "cron", minute="*/15")      # 每 15 分鐘 EMA + Top3
scheduler.add_job(heartbeat, "interval", minutes=30)  # 心跳訊息
scheduler.start()

# ===== Flask Web Server =====
@app.route("/")
def index():
    return "OKX EMA 永續合約監控系統運行中 ✅"

# ===== Ping 健康檢查 =====
@app.route("/ping")
def ping():
    return "pong ✅", 200

if __name__ == "__main__":
    send_telegram("✅ OKX EMA 永續合約監控系統啟動")
    job()  # 啟動時立即抓一次
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
