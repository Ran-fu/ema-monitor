from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import datetime
import pandas as pd
import matplotlib.pyplot as plt
import io

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"[Telegram] Status: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def send_telegram_image(image_buffer, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": image_buffer}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    try:
        response = requests.post(url, data=data, files=files, timeout=10)
        print(f"[Telegram Image] Status: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"Telegram image error: {e}")

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['close'] > prev['open'] and curr['open'] < prev['close']

def is_bearish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['close'] < prev['open'] and curr['open'] > prev['close']

def plot_candlestick(df, entry, tp, sl, symbol):
    df_plot = df[-50:].copy()
    fig, ax = plt.subplots(figsize=(10, 5))

    for idx, row in df_plot.iterrows():
        color = 'green' if row['close'] >= row['open'] else 'red'
        ax.plot([idx, idx], [row['low'], row['high']], color='black', linewidth=0.5)
        ax.plot([idx, idx], [row['open'], row['close']], color=color, linewidth=3)

    ax.plot(df_plot.index, df_plot['ema12'], label='EMA12', color='blue', linestyle='--')
    ax.plot(df_plot.index, df_plot['ema30'], label='EMA30', color='orange', linestyle='--')
    ax.plot(df_plot.index, df_plot['ema55'], label='EMA55', color='purple', linestyle='--')

    ax.axhline(entry, color='orange', linestyle='-', label='Entry')
    ax.axhline(tp, color='green', linestyle='--', label='Take Profit')
    ax.axhline(sl, color='red', linestyle='--', label='Stop Loss')

    ax.set_title(f"{symbol} - Trade Signal")
    ax.legend()
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

def analyze_symbol(symbol):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=30m&limit=100"
        response = requests.get(url, timeout=10)
        data = response.json()

        if 'data' not in data:
            print(f"No data for {symbol}")
            return

        df = pd.DataFrame(data['data'], columns=[
            'ts', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'
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

                image = plot_candlestick(df, entry, tp, sl, symbol)
                send_telegram_image(image, caption=msg)

        # 空頭策略判斷
        if last['ema12'] < last['ema30'] < last['ema55']:
            if last['high'] >= last['ema30'] and last['high'] < last['ema55'] and is_bearish_engulfing(df):
                entry = last['close']
                sl = round(last['ema55'], 4)
                tp = round(entry * 0.985, 4)
                msg = f"📉 空頭訊號：{symbol}\n進場價：{entry}\n止盈：{tp}\n止損：{sl}"
                send_telegram_message(msg)

                image = plot_candlestick(df, entry, tp, sl, symbol)
                send_telegram_image(image, caption=msg)

    except Exception as e:
        print(f"分析錯誤 {symbol}:", e)

def monitor():
    try:
        url = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"
        response = requests.get(url, timeout=10)
        data = response.json()

        if 'data' not in data:
            print("No instruments data")
            return

        usdt_pairs = [item['instId'] for item in data['data'] if item['instId'].endswith("USDT")]

        print(f"Analyzing {len(usdt_pairs)} pairs...")
        for symbol in usdt_pairs:
            analyze_symbol(symbol)

    except Exception as e:
        print("Monitor error:", e)

@app.route('/')
def home():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    send_telegram_message(f"📡 手動觸發測試訊號！\n目前時間：{now}")
    return 'EMA Monitor is running!'

if __name__ == '__main__':
    scheduler = BackgroundScheduler()
    scheduler.add_job(monitor, 'interval', minutes=5)
    scheduler.start()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    send_telegram_message(f"🚀 EMA 監控系統已啟動！\n目前時間：{now}")

    app.run(host='0.0.0.0', port=8080)
