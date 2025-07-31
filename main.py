from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 訊號記錄，避免重複通知
sent_signals = set()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"傳送 Telegram 訊息失敗: {e}")

def fetch_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    response = requests.get(url)
    usdt_pairs = []
    if response.status_code == 200:
        data = response.json()['data']
        for item in data:
            if item['instId'].endswith("USDT-SWAP"):
                usdt_pairs.append(item['instId'])
    return usdt_pairs

def fetch_klines(symbol):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=60"
    response = requests.get(url)
    if response.status_code != 200:
        return None
    raw = response.json()['data']
    raw.reverse()
    df = pd.DataFrame(raw, columns=[
        "timestamp", "open", "high", "low", "close", "volume", "volumeCcy",
        "volumeCcyQuote", "confirm", "tradeCount"
    ])
    df = df.astype({
        "open": float, "high": float, "low": float, "close": float
    })
    return df

def calculate_ema(df):
    df['ema12'] = df['close'].ewm(span=12).mean()
    df['ema30'] = df['close'].ewm(span=30).mean()
    df['ema55'] = df['close'].ewm(span=55).mean()
    return df

def is_bullish_engulfing(df):
    prev = df.iloc[-3]
    curr = df.iloc[-2]
    return (
        prev['close'] < prev['open'] and
        curr['close'] > curr['open'] and
        curr['close'] > prev['open'] and
        curr['open'] < prev['close']
    )

def is_bearish_engulfing(df):
    prev = df.iloc[-3]
    curr = df.iloc[-2]
    return (
        prev['close'] > prev['open'] and
        curr['close'] < curr['open'] and
        curr['close'] < prev['open'] and
        curr['open'] > prev['close']
    )

def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = calculate_ema(df)

    # 多單條件（收盤確認）
    long_cond = (
        df['ema12'].iloc[-2] > df['ema30'].iloc[-2] > df['ema55'].iloc[-2] and
        df['low'].iloc[-2] <= df['ema30'].iloc[-2] and
        df['low'].iloc[-2] > df['ema55'].iloc[-2] and
        is_bullish_engulfing(df)
    )

    # 空單條件（收盤確認）
    short_cond = (
        df['ema12'].iloc[-2] < df['ema30'].iloc[-2] < df['ema55'].iloc[-2] and
        df['high'].iloc[-2] >= df['ema30'].iloc[-2] and
        df['high'].iloc[-2] < df['ema55'].iloc[-2] and
        is_bearish_engulfing(df)
    )

    if long_cond and symbol + "_long" not in sent_signals:
        msg = f"📈 看漲吞沒：{symbol}\n收盤價：{df['close'].iloc[-2]}"
        send_telegram_message(msg)
        sent_signals.add(symbol + "_long")

    elif short_cond and symbol + "_short" not in sent_signals:
        msg = f"📉 看跌吞沒：{symbol}\n收盤價：{df['close'].iloc[-2]}"
        send_telegram_message(msg)
        sent_signals.add(symbol + "_short")

def run_monitor():
    print(f"⏰ 檢查時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    symbols = fetch_symbols()
    for symbol in symbols:
        check_signal(symbol)

@app.route('/')
def home():
    return "✅ OKX EMA 監控伺服器運作中"

# 啟動排程任務
scheduler = BackgroundScheduler()
scheduler.add_job(run_monitor, 'interval', minutes=15)
scheduler.start()

# 啟動時通知
send_telegram_message("✅ EMA 監控伺服器已啟動")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
