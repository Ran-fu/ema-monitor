from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd
import requests
import time
import datetime

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = '8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI'
TELEGRAM_CHAT_ID = '1634751416'
BINANCE_URL = "https://fapi.binance.com"

sent_signals = {}

def get_binance_symbols():
    res = requests.get(f"{BINANCE_URL}/fapi/v1/exchangeInfo")
    data = res.json()
    return [s['symbol'] for s in data['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']

def fetch_klines(symbol, interval='15m', limit=100):
    url = f"{BINANCE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    res = requests.get(url, params=params)
    df = pd.DataFrame(res.json(), columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "num_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["time"] = pd.to_datetime(df["open_time"], unit='ms')
    return df[["time", "open", "high", "low", "close"]]

def is_bullish_engulfing(df):
    c1 = df.iloc[-2]
    c2 = df.iloc[-1]
    return (
        c1["close"] < c1["open"] and
        c2["close"] > c2["open"] and
        c2["close"] > c1["open"] and
        c2["open"] < c1["close"]
    )

def analyze_symbol(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df['EMA12'] = df['close'].ewm(span=12).mean()
    df['EMA30'] = df['close'].ewm(span=30).mean()
    df['EMA55'] = df['close'].ewm(span=55).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭排列檢查
    if not (last['EMA12'] > last['EMA30'] > last['EMA55']):
        return

    # 回踩EMA30且未碰EMA55
    if not (last['low'] <= last['EMA30'] and last['low'] > last['EMA55']):
        return

    # 看漲吞沒
    if not is_bullish_engulfing(df):
        return

    signal_id = f"{symbol}_{df.iloc[-1]['time']}"
    if signal_id in sent_signals:
        return  # 避免重複發送

    sent_signals[signal_id] = True
    msg = f"📈 多頭訊號：{symbol}\n時間：{df.iloc[-1]['time']}\n價格：{last['close']:.2f}"
    send_telegram(msg)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, data=data)

def job():
    print(f"開始分析：{datetime.datetime.now()}")
    try:
        symbols = get_binance_symbols()
        for symbol in symbols:
            try:
                analyze_symbol(symbol)
            except Exception as e:
                print(f"{symbol} 分析錯誤：{e}")
    except Exception as e:
        print(f"分析任務錯誤：{e}")

scheduler = BackgroundScheduler()
scheduler.add_job(job, 'interval', minutes=15)
scheduler.start()

@app.route("/")
def home():
    return "Binance 合約監控系統啟動中"

@app.route("/ping")
def ping():
    return "pong", 200

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
