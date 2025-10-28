from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = 1634751416  # 整數型態 Chat ID

sent_signals = {}

# === 清理舊訊號 ===
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === Telegram 發送 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"[Telegram] 回傳：{response.status_code}")
        if not response.ok:
            print(f"Telegram 發送失敗: {response.text}")
        else:
            print(f"✅ 已發送訊息：{text}")
    except Exception as e:
        print(f"❌ Telegram 發送異常：{e}")

# === 取得 K 線資料 ===
def get_klines(symbol, retries=3):
    url = f'https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT&bar=30m&limit=200'
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = response.json().get('data', [])
            if len(data) == 0:
                print(f"[{symbol}] ❌ 無法取得資料")
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','c1','c2','c3'])
            df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()
            print(f"[{symbol}] ✅ 成功取得 {len(df)} 筆 K 線資料")
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    print(f"[{symbol}] 🚫 多次抓取失敗，跳過")
    return pd.DataFrame()

# === 吞沒判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === 主要策略檢查 ===
def check_signals():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 開始檢查訊號...")
    cleanup_old_signals()

    main_symbols = ["BTC", "ETH", "SOL", "XRP"]
    top3_symbols = []

    # 取得當日成交量前 3 名
    try:
        url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10).json()
        tickers = resp.get('data', [])
        df_vol = pd.DataFrame(tickers)
        df_vol = df_vol[df_vol['instId'].str.endswith("USDT")]
        df_vol['vol24h'] = df_vol['vol24h'].astype(float)
        df_vol = df_vol.sort_values('vol24h', ascending=False)
        top3_symbols = df_vol['instId'].head(3).str.replace("-USDT", "").tolist()
        print(f"📊 今日成交量 Top3: {', '.join(top3_symbols)}")
    except Exception as e:
        print(f"⚠️ 取得 Top3 失敗: {e}")

    watch_symbols = list(set(main_symbols + top3_symbols))

    for symbol in watch_symbols:
        try:
            print(f"🪙 正在檢查：{symbol}")
            df = get_klines(symbol)
            if df.empty or len(df) < 60:
                print(f"[{symbol}] ⚠️ 資料不足，跳過")
                continue

            ema12 = df['EMA12'].iloc[-2]
            ema30 = df['EMA30'].iloc[-2]
            ema55 = df['EMA55'].iloc[-2]
            close = df['close'].iloc[-2]
            candle_time = df['ts'].iloc[-2].floor('30T').strftime('%Y-%m-%d %H:%M')

            is_up = ema12 > ema30 > ema55
            is_down = ema12 < ema30 < ema55

            bull_key = f"{symbol}-{candle_time}-bull"
            bear_key = f"{symbol}-{candle_time}-bear"

            # 多頭訊號
            if is_up and bull_key not in sent_signals:
                if is_bullish_engulfing(df):
                    msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[bull_key] = datetime.utcnow()
                    print(f"[{symbol}] ✅ 發出多頭訊號")

            # 空頭訊號
            if is_down and bear_key not in sent_signals:
                if is_bearish_engulfing(df):
                    msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                    send_telegram_message(msg)
                    sent_signals[bear_key] = datetime.utcnow()
                    print(f"[{symbol}] ✅ 發出空頭訊號")

        except Exception as e:
            print(f"[{symbol}] ❌ 策略錯誤：{e}")

# === Flask 首頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <html><head><meta charset="UTF-8"><title>OKX EMA 吞沒策略</title></head>
    <body><h1>🚀 OKX EMA 吞沒策略伺服器運行中</h1></body></html>
    """)

@app.route('/ping')
def ping():
    return 'pong'

# === 排程設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')

def send_startup_message():
    send_telegram_message("🚀 OKX EMA 吞沒監控已啟動 ✅ 測試訊息")

scheduler.add_job(send_startup_message, 'date', run_date=datetime.utcnow() + timedelta(seconds=5))
scheduler.start()

# === 主程式 ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))  # ✅ 修正 Render port
    print(f"🌐 Flask server running on port {port}")
    check_signals()  # ✅ 啟動時立即檢查一次
    app.run(host='0.0.0.0', port=port)
