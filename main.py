from flask import Flask
import pandas as pd
import requests
import matplotlib.pyplot as plt
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
import datetime

app = Flask(__name__)

# ========== Telegram 設定 ==========
TELEGRAM_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
CHAT_ID = "1634751416"

# ========== 全域變數 ==========
notified_symbols = {}

# ========== 判斷吞沒形態 ==========
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev['close'] < prev['open'] and
        curr['close'] > curr['open'] and
        curr['close'] > prev['open'] and
        curr['open'] < prev['close']
    )

def is_bearish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev['close'] > prev['open'] and
        curr['close'] < curr['open'] and
        curr['close'] < prev['open'] and
        curr['open'] > prev['close']
    )

# ========== 發圖到 Telegram ==========
def send_telegram_chart(symbol, df, signal_time, direction):
    plt.figure(figsize=(10, 5))
    plt.plot(df['close'], label='Close', color='black')
    plt.plot(df['EMA12'], label='EMA12', color='blue')
    plt.plot(df['EMA30'], label='EMA30', color='green')
    plt.plot(df['EMA55'], label='EMA55', color='red')
    plt.title(f'{symbol} {direction.upper()} Signal @ {signal_time}')
    plt.legend()

    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    caption = f"{symbol} 出現{'多頭' if direction == 'long' else '空頭'}吞沒訊號 @ {signal_time}"
    requests.post(url, files={'photo': buf}, data={'chat_id': CHAT_ID, 'caption': caption})

# ========== 抓幣種清單 ==========
def get_okx_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"
    r = requests.get(url)
    data = r.json()
    return [item['instId'] for item in data['data'] if item['instId'].endswith("USDT")]

# ========== 抓 K 線 ==========
def get_klines(symbol):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=100"
    r = requests.get(url)
    data = r.json()
    df = pd.DataFrame(data['data'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', '_', '__', '___', '____', '_____'])
    df = df.iloc[::-1]
    df['open'] = df['open'].astype(float)
    df['close'] = df['close'].astype(float)
    df['low'] = df['low'].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['EMA12'] = df['close'].ewm(span=12).mean()
    df['EMA30'] = df['close'].ewm(span=30).mean()
    df['EMA55'] = df['close'].ewm(span=55).mean()
    return df

# ========== 核心判斷邏輯 ==========
def check_signals():
    global notified_symbols
    symbols = get_okx_symbols()
    now = datetime.datetime.now()

    for symbol in symbols:
        try:
            df = get_klines(symbol)
            if len(df) < 60:
                continue

            last = df.iloc[-1]
            ema12 = df['EMA12'].iloc[-1]
            ema30 = df['EMA30'].iloc[-1]
            ema55 = df['EMA55'].iloc[-1]

            # 多頭條件
            if (
                ema12 > ema30 > ema55 and
                last['low'] < ema30 and last['low'] > ema55 and
                is_bullish_engulfing(df)
            ):
                if notified_symbols.get(symbol) != 'long':
                    signal_time = df['timestamp'].iloc[-1].strftime("%Y-%m-%d %H:%M")
                    send_telegram_chart(symbol, df.tail(50), signal_time, 'long')
                    notified_symbols[symbol] = 'long'

            # 空頭條件
            elif (
                ema12 < ema30 < ema55 and
                last['high'] > ema30 and last['high'] < ema55 and
                is_bearish_engulfing(df)
            ):
                if notified_symbols.get(symbol) != 'short':
                    signal_time = df['timestamp'].iloc[-1].strftime("%Y-%m-%d %H:%M")
                    send_telegram_chart(symbol, df.tail(50), signal_time, 'short')
                    notified_symbols[symbol] = 'short'

        except Exception as e:
            print(f"[{symbol}] 發生錯誤: {e}")

# ========== 啟動時推送訊息 ==========
def send_startup_message():
    msg = f"✅ OKX EMA 多空策略監控器已啟動！\n時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': CHAT_ID, 'text': msg})

# ========== Flask 路由 ==========
@app.route('/')
def home():
    return "✅ OKX EMA 多空監控器運作中"

# ========== 排程啟動 ==========
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'interval', minutes=15)
scheduler.start()

# ========== 啟動應用 ==========
if __name__ == '__main__':
    send_startup_message()
    app.run(host='0.0.0.0', port=8080)
