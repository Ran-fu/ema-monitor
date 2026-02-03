from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import json

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1634751416")

# ===== 狀態紀錄 =====
sent_signals = {}  # 避免重複發送

# ===== 系統心跳 =====
def ping_system():
    send_telegram_message("✅ 系統在線中")

# ===== Telegram 發送 =====
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram 發送失敗:", e)

# ===== 取得全 USDT 永續合約幣種 =====
def fetch_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    res = requests.get(url, timeout=10)
    data = res.json()
    symbols = []
    if 'data' in data:
        for d in data['data']:
            instId = d['instId']
            if instId.endswith("-USDT-SWAP"):
                symbol = instId.replace("-USDT-SWAP", "")
                symbols.append(symbol)
    return symbols

# ===== 取得 K 線資料 =====
def fetch_klines(symbol, interval='30m', limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT-SWAP&bar={interval}&limit={limit}"
    res = requests.get(url, timeout=10)
    data = res.json()
    if 'data' in data:
        df = pd.DataFrame(data['data'], columns=['ts','o','h','l','c','vol','other1','other2','other3'])
        df['c'] = df['c'].astype(float)
        df['o'] = df['o'].astype(float)
        df['h'] = df['h'].astype(float)
        df['l'] = df['l'].astype(float)
        df['vol'] = df['vol'].astype(float)
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('ts', inplace=True)
        return df
    return None

# ===== EMA 計算 =====
def add_ema(df):
    df['EMA12'] = df['c'].ewm(span=12, adjust=False).mean()
    df['EMA30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['EMA55'] = df['c'].ewm(span=55, adjust=False).mean()
    return df

# ===== 吞沒形態判斷（完整性加強） =====
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    # 完整包住前一根實體且實體 >= 1.1倍前一根
    return curr['c'] > curr['o'] and prev['c'] < prev['o'] and curr['c'] > prev['o'] and curr['o'] < prev['c'] and (curr['c']-curr['o']) >= 1.1*(prev['c']-prev['o'])

def is_bearish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return curr['c'] < curr['o'] and prev['c'] > prev['o'] and curr['c'] < prev['o'] and curr['o'] > prev['c'] and (prev['c']-prev['o'])*1.1 <= (curr['o']-curr['c'])

# ===== 多時間週期 EMA 判斷 =====
def higher_tf_trend(symbol, interval='4h'):
    df = fetch_klines(symbol, interval=interval, limit=100)
    if df is None or len(df) < 60:
        return None
    df = add_ema(df)
    last = df.iloc[-1]
    if last['EMA12'] > last['EMA30'] > last['EMA55']:
        return '多頭'
    elif last['EMA12'] < last['EMA30'] < last['EMA55']:
        return '空頭'
    return None

# ===== 判斷進場訊號 =====
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df) < 60:
        return

    df = add_ema(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # EMA 多空排列 + 回踩 EMA30 且未碰 EMA55
    bullish_base = last['EMA12'] > last['EMA30'] > last['EMA55'] and last['c'] >= last['EMA30'] and last['c'] > last['EMA55']
    bearish_base = last['EMA12'] < last['EMA30'] < last['EMA55'] and last['c'] <= last['EMA30'] and last['c'] < last['EMA55']

    # EMA30 斜率過濾（趨勢強度）
    ema30_slope = (last['EMA30'] - prev['EMA30']) / prev['EMA30']
    bullish_slope_ok = bullish_base and ema30_slope > 0.001  # >0.1%
    bearish_slope_ok = bearish_base and ema30_slope < -0.001  # <-0.1%

    # 吞沒形態
    bullish_engulf = bullish_slope_ok and is_bullish_engulfing(df)
    bearish_engulf = bearish_slope_ok and is_bearish_engulfing(df)

    # 多時間週期共振
    trend_h4 = higher_tf_trend(symbol)
    if trend_h4 is None:
        return
    bullish_final = bullish_engulf and trend_h4 == '多頭'
    bearish_final = bearish_engulf and trend_h4 == '空頭'

    signal = None
    if bullish_final:
        signal = '多頭'
    elif bearish_final:
        signal = '空頭'

    if signal:
        key = f"{symbol}_{last.name}"
        if key in sent_signals:
            return  # 避免重複發送
        sent_signals[key] = True

        entry = last['c']
        stoploss = last['EMA55']
        distance = abs(entry - stoploss)
        takeprofit_1 = entry + distance if signal == '多頭' else entry - distance
        takeprofit_15 = entry + distance*1.5 if signal == '多頭' else entry - distance*1.5

        msg = (
            f"📊 {symbol} {signal}訊號\n"
            f"進場價: {entry:.2f}\n"
            f"止損(EMA55): {stoploss:.2f}\n"
            f"止盈1:1: {takeprofit_1:.2f}\n"
            f"止盈1:1.5: {takeprofit_15:.2f}\n"
            f"條件: EMA多空排列 + EMA30回踩 + 完整吞沒 + EMA30斜率 + H4共振"
        )
        send_telegram_message(msg)

# ===== 定時排程 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(lambda: [check_signal(s) for s in fetch_symbols()], 'cron', minute='2')  # 每30分K收盤後2分鐘
scheduler.add_job(ping_system, 'interval', minutes=60)  # 系統心跳
scheduler.start()

@app.route('/')
def home():
    return "OKX EMA 全幣種升級策略監控系統在線中 ✅"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
