from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

# ===== 狀態紀錄 =====
sent_signals = {}  # 避免重複發送

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
    try:
        res = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP", timeout=10)
        data = res.json()
        symbols = []
        for d in data.get('data', []):
            instId = d['instId']
            if instId.endswith("-USDT-SWAP"):
                symbols.append(instId.replace("-USDT-SWAP", ""))
        return symbols
    except:
        return []

# ===== 取得 K 線資料（安全處理） =====
def fetch_klines(symbol, interval='30m', limit=100):
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT-SWAP&bar={interval}&limit={limit}", timeout=10)
        data = res.json()
        if 'data' not in data or not data['data']:
            return None
        df = pd.DataFrame(data['data'], columns=['ts','o','h','l','c','vol','other1','other2','other3'])
        df[['o','h','l','c','vol']] = df[['o','h','l','c','vol']].astype(float)
        df['ts'] = df['ts'].apply(lambda x: pd.to_datetime(int(float(x)), unit='ms', errors='coerce'))
        df = df.dropna(subset=['ts'])
        df.set_index('ts', inplace=True)
        return df
    except Exception as e:
        print(f"fetch_klines {symbol} 錯誤:", e)
        return None

# ===== EMA 計算 =====
def add_ema(df):
    df['EMA12'] = df['c'].ewm(span=12, adjust=False).mean()
    df['EMA30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['EMA55'] = df['c'].ewm(span=55, adjust=False).mean()
    return df

# ===== 吞沒形態判斷 =====
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
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

    bullish_base = last['EMA12'] > last['EMA30'] > last['EMA55'] and last['c'] >= last['EMA30'] and last['c'] > last['EMA55']
    bearish_base = last['EMA12'] < last['EMA30'] < last['EMA55'] and last['c'] <= last['EMA30'] and last['c'] < last['EMA55']

    ema30_slope = (last['EMA30'] - prev['EMA30']) / prev['EMA30']
    bullish_slope_ok = bullish_base and ema30_slope > 0.001
    bearish_slope_ok = bearish_base and ema30_slope < -0.001

    bullish_engulf = bullish_slope_ok and is_bullish_engulfing(df)
    bearish_engulf = bearish_slope_ok and is_bearish_engulfing(df)

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
            return
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

# ===== 系統自動 Ping（啟動 + 每小時） =====
def ping_system():
    symbols = fetch_symbols()
    count = len(symbols)
    now = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    send_telegram_message(f"✅ 系統在線中\n時間: {now}\n監控幣種數量: {count}")

# ===== 定時排程 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(lambda: [check_signal(s) for s in fetch_symbols()], 'cron', minute='2')
scheduler.add_job(ping_system, 'interval', minutes=60)
scheduler.start()

# ===== 啟動立即 Ping =====
ping_system()

@app.route('/')
def home():
    return "OKX EMA 全幣種升級策略監控系統在線中 ✅"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
