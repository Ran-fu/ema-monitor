from flask import Flask
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

FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 抓取 Bitunix K 線 ===
def get_klines(symbol, size=100, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
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
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒判斷 ===
def is_bullish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return prev['close'] < prev['open'] and curr['close'] > curr['open'] and curr['close'] > prev['open'] and curr['open'] < prev['close']

def is_bearish_engulfing(df):
    prev, curr = df.iloc[-2], df.iloc[-1]
    return prev['close'] > prev['open'] and curr['close'] < curr['open'] and curr['close'] < prev['open'] and curr['open'] > prev['close']

# === 發送 Telegram ===
def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print(f"✅ Telegram 發送成功: {msg}")
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

# === 取得成交量 Top3 ===
def get_top3_volume():
    try:
        url = "https://api.bitunix.com/api/v1/market/tickers"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()['data']
        df = pd.DataFrame(data)
        df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0)
        df_usdt = df[df["symbol"].str.endswith("USDT")]
        return df_usdt.nlargest(3, "vol")["symbol"].tolist()
    except Exception as e:
        print("取得 Top3 成交量失敗:", e)
        return []

# === 檢查訊號 ===
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
            candle_time = df['ts'].iloc[-1].strftime('%Y-%m-%d %H:%M')
            key_base = f"{symbol}-{candle_time}"

            # 多頭排列
            if ema12 > ema30 > ema55 and low <= ema30 and low > ema55:
                if is_bullish_engulfing(df) and key_base + "-bull" not in sent_signals:
                    send_telegram_message(f"🟢 {symbol}\n看漲吞沒，收盤 {close:.4f} ({candle_time})")
                    sent_signals[key_base + "-bull"] = datetime.utcnow()

            # 空頭排列
            elif ema12 < ema30 < ema55 and high >= ema30 and high < ema55:
                if is_bearish_engulfing(df) and key_base + "-bear" not in sent_signals:
                    send_telegram_message(f"🔴 {symbol}\n看跌吞沒，收盤 {close:.4f} ({candle_time})")
                    sent_signals[key_base + "-bear"] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 發生錯誤: {e}")

# === Flask 網頁 ===
@app.route("/")
def home():
    return "🚀 EMA 吞沒策略伺服器運行中 (Bitunix)"

# === 啟動排程 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')
scheduler.start()

# 啟動立即檢查並發送啟動訊息
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
