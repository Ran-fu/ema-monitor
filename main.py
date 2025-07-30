import pandas as pd
import requests
import time
import io
import matplotlib.pyplot as plt
import mplfinance as mpf
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 幣安期貨 API
BASE_URL = "https://fapi.binance.com"

# EMA 參數
EMA_SHORT = 12
EMA_MID = 30
EMA_LONG = 55

# 已發送過的訊號（避免重複推播）
sent_signals = set()

@app.route('/')
def home():
    return 'Binance EMA Signal Monitor is running!'

@app.route('/ping')
def ping():
    return 'pong', 200

def get_binance_usdt_symbols():
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    response = requests.get(url)
    data = response.json()
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    ]
    return symbols

def get_klines(symbol, interval="15m", limit=100):
    url = f"{BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url)
    data = response.json()
    df = pd.DataFrame(data, columns=[
        'timestamp','open','high','low','close','volume',
        'close_time','quote_asset_volume','trades',
        'taker_buy_base_asset_volume','taker_buy_quote_asset_volume','ignore'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[['open','high','low','close','volume']].astype(float)
    return df

def is_bullish_engulfing(df):
    c1, c2 = df.iloc[-2], df.iloc[-1]
    return c1['close'] < c1['open'] and c2['close'] > c2['open'] and c2['close'] > c1['open'] and c2['open'] < c1['close']

def is_bearish_engulfing(df):
    c1, c2 = df.iloc[-2], df.iloc[-1]
    return c1['close'] > c1['open'] and c2['close'] < c2['open'] and c2['close'] < c1['open'] and c2['open'] > c1['close']

def check_signal(symbol):
    try:
        df = get_klines(symbol)
        df['EMA12'] = df['close'].ewm(span=EMA_SHORT).mean()
        df['EMA30'] = df['close'].ewm(span=EMA_MID).mean()
        df['EMA55'] = df['close'].ewm(span=EMA_LONG).mean()

        # 多頭排列 + 回踩EMA30 + 看漲吞沒
        if (
            df['EMA12'].iloc[-1] > df['EMA30'].iloc[-1] > df['EMA55'].iloc[-1] and
            df['low'].iloc[-1] <= df['EMA30'].iloc[-1] and
            df['low'].iloc[-1] > df['EMA55'].iloc[-1] and
            is_bullish_engulfing(df)
        ):
            key = f"{symbol}_LONG_{df.index[-1]}"
            if key not in sent_signals:
                sent_signals.add(key)
                send_signal(symbol, "多單訊號", df)
        # 空頭排列 + 回踩EMA30 + 看跌吞沒
        elif (
            df['EMA12'].iloc[-1] < df['EMA30'].iloc[-1] < df['EMA55'].iloc[-1] and
            df['high'].iloc[-1] >= df['EMA30'].iloc[-1] and
            df['high'].iloc[-1] < df['EMA55'].iloc[-1] and
            is_bearish_engulfing(df)
        ):
            key = f"{symbol}_SHORT_{df.index[-1]}"
            if key not in sent_signals:
                sent_signals.add(key)
                send_signal(symbol, "空單訊號", df)
    except Exception as e:
        print(f"{symbol} error: {e}")

def send_signal(symbol, signal_type, df):
    last_price = df['close'].iloc[-1]
    msg = f"🚨 {signal_type}：{symbol}\n價格：{last_price:.4f}"
    print(msg)
    send_telegram_message(msg)
    send_chart_image(symbol, df)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    requests.post(url, json=payload)

def send_chart_image(symbol, df):
    df = df.tail(50)
    apds = [
        mpf.make_addplot(df['EMA12'], color='red'),
        mpf.make_addplot(df['EMA30'], color='blue'),
        mpf.make_addplot(df['EMA55'], color='green')
    ]
    fig, axlist = mpf.plot(
        df, type='candle', style='charles',
        addplot=apds, returnfig=True, volume=True
    )
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {'photo': buf}
    data = {'chat_id': TELEGRAM_CHAT_ID}
    requests.post(url, files=files, data=data)

def run_all():
    print("⏰ 開始分析所有幣種...")
    symbols = get_binance_usdt_symbols()
    for symbol in symbols:
        check_signal(symbol)

# 每 15 分鐘自動分析
scheduler = BackgroundScheduler()
scheduler.add_job(run_all, 'interval', minutes=15)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)p
