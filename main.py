from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import json

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

# ===== 狀態紀錄 =====
STATE_FILE = "state.json"
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r") as f:
        sent_signals = json.load(f)
else:
    sent_signals = {}

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(sent_signals, f, indent=2, default=str)

# ===== Telegram 發送 =====
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode":"Markdown"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram 發送失敗:", e)

# ===== 取得全 USDT 永續合約幣種 =====
def fetch_symbols():
    try:
        res = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP", timeout=10)
        data = res.json()
        symbols = [d['instId'].replace("-USDT-SWAP","") 
                   for d in data.get('data',[]) if d['instId'].endswith("-USDT-SWAP")]
        return symbols
    except:
        return []

# ===== 取得 K 線資料 =====
def fetch_klines(symbol, interval='30m', limit=100):
    try:
        res = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={symbol}-USDT-SWAP&bar={interval}&limit={limit}", timeout=10)
        data = res.json().get('data', [])
        if not data: return None
        df = pd.DataFrame(data, columns=['ts','o','h','l','c','vol','other1','other2','other3'])
        df[['o','h','l','c','vol']] = df[['o','h','l','c','vol']].astype(float)
        df['ts'] = pd.to_datetime(df['ts'].astype(float), unit='ms', errors='coerce')
        df.dropna(subset=['ts'], inplace=True)
        df.set_index('ts', inplace=True)
        return df
    except Exception as e:
        print(f"{symbol} K線抓取錯誤:", e)
        return None

# ===== EMA 計算 =====
def add_ema(df):
    df['EMA12'] = df['c'].ewm(span=12, adjust=False).mean()
    df['EMA30'] = df['c'].ewm(span=30, adjust=False).mean()
    df['EMA55'] = df['c'].ewm(span=55, adjust=False).mean()
    return df

# ===== 吞沒形態判斷 =====
def is_bullish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return curr['c'] > curr['o'] and prev['c'] < prev['o'] and curr['c'] >= prev['o'] and curr['o'] <= prev['c']

def is_bearish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return curr['c'] < curr['o'] and prev['c'] > prev['o'] and curr['c'] <= prev['o'] and curr['o'] >= prev['c']

# ===== 判斷進場訊號（核心策略） =====
def check_signal(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df)<60: 
        return
    df = add_ema(df)
    last = df.iloc[-1]

    # 多單條件：EMA 多頭 + 回踩 EMA30 未碰 EMA55 + 看漲吞沒
    bull_trend = last['EMA12'] > last['EMA30'] > last['EMA55']
    bull_pullback = last['l'] <= last['EMA30'] and last['l'] > last['EMA55']
    bull_engulf = is_bullish_engulfing(df)
    bullish_signal = bull_trend and bull_pullback and bull_engulf

    # 空單條件：EMA 空頭 + 回踩 EMA30 未碰 EMA55 + 看跌吞沒
    bear_trend = last['EMA12'] < last['EMA30'] < last['EMA55']
    bear_pullback = last['h'] >= last['EMA30'] and last['h'] < last['EMA55']
    bear_engulf = is_bearish_engulfing(df)
    bearish_signal = bear_trend and bear_pullback and bear_engulf

    signal = None
    if bullish_signal: signal = '多頭'
    elif bearish_signal: signal = '空頭'

    if signal:
        key = f"{symbol}_{last.name}"
        if key in sent_signals: 
            return
        sent_signals[key] = True
        save_state()

        entry = last['c']
        stoploss = last['EMA55']
        risk = abs(entry - stoploss)
        tp1 = entry + risk if signal=='多頭' else entry - risk
        tp2 = entry + risk*1.5 if signal=='多頭' else entry - risk*1.5

        msg = (
            f"📊 {symbol} {signal}訊號\n"
            f"進場價: {entry:.2f}\n止損(EMA55): {stoploss:.2f}\n"
            f"止盈1:1: {tp1:.2f}\n止盈1:1.5: {tp2:.2f}\n"
            f"條件: EMA多空排列 + EMA30回踩 + 完整吞沒"
        )
        send_telegram_message(msg)

# ===== 系統自動 Ping =====
def ping_system():
    symbols = fetch_symbols()
    send_telegram_message(f"✅ 系統在線中\n監控幣種數量: {len(symbols)}")

# ===== 排程 =====
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(lambda: [check_signal(s) for s in fetch_symbols()], 'cron', minute='2')  # 每30分K收盤後2分鐘
scheduler.add_job(ping_system, 'interval', minutes=60)  # 每小時自動 Ping
scheduler.start()

# ===== 啟動立即 Ping =====
ping_system()

@app.route('/')
def home():
    return "OKX EMA 全幣種升級策略監控系統在線中 ✅"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)))
