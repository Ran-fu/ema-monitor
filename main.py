from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, json

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# ===== OKX 設定 =====
OKX_BASE = "https://www.okx.com/api/v5/market"

# ===== EMA 與策略判斷函數 =====
def get_klines(symbol, interval="15m", limit=200):
    url = f"{OKX_BASE}/candles?instId={symbol}-SWAP&bar={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
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

def ema_strategy(df):
    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()
    df["signal"] = None

    # 最後一根收盤 K
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭排列 + 回踩 EMA30 + 看漲吞沒
    if last["EMA12"] > last["EMA30"] > last["EMA55"]:
        if prev["low"] > last["EMA55"] and prev["close"] < prev["open"] and last["close"] > last["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
            return "看漲吞沒"
    # 空頭排列 + 回踩 EMA30 + 看跌吞沒
    elif last["EMA12"] < last["EMA30"] < last["EMA55"]:
        if prev["high"] < last["EMA55"] and prev["close"] > prev["open"] and last["close"] < last["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]:
            return "看跌吞沒"
    return None

# ===== Telegram 發訊函數 =====
def send_telegram(msg):
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(TELEGRAM_URL, data=payload, timeout=10)
    except Exception as e:
        print("Telegram 發訊錯誤:", e)

# ===== Top3 漲跌幅函數 =====
def get_top3():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        r = requests.get(url, timeout=10).json()
        tickers = r.get("data", [])
        df = pd.DataFrame(tickers)
        df["changeRate"] = df["changeRate"].astype(float)
        top_up = df.sort_values("changeRate", ascending=False).head(3)
        top_down = df.sort_values("changeRate").head(3)
        msg = "📈 Top3 漲幅榜:\n" + "\n".join([f"{r['instId']} {r['changeRate']*100:.2f}%" for _, r in top_up.iterrows()])
        msg += "\n\n📉 Top3 跌幅榜:\n" + "\n".join([f"{r['instId']} {r['changeRate']*100:.2f}%" for _, r in top_down.iterrows()])
        send_telegram(msg)
    except Exception as e:
        print("Top3 抓取錯誤:", e)

# ===== 主排程函數 =====
def job():
    print("=== 執行 EMA + 吞沒判斷 ===", datetime.now(tz))
    get_top3()
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]  # 可擴展全 USDT 永續
    for sym in symbols:
        df = get_klines(sym, interval="30m")
        if df is None or df.empty:
            continue
        signal = ema_strategy(df)
        if signal:
            send_telegram(f"{sym} {signal} 收盤價 {df['close'].iloc[-1]}")

# ===== 啟動 =====
send_telegram("✅ OKX EMA 永續合約監控系統啟動")
job()  # 啟動時立即抓一次

# ===== APScheduler 排程 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(job, "cron", minute="*/15")  # 每 15 分鐘抓一次
scheduler.start()

# ===== Flask Web Server =====
@app.route("/")
def index():
    return "OKX EMA 永續合約監控系統運行中 ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
