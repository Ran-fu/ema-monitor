from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import matplotlib.pyplot as plt
import io
import datetime
import time
import os

# Telegram 設定
TELEGRAM_TOKEN = '8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI'
TELEGRAM_CHAT_ID = '1634751416'

# EMA 參數
EMA_SHORT = 12
EMA_MID = 30
EMA_LONG = 55

# 保留已發送的訊號避免重複推播
sent_signals = set()

# 建 Flask App
app = Flask(__name__)

@app.route('/')
def home():
    return 'OKX EMA 監控啟動成功'

# 發送訊息至 Telegram
def send_telegram_message(message, image_path=None):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    requests.post(url, data=payload)

    if image_path:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto'
        with open(image_path, 'rb') as photo:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID}, files={'photo': photo})

# 取得 OKX 所有 USDT 幣種
def get_all_usdt_symbols():
    url = 'https://www.okx.com/api/v5/public/instruments?instType=SPOT'
    response = requests.get(url)
    data = response.json()
    symbols = []
    for item in data['data']:
        if item['instId'].endswith('USDT'):
            symbols.append(item['instId'])
    return symbols

# 抓取幣種 K 線資料
def fetch_klines(symbol):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=100'
    try:
        response = requests.get(url)
        klines = response.json()['data'][::-1]  # 時間由舊到新
        df = pd.DataFrame(klines, columns=[
            "timestamp", "open", "high", "low", "close",
            "volume", "volumeCcy", "volumeQuote", "confirm"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
        return df
    except Exception as e:
        print(f"{symbol} 無法解析資料格式：{e}")
        return None

# 計算 EMA 指標
def calculate_ema(df):
    df['ema12'] = df['close'].ewm(span=EMA_SHORT, adjust=False).mean()
    df['ema30'] = df['close'].ewm(span=EMA_MID, adjust=False).mean()
    df['ema55'] = df['close'].ewm(span=EMA_LONG, adjust=False).mean()
    return df

# 判斷是否出現看漲或看跌吞沒
def is_bullish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['close'] > prev['open'] and curr['open'] < prev['close']

def is_bearish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['open'] > prev['close'] and curr['close'] < prev['open']

# 畫圖
def plot_kline(df, symbol, direction):
    plt.figure(figsize=(10, 4))
    plt.title(f"{symbol} - {direction}")
    plt.plot(df['timestamp'], df['close'], label='Close')
    plt.plot(df['timestamp'], df['ema12'], label='EMA12')
    plt.plot(df['timestamp'], df['ema30'], label='EMA30')
    plt.plot(df['timestamp'], df['ema55'], label='EMA55')
    plt.legend()
    filename = f"{symbol.replace('/', '_')}_{int(time.time())}.png"
    plt.tight_layout()
    plt.savefig(filename)
    plt.close()
    return filename

# 核心策略邏輯
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = calculate_ema(df)

    # 多頭條件
    long_cond = (
        df['ema12'].iloc[-1] > df['ema30'].iloc[-1] > df['ema55'].iloc[-1] and
        df['low'].iloc[-1] <= df['ema30'].iloc[-1] and
        df['low'].iloc[-1] > df['ema55'].iloc[-1] and
        is_bullish_engulfing(df)
    )

    # 空頭條件
    short_cond = (
        df['ema12'].iloc[-1] < df['ema30'].iloc[-1] < df['ema55'].iloc[-1] and
        df['high'].iloc[-1] >= df['ema30'].iloc[-1] and
        df['high'].iloc[-1] < df['ema55'].iloc[-1] and
        is_bearish_engulfing(df)
    )

    if long_cond and symbol + "_long" not in sent_signals:
        image_path = plot_kline(df, symbol, "多單訊號")
        msg = f"📈 多頭訊號：{symbol}\n價格：{df['close'].iloc[-1]}"
        send_telegram_message(msg, image_path)
        sent_signals.add(symbol + "_long")

    elif short_cond and symbol + "_short" not in sent_signals:
        image_path = plot_kline(df, symbol, "空單訊號")
        msg = f"📉 空頭訊號：{symbol}\n價格：{df['close'].iloc[-1]}"
        send_telegram_message(msg, image_path)
        sent_signals.add(symbol + "_short")

# 主監控函數
def run_monitor():
    print(f"執行掃描時間：{datetime.datetime.now()}")
    symbols = get_all_usdt_symbols()
    for symbol in symbols:
        check_signal(symbol)

# 每 15 分鐘排程一次
scheduler = BackgroundScheduler()
scheduler.add_job(run_monitor, 'interval', minutes=15)
scheduler.start()

# 啟動時通知
send_telegram_message("✅ EMA 策略監控已啟動")

# 避免 Render 自動休眠可設保活 route
@app.route('/ping')
def ping():
    return 'pong'

# Run Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
