from flask import Flask, render_template_string, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import json

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 已發送過的訊號記錄（包含時間）
sent_signals = {}
today_top3 = []
today_date = None
STATE_FILE = "state.json"

# === 狀態管理 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                sent_signals.update({k: datetime.fromisoformat(v) for k,v in data.get("sent_signals", {}).items()})
                td = data.get("today_date")
                if td:
                    global today_date
                    today_date = datetime.fromisoformat(td).date()
            print("🧩 狀態已載入")
        except:
            print("⚠️ 狀態載入失敗")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "sent_signals": {k: v.isoformat() for k,v in sent_signals.items()},
                "today_date": str(today_date)
            }, f)
    except:
        print("⚠️ 狀態保存失敗")

# === Telegram 發送訊息 ===
def send_telegram_message(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.ok:
            print(f"✅ 發送訊息: {text}")
        else:
            print(f"❌ Telegram 發送失敗: {r.text}")
    except Exception as e:
        print(f"❌ Telegram 發送異常: {e}")

# === 清理舊訊號 ===
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 取得 Bitunix K 線資料 ===
def get_klines(symbol, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size=100'
    for _ in range(retries):
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()['data']
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗: {e}")
            time.sleep(1)
    print(f"[{symbol}] 多次抓取失敗，略過")
    return pd.DataFrame()

# === 吞沒判斷 ===
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === 更新今日 Top3 ===
def update_today_top3():
    global today_top3, today_date
    now_date = datetime.utcnow().date()
    if today_date != now_date:
        today_date = now_date
        try:
            url = "https://api.bitunix.com/api/v1/market/tickers"
            resp = requests.get(url, timeout=10).json()
            tickers = resp.get('data', [])
            df_vol = pd.DataFrame(tickers)
            df_vol = df_vol[df_vol['symbol'].str.endswith("USDT")]
            df_vol['vol24h'] = pd.to_numeric(df_vol['vol24h'], errors='coerce')
            df_vol = df_vol.dropna(subset=['vol24h'])
            df_vol = df_vol.sort_values('vol24h', ascending=False)
            today_top3 = df_vol['symbol'].head(3).tolist()
            print(f"📊 今日 Top3: {today_top3}")
        except Exception as e:
            print(f"⚠️ 更新 Top3 失敗: {e}")

# === 每日零點清空訊號 ===
def daily_reset():
    global sent_signals
    sent_signals.clear()
    print("🧹 每日訊號已清空")
    update_today_top3()
    save_state()
    send_telegram_message("🧹 今日訊號已清空，Top3 已更新")

# === 檢查訊號 ===
def check_signals():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
    cleanup_old_signals()
    update_today_top3()

    main_symbols = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]
    watch_symbols = list(set(main_symbols + today_top3))

    for symbol in watch_symbols:
        df = get_klines(symbol)
        if df.empty or len(df) < 60:
            continue

        ema12, ema30, ema55 = df['EMA12'].iloc[-1], df['EMA30'].iloc[-1], df['EMA55'].iloc[-1]
        close = df['close'].iloc[-1]
        candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
        bull_key = f"{symbol}-{candle_time}-bull"
        bear_key = f"{symbol}-{candle_time}-bear"
        is_top3 = symbol in today_top3

        # 多頭訊號
        if ema12 > ema30 > ema55 and is_bullish_engulfing(df) and bull_key not in sent_signals:
            prefix = "📈 Top3 " if is_top3 else "🟢"
            msg = f"{prefix}{symbol}\n看漲吞沒，收盤: {close} ({candle_time})"
            send_telegram_message(msg)
            sent_signals[bull_key] = datetime.utcnow()

        # 空頭訊號
        if ema12 < ema30 < ema55 and is_bearish_engulfing(df) and bear_key not in sent_signals:
            prefix = "📈 Top3 " if is_top3 else "🔴"
            msg = f"{prefix}{symbol}\n看跌吞沒，收盤: {close} ({candle_time})"
            send_telegram_message(msg)
            sent_signals[bear_key] = datetime.utcnow()

    save_state()

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("<h1>🚀 Bitunix EMA 吞沒策略運行中 ✅</h1>")

@app.route('/ping')
def ping():
    return 'pong'

# === 排程設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')      # 每小時 2/32 分檢查
scheduler.add_job(daily_reset, 'cron', hour=0, minute=0)      # 每日零點清空訊號 & 更新 Top3
scheduler.start()

# === 啟動訊息 ===
load_state()
update_today_top3()
check_signals()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動 ✅")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
