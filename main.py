from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import os
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 固定監控幣種
FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# 已發送訊號記錄
sent_signals = {}

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for key in list(sent_signals.keys()):
        if sent_signals[key] < cutoff:
            del sent_signals[key]

# === 抓取 K 線 ===
def get_klines(symbol, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size=100'
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10)
            data = r.json()['data']
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] K線抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return prev_close < prev_open and last_close > last_open and last_close > prev_open and last_open < prev_close

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return prev_close > prev_open and last_close < last_open and last_close < prev_open and last_open > prev_close

# === Telegram 發訊 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if not r.ok:
            print(f"Telegram 發送失敗：{r.text}")
        else:
            print(f"Telegram 發送成功：{text}")
    except Exception as e:
        print(f"Telegram 發送異常：{e}")

# === 取得當日成交量 Top3 ===
def get_top3_volume():
    try:
        r = requests.get("https://api.bitunix.com/api/v1/market/tickers", timeout=10)
        data = r.json().get("data", [])
        df = pd.DataFrame(data)
        df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0)
        usdt_df = df[df['symbol'].str.endswith('USDT')]
        top3 = usdt_df.nlargest(3, 'vol')['symbol'].tolist()
        return top3
    except Exception as e:
        print(f"取得成交量 Top3 失敗：{e}")
        return []

# === EMA 策略 ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號")
    cleanup_old_signals()

    symbols = list(set(FIXED_SYMBOLS + get_top3_volume()))
    for symbol in symbols:
        try:
            df = get_klines(symbol)
            if len(df) < 60:
                continue

            ema12, ema30, ema55 = df['EMA12'].iloc[-1], df['EMA30'].iloc[-1], df['EMA55'].iloc[-1]
            low, high, close = df['low'].iloc[-1], df['high'].iloc[-1], df['close'].iloc[-1]
            candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
            key_bull, key_bear = f"{symbol}-{candle_time}-bull", f"{symbol}-{candle_time}-bear"

            # 多頭排列
            if ema12 > ema30 > ema55 and low <= ema30 and low > ema55:
                if is_bullish_engulfing(df) and key_bull not in sent_signals:
                    msg = f"🟢 {symbol} 看漲吞沒 收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[key_bull] = datetime.utcnow()

            # 空頭排列
            elif ema12 < ema30 < ema55 and high >= ema30 and high < ema55:
                if is_bearish_engulfing(df) and key_bear not in sent_signals:
                    msg = f"🔴 {symbol} 看跌吞沒 收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[key_bear] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 錯誤：{e}")

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("<h1>🚀 EMA 吞沒策略運行中 (Bitunix)</h1>")

@app.route('/ping')
def ping():
    return 'pong'

# === 排程 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')
scheduler.start()

# 啟動即執行一次
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
