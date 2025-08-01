from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"  # ← 移除多餘空白

# 已發送過的訊號記錄
sent_signals = {}

# === 取得 OKX 合約 USDT 對 K 線 ===
def get_klines(symbol, retries=3):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=100'
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=10)
            data = response.json()
            df = pd.DataFrame(data['data'], columns=[
                'ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
            ])
            df[['open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote']] = \
                df[['open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 判斷吞沒形態（使用已收盤的兩根 K 線）===
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
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 傳送失敗：{e}")

# === 主策略判斷函式 ===
def check_signals():
    print("正在檢查訊號...")
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    response = requests.get(url, timeout=10)
    instruments = response.json()['data']
    usdt_pairs = [inst['instId'] for inst in instruments if inst['instId'].endswith("USDT-SWAP")]

    for symbol in usdt_pairs:
        try:
            df = get_klines(symbol)
            if len(df) < 60:
                continue

            # 使用倒數第二根 K 線進行已收盤判斷
            ema12 = df['EMA12'].iloc[-2]
            ema30 = df['EMA30'].iloc[-2]
            ema55 = df['EMA55'].iloc[-2]
            close = df['close'].iloc[-2]
            low = df['low'].iloc[-2]
            high = df['high'].iloc[-2]

            is_up = ema12 > ema30 > ema55
            is_down = ema12 < ema30 < ema55
            candle_time = df['ts'].iloc[-2].strftime('%Y-%m-%d %H:%M')
            signal_key = f"{symbol}-{candle_time}"

            if is_up and low <= ema30 and low > ema55:
                if is_bullish_engulfing(df) and signal_key not in sent_signals:
                    msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f}"
                    send_telegram_message(msg)
                    sent_signals[signal_key] = True

            elif is_down and high >= ema30 and high < ema55:
                if is_bearish_engulfing(df) and signal_key not in sent_signals:
                    msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f}"
                    send_telegram_message(msg)
                    sent_signals[signal_key] = True

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")

# === Flask 路由 ===
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>EMA 吞沒策略</title>
        <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    </head>
    <body>
        <h1>🚀 EMA 吞沒策略伺服器運行中</h1>
    </body>
    </html>
    """)

@app.route('/ping')
def ping():
    return 'pong'

# === 啟動 Scheduler ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'interval', minutes=30)
scheduler.start()

# 啟動時傳送一則訊息
send_telegram_message("🚀 OKX EMA 吞沒監控已啟動")

# === 啟動 Flask 伺服器 ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
