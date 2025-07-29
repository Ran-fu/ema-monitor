import time
import requests
import threading
from flask import Flask
import pandas as pd

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

app = Flask(__name__)

@app.route('/')
def home():
    return 'EMA Monitor is running!'

# 發送 Telegram 訊息
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# 計算 EMA
def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

# 檢查吞沒形態
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['close'] > prev['open'] and curr['open'] < prev['close']

def is_bearish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['close'] < prev['open'] and curr['open'] > prev['close']

# 分析單一幣種
def analyze_symbol(symbol):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=30m&limit=100"
        response = requests.get(url, timeout=10)
        data = response.json()

        if 'data' not in data:
            return

        df = pd.DataFrame(data['data'], columns=[
            'ts', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm', 'chgPct', 'chgAmt'
        ])
        df = df.astype({'open': 'float', 'high': 'float', 'low': 'float', 'close': 'float'})

        df = df[::-1].reset_index(drop=True)

        df['ema12'] = ema(df['close'], 12)
        df['ema30'] = ema(df['close'], 30)
        df['ema55'] = ema(df['close'], 55)

        last = df.iloc[-1]

        # 多頭策略判斷
        if last['ema12'] > last['ema30'] > last['ema55']:
            if last['low'] <= last['ema30'] and last['low'] > last['ema55'] and is_bullish_engulfing(df):
                entry = last['close']
                sl = round(last['ema55'], 4)
                tp = round(entry * 1.015, 4)
                msg = f"📈 多頭訊號：{symbol}\n進場價：{entry}\n止盈：{tp}\n止損：{sl}"
                send_telegram_message(msg)

        # 空頭策略判斷
        if last['ema12'] < last['ema30'] < last['ema55']:
            if last['high'] >= last['ema30'] and last['high'] < last['ema55'] and is_bearish_engulfing(df):
                entry = last['close']
                sl = round(last['ema55'], 4)
                tp = round(entry * 0.985, 4)
                msg = f"📉 空頭訊號：{symbol}\n進場價：{entry}\n止盈：{tp}\n止損：{sl}"
                send_telegram_message(msg)

    except Exception as e:
        print(f"分析錯誤 {symbol}:", e)

# 每 5 分鐘更新一次資料
def monitor_loop():
    while True:
        try:
            url = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"
            response = requests.get(url, timeout=10)
            data = response.json()

            if 'data' not in data:
                time.sleep(300)
                continue

            usdt_pairs = [item['instId'] for item in data['data'] if item['instId'].endswith("-USDT")]

            print(f"正在分析 {len(usdt_pairs)} 個幣種...")
            for symbol in usdt_pairs:
                analyze_symbol(symbol)

        except Exception as e:
            print("主迴圈錯誤:", e)

        print("等待 5 分鐘...")
        time.sleep(300)

# 啟動後台監控
threading.Thread(target=monitor_loop, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
