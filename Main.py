from flask import Flask, render_template_string, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 已發送過的訊號記錄（包含時間）
sent_signals = {}

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 抓取 Bitunix 30m K 線資料 ===
def get_klines(symbol, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size=100'
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=10)
            data = response.json()['data']
            df = pd.DataFrame(data, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)  # 時間正序
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === 傳送 Telegram 訊息 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"Telegram 發送失敗: {response.text}")
        else:
            print(f"Telegram 發送成功: {text}")
    except Exception as e:
        print(f"Telegram 發送異常：{e}")

# === EMA 策略邏輯 ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
    cleanup_old_signals()

    # 取得 Bitunix 所有交易對
    try:
        url = "https://api.bitunix.com/api/v1/market/symbols"
        response = requests.get(url, timeout=10)
        instruments = response.json()['data']
        usdt_pairs = [inst['symbol'] for inst in instruments if inst['symbol'].endswith("USDT")]
    except Exception as e:
        print(f"無法取得合約清單：{e}")
        return

    for symbol in usdt_pairs:
        try:
            df = get_klines(symbol)
            if len(df) < 60:
                continue

            ema12 = df['EMA12'].iloc[-1]
            ema30 = df['EMA30'].iloc[-1]
            ema55 = df['EMA55'].iloc[-1]
            close = df['close'].iloc[-1]
            low = df['low'].iloc[-1]
            high = df['high'].iloc[-1]

            candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
            signal_key = f"{symbol}-{candle_time}"

            # === 多頭排列 ===
            if ema12 > ema30 > ema55:
                cond = (df['EMA12'] > df['EMA30']) & (df['EMA30'] > df['EMA55'])
                up_df = df[cond]
                if not up_df.empty:
                    start_idx = up_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['low'] <= df_after['EMA55']).any()

                    if (low <= ema30 and low > ema55) and not touched_ema55:
                        if is_bullish_engulfing(df) and signal_key + "-bull" not in sent_signals:
                            msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[signal_key + "-bull"] = datetime.utcnow()

            # === 空頭排列 ===
            elif ema12 < ema30 < ema55:
                cond = (df['EMA12'] < df['EMA30']) & (df['EMA30'] < df['EMA55'])
                down_df = df[cond]
                if not down_df.empty:
                    start_idx = down_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['high'] >= df_after['EMA55']).any()

                    if (high >= ema30 and high < ema55) and not touched_ema55:
                        if is_bearish_engulfing(df) and signal_key + "-bear" not in sent_signals:
                            msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[signal_key + "-bear"] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")

# === Flask 網頁 ===
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
        <h1>🚀 EMA 吞沒策略伺服器運行中 (Bitunix)</h1>
    </body>
    </html>
    """)

@app.route('/ping')
def ping():
    return 'pong'

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# === 啟動伺服器與排程 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')  # 每小時的 2 分和 32 分執行
scheduler.start()

# 啟動即執行一次策略並發送啟動訊息
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
