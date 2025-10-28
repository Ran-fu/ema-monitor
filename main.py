from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import json

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = 1634751416

# === 全域變數 ===
sent_signals = {}
today_top3 = []

# === 時區轉換 ===
def taipei_time(dt):
    return (dt + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')

# === 傳送 Telegram 訊息 ===
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[Telegram 錯誤] {e}")

# === 取得 OKX K 線資料 ===
def get_klines(symbol, limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT-SWAP&bar=30m&limit={limit}"
    try:
        r = requests.get(url)
        data = r.json()
        if "data" not in data:
            print(f"[{symbol}] 資料格式錯誤：{data}")
            return pd.DataFrame()
        df = pd.DataFrame(data["data"], columns=["ts","open","high","low","close","volume","volCcy","volCcyQuote","confirm"])
        df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms")
        df = df.sort_values("ts")
        df["EMA12"] = df["close"].ewm(span=12).mean()
        df["EMA30"] = df["close"].ewm(span=30).mean()
        df["EMA55"] = df["close"].ewm(span=55).mean()
        return df
    except Exception as e:
        print(f"[{symbol}] K線抓取失敗：{e}")
        return pd.DataFrame()

# === 吞沒判斷（含十字線）===
def is_bullish_engulfing(prev_open, prev_close, open, close):
    body_prev = abs(prev_close - prev_open)
    body_curr = abs(close - open)
    return (
        # 前一根是陰線或十字線
        (prev_close <= prev_open or body_prev <= (body_curr * 0.3))
        # 當前是陽線
        and close > open
        # 當前實體吞沒前一根實體
        and close > prev_open
        and open < prev_close
    )

def is_bearish_engulfing(prev_open, prev_close, open, close):
    body_prev = abs(prev_close - prev_open)
    body_curr = abs(close - open)
    return (
        # 前一根是陽線或十字線
        (prev_close >= prev_open or body_prev <= (body_curr * 0.3))
        # 當前是陰線
        and close < open
        # 當前實體吞沒前一根實體
        and close < prev_open
        and open > prev_close
    )

# === 清理舊訊號（每日重置）===
def cleanup_old_signals():
    today = datetime.utcnow().date()
    for key in list(sent_signals.keys()):
        if sent_signals[key]["date"] != today:
            del sent_signals[key]

# === 更新每日成交量 Top3 ===
def update_today_top3():
    global today_top3
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        data = requests.get(url).json()["data"]
        df = pd.DataFrame(data)
        df["volume"] = df["vol24h"].astype(float)
        df = df[df["instId"].str.endswith("-USDT-SWAP")]
        top3 = df.nlargest(3, "volume")["instId"].str.replace("-USDT-SWAP", "").tolist()
        today_top3 = top3
        print(f"今日成交量 Top3: {today_top3}")
    except Exception as e:
        print(f"取得成交量 Top3 失敗：{e}")

# === 檢查訊號 ===
def check_signals():
    print(f"\n[{taipei_time(datetime.utcnow())}] 開始檢查訊號...")
    cleanup_old_signals()
    update_today_top3()

    main_symbols = ["BTC", "ETH", "SOL", "XRP"]
    watch_symbols = list(set(main_symbols + today_top3))

    for symbol in watch_symbols:
        df = get_klines(symbol)
        if df.empty or len(df) < 60:
            continue

        ema12, ema30, ema55 = df["EMA12"].iloc[-1], df["EMA30"].iloc[-1], df["EMA55"].iloc[-1]
        prev_open, prev_close = df["open"].iloc[-2], df["close"].iloc[-2]
        open, close = df["open"].iloc[-1], df["close"].iloc[-1]
        candle_time = (df["ts"].iloc[-1] + timedelta(hours=8)).floor("30T").strftime("%Y-%m-%d %H:%M")

        # === 多頭排列 & 看漲吞沒 ===
        if ema12 > ema30 > ema55 and is_bullish_engulfing(prev_open, prev_close, open, close):
            signal_key = f"{symbol}_bull_{candle_time}"
            if signal_key not in sent_signals:
                msg = f"🟢【看漲吞沒】{symbol}/USDT\n收盤價：{close}\n時間：{candle_time}"
                send_telegram_message(msg)
                sent_signals[signal_key] = {"date": datetime.utcnow().date()}
                print(f"{symbol} 看漲吞沒 → 已發送")

        # === 空頭排列 & 看跌吞沒 ===
        elif ema12 < ema30 < ema55 and is_bearish_engulfing(prev_open, prev_close, open, close):
            signal_key = f"{symbol}_bear_{candle_time}"
            if signal_key not in sent_signals:
                msg = f"🔴【看跌吞沒】{symbol}/USDT\n收盤價：{close}\n時間：{candle_time}"
                send_telegram_message(msg)
                sent_signals[signal_key] = {"date": datetime.utcnow().date()}
                print(f"{symbol} 看跌吞沒 → 已發送")

# === Flask 心跳頁 ===
@app.route("/")
def home():
    now = taipei_time(datetime.utcnow())
    return f"<h3>✅ EMA 監控執行中<br>台灣時間：{now}</h3>"

# === 定時任務設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, "cron", minute="2,32")  # 每 30 分收盤後 2 分鐘檢查
scheduler.start()

if __name__ == "__main__":
    print("🚀 EMA 吞沒監控系統啟動中...")
    check_signals()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
