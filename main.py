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

# 已發送訊號記錄
sent_signals = {}

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 每日重置 sent_signals ===
def reset_daily_signals():
    sent_signals.clear()
    send_telegram_message("🔄 今日 sent_signals 已重置")
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] sent_signals 已清空")

# === 抓取 Bitunix 30m K 線資料 ===
def get_klines(symbol, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size=100'
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=10)
            data = response.json()['data']
            df = pd.DataFrame(data, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    print(f"[{symbol}] 多次抓取失敗")
    return None

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
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

    usdt_pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    df_dict = {}
    failed_symbols = []

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # 抓資料並計算今天成交量
    for symbol in usdt_pairs:
        df = get_klines(symbol)
        if df is None or len(df) < 3:
            failed_symbols.append(symbol)
            continue
        today_df = df[(df['ts'] >= today_start) & (df['ts'] < today_end)]
        if today_df.empty:
            failed_symbols.append(symbol)
            continue
        df_dict[symbol] = {'full_df': df, 'today_vol': today_df['vol'].sum()}

    if not df_dict:
        print("所有幣種抓取失敗或今日無資料，跳過本次檢查")
        return

    # 選出今日成交量前 3 名
    top3_symbols = sorted(df_dict.keys(), key=lambda s: df_dict[s]['today_vol'], reverse=True)[:3]
    print("當日成交量Top3幣種:", top3_symbols)

    for symbol in top3_symbols:
        df = df_dict[symbol]['full_df']
        try:
            ema12 = df['EMA12'].iloc[-2]
            ema30 = df['EMA30'].iloc[-2]
            ema55 = df['EMA55'].iloc[-2]
            close = df['close'].iloc[-2]
            low = df['low'].iloc[-2]
            high = df['high'].iloc[-2]

            is_up = ema12 > ema30 > ema55
            is_down = ema12 < ema30 < ema55
            candle_time = df['ts'].iloc[-2].floor('30T').strftime('%Y-%m-%d %H:%M')
            signal_key = f"{symbol}-{candle_time}"

            # 多頭排列
            if is_up:
                up_df = df[df['EMA12'] > df['EMA30']]
                up_df = up_df[up_df['EMA30'] > up_df['EMA55']]
                if not up_df.empty:
                    touched_ema55 = (up_df['low'] <= up_df['EMA55']).any()
                    if (low <= ema30 and low > ema55) and not touched_ema55:
                        if is_bullish_engulfing(df) and signal_key + "-bull" not in sent_signals:
                            msg = (f"🟢 {symbol}\n看漲吞沒\n收盤：{close:.4f}\n"
                                   f"EMA12:{ema12:.2f} EMA30:{ema30:.2f} EMA55:{ema55:.2f}\n"
                                   f"時間：{candle_time}")
                            send_telegram_message(msg)
                            sent_signals[signal_key + "-bull"] = datetime.utcnow()

            # 空頭排列
            elif is_down:
                down_df = df[df['EMA12'] < df['EMA30']]
                down_df = down_df[down_df['EMA30'] < df['EMA55']]
                if not down_df.empty:
                    touched_ema55 = (down_df['high'] >= down_df['EMA55']).any()
                    if (high >= ema30 and high < ema55) and not touched_ema55:
                        if is_bearish_engulfing(df) and signal_key + "-bear" not in sent_signals:
                            msg = (f"🔴 {symbol}\n看跌吞沒\n收盤：{close:.4f}\n"
                                   f"EMA12:{ema12:.2f} EMA30:{ema30:.2f} EMA55:{ema55:.2f}\n"
                                   f"時間：{candle_time}")
                            send_telegram_message(msg)
                            sent_signals[signal_key + "-bear"] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")
            failed_symbols.append(symbol)

    if failed_symbols:
        print("抓取失敗交易對:", failed_symbols)

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>EMA 吞沒策略</title>
    </head>
    <body>
        <h1>🚀 EMA 吞沒策略伺服器運行中 (Bitunix)</h1>
    </body>
    </html>
    """)

@app.route('/ping')
def ping():
    return 'pong'

# === 排程 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')          # 每小時 2 分與 32 分
scheduler.add_job(reset_daily_signals, 'cron', hour=0, minute=1) # 每天 00:01 重置 sent_signals
scheduler.start()

# 啟動即執行一次策略並發送啟動訊息
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
