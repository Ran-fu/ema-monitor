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

# 清理舊訊號
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === Telegram 發送訊息 ===
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

# === 抓取 OKX K 線資料 ===
def get_klines(symbol, retries=3):
    url = f'https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT&bar=30m&limit=200'
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = response.json().get('data', [])
            if len(data) == 0:
                msg = f"[{symbol}] 無法取得資料"
                print(msg)
                send_telegram_message(msg)
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','c1','c2','c3'])
            df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()
            return df
        except Exception as e:
            msg = f"[{symbol}] 抓取失敗：{e}"
            print(msg)
            send_telegram_message(msg)
            time.sleep(1)
    msg = f"[{symbol}] 多次抓取失敗，跳過"
    print(msg)
    send_telegram_message(msg)
    return pd.DataFrame()

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === EMA 策略判斷（含訊號去重）===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
    cleanup_old_signals()

    main_symbols = ["BTC","ETH","SOL","XRP"]

    # 取得當日成交量 Top3
    top3_symbols = []
    try:
        url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10).json()
        tickers = resp.get('data', [])
        df_vol = pd.DataFrame(tickers)
        df_vol = df_vol[df_vol['instId'].str.endswith("USDT")]
        df_vol['vol24h'] = df_vol['vol24h'].astype(float)
        df_vol = df_vol.sort_values('vol24h', ascending=False)
        top3_symbols = df_vol['instId'].head(3).str.replace("-USDT","").tolist()
    except Exception as e:
        msg = f"取得 Top3 交易量失敗: {e}"
        print(msg)
        send_telegram_message(msg)

    watch_symbols = list(set(main_symbols + top3_symbols))

    for symbol in watch_symbols:
        try:
            df = get_klines(symbol)
            if df.empty or len(df) < 60:
                msg = f"[{symbol}] 資料不足，跳過策略判斷"
                print(msg)
                send_telegram_message(msg)
                continue

            ema12 = df['EMA12'].iloc[-2]
            ema30 = df['EMA30'].iloc[-2]
            ema55 = df['EMA55'].iloc[-2]
            close = df['close'].iloc[-2]
            low = df['low'].iloc[-2]
            high = df['high'].iloc[-2]

            is_up = ema12 > ema30 > ema55
            is_down = ema12 < ema30 < ema55
            candle_time = df['ts'].iloc[-2].floor('30T').strftime('%Y-%m-%d %H:%M')

            # 訊號去重 key
            bull_key = f"{symbol}-{candle_time}-bull"
            bear_key = f"{symbol}-{candle_time}-bear"

            # 多頭訊號
            if is_up and bull_key not in sent_signals:
                up_df = df[df['EMA12'] > df['EMA30']]
                up_df = up_df[up_df['EMA30'] > up_df['EMA55']]
                if not up_df.empty:
                    touched_ema55 = (up_df['low'] <= up_df['EMA55']).any()
                    if (low <= ema30 and low > ema55) and not touched_ema55:
                        if is_bullish_engulfing(df):
                            msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[bull_key] = datetime.utcnow()

            # 空頭訊號
            if is_down and bear_key not in sent_signals:
                down_df = df[df['EMA12'] < df['EMA30']]
                down_df = down_df[down_df['EMA30'] < df['EMA55']]
                if not down_df.empty:
                    touched_ema55 = (down_df['high'] >= down_df['EMA55']).any()
                    if (high >= ema30 and high < ema55) and not touched_ema55:
                        if is_bearish_engulfing(df):
                            msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[bear_key] = datetime.utcnow()

        except Exception as e:
            msg = f"[{symbol}] 判斷策略錯誤：{e}"
            print(msg)
            send_telegram_message(msg)

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>OKX EMA 吞沒策略</title>
    </head>
    <body>
        <h1>🚀 OKX EMA 吞沒策略伺服器運行中</h1>
