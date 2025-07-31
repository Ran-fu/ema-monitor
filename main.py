from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "你的 Bot Token"
TELEGRAM_CHAT_ID = "你的 Chat ID"

# 已發送過的訊號記錄
sent_signals = {}

# === 取得 OKX 合約 USDT 對 K 線 ===
def get_klines(symbol):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=100'
    response = requests.get(url)
    data = response.json()
    df = pd.DataFrame(data['data'], columns=[
        'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
    ])
    df = df.astype(float)
    df = df.iloc[::-1].reset_index(drop=True)
    df['EMA12'] = df['close'].ewm(span=12).mean()
    df['EMA30'] = df['close'].ewm(span=30).mean()
    df['EMA55'] = df['close'].ewm(span=55).mean()
    return df

# === 判斷吞沒形態 ===
def is_bullish_engulfing(df):
    c1, o1 = df['close'].iloc[-3], df['open'].iloc[-3]
    c2, o2 = df['close'].iloc[-2], df['open'].iloc[-2]
    return (c1 < o1) and (c2 > o2) and (c2 > o1) and (o2 < c1)

def is_bearish_engulfing(df):
    c1, o1 = df['close'].iloc[-3], df['open'].iloc[-3]
    c2, o2 = df['close'].iloc[-2], df['open'].iloc[-2]
    return (c1 > o1) and (c2 < o2) and (c2 < o1) and (o2 > c1)

# === 發送 Telegram 訊息 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, json=payload)

# === 主策略判斷函式 ===
def check_signals():
    print("正在檢查訊號...")
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    response = requests.get(url)
    instruments = response.json()['data']
    usdt_pairs = [inst['instId'] for inst in instruments if inst['instId'].endswith("USDT-SWAP")]

    for symbol in usdt_pairs:
        try:
            df = get_klines(symbol)
            if len(df) < 60:
                continue

            ema12 = df['EMA12'].iloc[-2]
            ema30 = df['EMA30'].iloc[-2]
            ema55 = df['EMA55'].iloc[-2]
            close = df['close'].iloc[-2]

            is_up = ema12 > ema30 > ema55
            is_down = ema12 < ema30 < ema55

            signal_key = f"{symbol}-{df['ts'].iloc[-2]}"

            if is_up and df['low'].iloc[-2] <= ema30 and df['low'].iloc[-2] > ema55:
                if is_bullish_engulfing(df) and signal_key not in sent_signals:
                    msg = f"{symbol} 看漲吞沒，收盤價：{close:.4f}"
                    send_telegram_message(msg)
                    sent_signals[signal_key] = True

            elif is_down and df['high'].iloc[-2] >= ema30 and df['high'].iloc[-2] < ema55:
                if is_bearish_engulfing(df) and signal_key not in sent_signals:
                    msg = f"{symbol} 看跌吞沒，收盤價：{close:.4f}"
                    send_telegram_message(msg)
                    sent_signals[signal_key] = True

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")

# === Flask 路由 ===
@app.route('/')
def home():
    return 'EMA 吞沒策略伺服器運行中'

@app.route('/ping')
def ping():
    return 'pong'

# === 啟動 Scheduler ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'interval', minutes=15)
scheduler.start()

# 啟動時傳送一則訊息
send_telegram_message("🚀 OKX EMA 吞沒監控已啟動")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
