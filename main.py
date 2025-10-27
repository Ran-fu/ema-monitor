from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# === 監控幣種設定 (固定 BTC/ETH/SOL/XRP) ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
KLINE_INTERVAL = "30m"  # 30 分鐘 K 線
SIGNAL_RETENTION_HOURS = 24  # 信號保留 24 小時

sent_signals = set()

# === 取得 Bitunix K 線資料函數 ===
def fetch_kline(symbol):
    url = f"https://api.bitunix.io/api/v1/klines?symbol={symbol}&interval={KLINE_INTERVAL}&limit=100"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume","other1","other2","other3"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
        return df
    except Exception as e:
        print(f"[{symbol}] K線抓取失敗：{e}")
        return None

# === EMA + 吞沒策略判斷 ===
def calculate_ema_signal(df):
    df["ema12"] = df["close"].ewm(span=12).mean()
    df["ema30"] = df["close"].ewm(span=30).mean()
    df["ema55"] = df["close"].ewm(span=55).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭條件：EMA 多頭排列且回踩 EMA30 不碰 EMA55 + 看漲吞沒
    if last["ema12"] > last["ema30"] > last["ema55"] and prev["close"] < prev["open"] and last["close"] > last["open"] and last["low"] > last["ema55"]:
        return "看漲吞沒", last["close"]

    # 空頭條件：EMA 空頭排列且回踩 EMA30 不碰 EMA55 + 看跌吞沒
    if last["ema12"] < last["ema30"] < last["ema55"] and prev["close"] > prev["open"] and last["close"] < last["open"] and last["high"] < last["ema55"]:
        return "看跌吞沒", last["close"]

    return None, None

# === 發送 Telegram 訊息 ===
def send_telegram(msg):
    try:
        requests.get(TELEGRAM_API_URL, params={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        print(f"[Telegram] 發送失敗：{e}")

# === 清理過期信號 ===
def cleanup_sent_signals():
    global sent_signals
    now = datetime.utcnow()
    new_sent = set()
    for key in sent_signals:
        try:
            ts_str = "_".join(key.split("_")[-2:])  # 取得時間字串
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
            if (now - ts).total_seconds() <= SIGNAL_RETENTION_HOURS * 3600:
                new_sent.add(key)
        except Exception as e:
            print(f"[清理信號錯誤] {key}: {e}")
    sent_signals = new_sent

# === 每 30 分鐘檢查訊號 ===
def check_signals():
    global sent_signals
    cleanup_sent_signals()

    # 取得當日交易量 Top3
    volume_data = []
    for sym in SYMBOLS:
        df = fetch_kline(sym)
        if df is not None:
            today_volume = df.iloc[-1]["volume"]
            volume_data.append((sym, today_volume))
        else:
            print(f"[{sym}] 無法取得交易量，略過")
    if not volume_data:
        print("[系統] 所有幣種抓取失敗或今日無資料，跳過本次檢查")
        return
    volume_data.sort(key=lambda x: x[1], reverse=True)
    today_top3 = [v[0] for v in volume_data[:3]]

    for sym in SYMBOLS:
        df = fetch_kline(sym)
        if df is None or len(df) < 2:
            print(f"[{sym}] 多次抓取失敗或資料不足，略過")
            continue
        try:
            signal, price = calculate_ema_signal(df)
            if signal:
                key = f"{sym}_{signal}_{df.iloc[-1]['ts'].strftime('%Y-%m-%d %H:%M')}"
                if sym in today_top3 and key not in sent_signals:
                    msg = f"{sym} {signal} 收盤價: {price:.4f}"
                    send_telegram(msg)
                    sent_signals.add(key)
                    print(msg)
        except Exception as e:
            print(f"[{sym}] 信號計算錯誤: {e}")

# === Flask 路由 ===
@app.route("/")
def index():
    return "EMA 監控系統運行中"

@app.route("/ping")
def ping():
    return "pong"

# === 排程啟動 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, "cron", minute="2,32")  # 每根 30 分 K 線收盤後 2 分鐘判斷
scheduler.start()

# 啟動時發送訊息
send_telegram("[系統] EMA 監控系統啟動成功")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
