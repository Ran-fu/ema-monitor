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

# === 取得 K 線資料（強化容錯 + 偽裝瀏覽器） ===
def get_klines(symbol, size=100, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"[{symbol}] HTTP 狀態碼 {r.status_code}")
                print("回傳內容：", r.text[:200])
                continue

            text = r.text.strip()
            if not text:
                print(f"[{symbol}] 回傳空白")
                continue

            try:
                data = r.json()
            except Exception as e:
                print(f"[{symbol}] JSON 解析失敗：{e}")
                print("原始內容：", text[:200])
                continue

            # --- 自動偵測多種格式 ---
            candles = None
            if isinstance(data, list):
                candles = data
            elif isinstance(data, dict):
                if 'data' in data and isinstance(data['data'], list):
                    candles = data['data']
                elif 'data' in data and isinstance(data['data'], dict) and 'list' in data['data']:
                    candles = data['data']['list']

            if not candles or not isinstance(candles, list) or len(candles) == 0:
                print(f"[{symbol}] 回傳結構異常或無資料: {data}")
                continue

            try:
                df = pd.DataFrame(candles)
                if len(df.columns) >= 6:
                    df = df.iloc[:, :6]
                    df.columns = ['ts', 'open', 'high', 'low', 'close', 'vol']
                else:
                    print(f"[{symbol}] 欄位數不足: {df.head(1)}")
                    continue

                df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
                df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0.0)
                df['ts'] = pd.to_datetime(df['ts'], unit='ms')
                df = df.iloc[::-1].reset_index(drop=True)
                df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
                df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
                df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
                return df
            except Exception as e:
                print(f"[{symbol}] DataFrame 建立失敗：{e}")
                continue

        except Exception as e:
            print(f"[{symbol}] 抓取異常：{e}")

        time.sleep(2)

    print(f"⚠️ {symbol} 多次抓取失敗，略過此幣種")
    return None

# === 取得 Top3 交易量 USDT 合約對（加 User-Agent） ===
def get_top3_volume_symbols():
    url = "https://api.bitunix.com/api/v1/market/tickers"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if not (isinstance(data, dict) and 'data' in data and isinstance(data['data'], list)):
            print("Top3 API 回傳格式不符：", data)
            return []
        df = pd.DataFrame(data['data'])
        df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0.0)
        df_usdt = df[df['symbol'].str.endswith("USDT")]
        top3 = df_usdt.nlargest(3, 'vol')['symbol'].tolist()
        return top3
    except Exception as e:
        print("取得 Top3 交易量幣種失敗：", e)
        return []

# === 監控幣種 ===
FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
def get_symbols_to_monitor():
    top3 = get_top3_volume_symbols()
    symbols = list(set(FIXED_SYMBOLS + top3))
    return symbols

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === Telegram 發訊 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            print("✅ Telegram 發送成功：", text)
        else:
            print("❌ Telegram 發送失敗：", r.text)
    except Exception as e:
        print("❌ Telegram 發送異常：", e)

# === 檢查 EMA + 吞沒訊號 ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
    cleanup_old_signals()
    symbols = get_symbols_to_monitor()
    for symbol in symbols:
        try:
            df = get_klines(symbol)
            if df is None or len(df) < 60:
                continue

            ema12 = df['EMA12'].iloc[-1]
            ema30 = df['EMA30'].iloc[-1]
            ema55 = df['EMA55'].iloc[-1]
            close = df['close'].iloc[-1]
            low = df['low'].iloc[-1]
            high = df['high'].iloc[-1]
            candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
            signal_key = f"{symbol}-{candle_time}"

            # 多頭排列
            if ema12 > ema30 > ema55 and low <= ema30 and low > ema55:
                if is_bullish_engulfing(df) and signal_key+"-bull" not in sent_signals:
                    msg = f"🟢 {symbol} 看漲吞沒 收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[signal_key+"-bull"] = datetime.utcnow()

            # 空頭排列
            elif ema12 < ema30 < ema55 and high >= ema30 and high < ema55:
                if is_bearish_engulfing(df) and signal_key+"-bear" not in sent_signals:
                    msg = f"🔴 {symbol} 看跌吞沒 收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[signal_key+"-bear"] = datetime.utcnow()
        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <h1>🚀 Bitunix EMA 吞沒監控伺服器運行中</h1>
    """)

@app.route('/ping')
def ping():
    return 'pong'

# === 排程任務 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')
scheduler.start()

# 啟動即檢查一次
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
