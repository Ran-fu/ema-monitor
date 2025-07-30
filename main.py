from flask import Flask
import pandas as pd
import requests
import matplotlib.pyplot as plt
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
import datetime
import time

app = Flask(__name__)

# ========== Telegram 設定 ==========
TELEGRAM_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
CHAT_ID = "1634751416"

# ========== Binance API ==========
client = Client()

# ========== 全域變數：防重複發送 ==========
notified_symbols = {}

# ========== 判斷看漲吞沒形態 ==========
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (
        prev['close'] < prev['open'] and
        curr['close'] > curr['open'] and
        curr['close'] > prev['open'] and
        curr['open'] < prev['close']
    )

# ========== 畫圖＋傳送訊號 ==========
def send_telegram_message_with_chart(symbol, df, signal_time):
    plt.figure(figsize=(10, 5))
    plt.plot(df['close'], label='Close', color='black')
    plt.plot(df['EMA12'], label='EMA12', color='blue')
    plt.plot(df['EMA30'], label='EMA30', color='green')
    plt.plot(df['EMA55'], label='EMA55', color='red')
    plt.title(f'{symbol} Signal @ {signal_time}')
    plt.legend()
    
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': buf}
    data = {'chat_id': CHAT_ID, 'caption': f'{symbol} 出現看漲吞沒訊號 @ {signal_time}'}
    requests.post(url, files=files, data=data)

# ========== 主邏輯：檢查全部 USDT 幣種 ==========
def check_binance_symbols():
    global notified_symbols

    exchange_info = client.get_exchange_info()
    symbols = [
        s['symbol'] for s in exchange_info['symbols']
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and not s['symbol'].endswith('DOWNUSDT') and not s['symbol'].endswith('UPUSDT')
    ]

    for symbol in symbols:
        try:
            klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_15MINUTE, limit=100)
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_volume', 'taker_buy_quote_volume', 'ignore'
            ])
            df['open'] = df['open'].astype(float)
            df['close'] = df['close'].astype(float)
            df['low'] = df['low'].astype(float)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()

            if len(df) < 60:
                continue

            last = df.iloc[-1]
            if (
                df['EMA12'].iloc[-1] > df['EMA30'].iloc[-1] > df['EMA55'].iloc[-1] and
                last['low'] < df['EMA30'].iloc[-1] and last['low'] > df['EMA55'].iloc[-1] and
                is_bullish_engulfing(df)
            ):
                if symbol not in notified_symbols or (datetime.datetime.now() - notified_symbols[symbol]).seconds > 3600:
                    signal_time = datetime.datetime.fromtimestamp(int(df['timestamp'].iloc[-1]/1000)).strftime("%Y-%m-%d %H:%M")
                    send_telegram_message_with_chart(symbol, df.tail(50), signal_time)
                    notified_symbols[symbol] = datetime.datetime.now()
        except Exception as e:
            print(f"[{symbol}] 發生錯誤: {e}")

# ========== Flask 路由 ==========
@app.route('/')
def home():
    return 'Binance EMA 監控器運作中'

# ========== 排程設定 ==========
scheduler = BackgroundScheduler()
scheduler.add_job(check_binance_symbols, 'interval', minutes=15)
scheduler.start()

# ========== 啟動伺服器 ==========
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
