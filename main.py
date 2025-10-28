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

# === 已發送訊號紀錄 ===
sent_signals = {}

def cleanup_old_signals(hours=6):
    """清理超過指定時間的已發送訊號"""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]
    if keys_to_delete:
        print(f"🧹 已清理 {len(keys_to_delete)} 條過期訊號")

def send_telegram_message(text):
    """發送 Telegram 訊息"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.ok:
            print(f"Telegram 發送成功: {text}")
        else:
            print(f"Telegram 發送失敗: {response.text}")
    except Exception as e:
        print(f"Telegram 發送異常：{e}")

def get_klines(symbol, retries=3):
    """從 OKX 取得 K 線資料"""
    url = f'https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT&bar=30m&limit=200'
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = response.json().get('data', [])
            if len(data) == 0:
                print(f"[{symbol}] 無法取得資料")
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
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    print(f"[{symbol}] 多次抓取失敗，跳過")
    return pd.DataFrame()

def is_bullish_engulfing(df):
    """判斷看漲吞沒"""
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    """判斷看跌吞沒"""
    prev_open, prev_close = df['open'].iloc[-3], df['close'].iloc[-3]
    last_open, last_close = df['open'].iloc[-2], df['close'].iloc[-2]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

def check_signals():
    """主要策略檢查邏輯"""
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
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
    except Exception as e:
        print(f"取得 Top3 交易量失敗: {e}")

    watch_symbols = list(set(main_symbols + top3_symbols))

    # 檢查每個幣種的策略條件
    for symbol in watch_symbols:
        try:
            df = get_klines(symbol)
            if df.empty or len(df) < 60:
                print(f"[{symbol}] 資料不足，跳過策略判斷")
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

            # === 多頭訊號 ===
            if is_up and bull_key not in sent_signals:
                cond_up = (df['EMA12'] > df['EMA30']) & (df['EMA30'] > df['EMA55'])
                up_df = df[cond_up].reset_index(drop=True)
                if not up_df.empty and not (up_df['low'] <= up_df['EMA55']).any():
                    if is_bullish_engulfing(df):
                        msg = f"🟢 {symbol}\n看漲吞沒\n收盤：{close:.4f}\n時間（台灣）：{(datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')}"
                        send_telegram_message(msg)
                        sent_signals[bull_key] = datetime.utcnow()

            # === 空頭訊號 ===
            if is_down and bear_key not in sent_signals:
                cond_down = (df['EMA12'] < df['EMA30']) & (df['EMA30'] < df['EMA55'])
                down_df = df[cond_down].reset_index(drop=True)
                if not down_df.empty and not (down_df['high'] >= down_df['EMA55']).any():
                    if is_bearish_engulfing(df):
                        msg = f"🔴 {symbol}\n看跌吞沒\n收盤：{close:.4f}\n時間（台灣）：{(datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')}"
                        send_telegram_message(msg)
                        sent_signals[bear_key] = datetime.utcnow()

        except Exception as e:
            print(f"[{symbol}] 判斷策略錯誤：{e}")
            send_telegram_message(f"[{symbol}] 策略錯誤：{e}")

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head><meta charset="UTF-8"><title>OKX EMA 吞沒策略</title></head>
    <body><h1>🚀 OKX EMA 吞沒策略伺服器運行中</h1></body>
    </html>
    """)

@app.route('/ping')
def ping():
    return 'pong'

# === 排程設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')  # 每 30 分收盤後 2 分執行
scheduler.start()

# === 啟動時立即通知 + 執行一次 ===
if __name__ == '__main__':
    taipei_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    send_telegram_message(f"✅ Render 伺服器已啟動\n📅 台灣時間：{taipei_time}")
    check_signals()  # 啟動時立即跑一次
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
