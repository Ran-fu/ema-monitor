from flask import Flask
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
TELEGRAM_CHAT_ID = 1634751416

sent_signals = {}
today_top3 = []
today_date = None
STATE_FILE = "state.json"

# === 狀態保存與載入 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                sent_signals = {k: datetime.fromisoformat(v) for k, v in data.get("sent_signals", {}).items()}
                today_date = datetime.fromisoformat(data.get("today_date")).date()
                print("🧩 狀態已載入")
        except Exception as e:
            print(f"⚠️ 載入狀態失敗：{e}")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "sent_signals": {k: v.isoformat() for k, v in sent_signals.items()},
                "today_date": str(today_date)
            }, f)
    except Exception as e:
        print(f"⚠️ 保存狀態失敗：{e}")

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
        if not response.ok:
            print(f"Telegram 發送失敗: {response.text}")
        else:
            print(f"✅ 發送訊息：{text}")
    except Exception as e:
        print(f"❌ Telegram 發送異常：{e}")

# === 取得 K 線資料（OKX SWAP 合約） ===
def get_klines(symbol, retries=3):
    url = f'https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT-SWAP&bar=30m&limit=200'
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            data = response.json().get('data', [])
            if len(data) == 0:
                send_telegram_message(f"[{symbol}] ❌ 無法取得資料")
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','c1','c2','c3'])
            df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12).mean()
            df['EMA30'] = df['close'].ewm(span=30).mean()
            df['EMA55'] = df['close'].ewm(span=55).mean()
            time.sleep(0.5)  # 避免封鎖
            return df
        except Exception as e:
            send_telegram_message(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    send_telegram_message(f"[{symbol}] 🚫 多次抓取失敗，略過")
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

# === 更新每日成交量 Top3（SWAP） ===
def update_today_top3():
    global today_top3, today_date
    now_date = datetime.utcnow().date()
    if today_date != now_date:
        today_date = now_date
        try:
            url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=10).json()
            tickers = resp.get('data', [])
            df_vol = pd.DataFrame(tickers)
            df_vol = df_vol[df_vol['instId'].str.endswith("-USDT-SWAP")]
            df_vol['vol24h'] = pd.to_numeric(df_vol['vol24h'], errors='coerce')
            df_vol = df_vol.dropna(subset=['vol24h'])
            df_vol = df_vol.sort_values('vol24h', ascending=False)
            today_top3 = df_vol['instId'].head(3).str.replace("-USDT-SWAP", "").tolist()
            print(f"📊 今日 Top3: {', '.join(today_top3)}")
        except Exception as e:
            send_telegram_message(f"⚠️ 更新 Top3 失敗：{e}")

# === 訊號檢查邏輯 ===
def check_signals():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 開始檢查訊號...")
    cleanup_old_signals()
    update_today_top3()

    main_symbols = ["BTC","ETH","SOL","XRP"]
    watch_symbols = list(set(main_symbols + today_top3))

    for symbol in watch_symbols:
        try:
            df = get_klines(symbol)
            if df.empty or len(df) < 60:
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

            is_top3 = symbol in today_top3

            # 多頭訊號
            if is_up and bull_key not in sent_signals and is_bullish_engulfing(df):
                prefix = "📈 Top3 " if is_top3 else "🟢"
                msg = f"{prefix}{symbol}（SWAP）\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bull_key] = datetime.utcnow()

            # 空頭訊號
            if is_down and bear_key not in sent_signals and is_bearish_engulfing(df):
                prefix = "📈 Top3 " if is_top3 else "🔴"
                msg = f"{prefix}{symbol}（SWAP）\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bear_key] = datetime.utcnow()

        except Exception as e:
            send_telegram_message(f"[{symbol}] ❌ 策略錯誤：{e}")

    save_state()  # 保存狀態

# === Flask 路由 ===
@app.route('/')
def home():
    return "🚀 OKX SWAP EMA 吞沒策略伺服器運行中 ✅"

@app.route('/ping')
def ping():
    return 'pong'

# === 排程設定 ===
scheduler = BackgroundScheduler(timezone='Asia/Taipei')
scheduler.add_job(check_signals, 'cron', minute='2,32')

def send_startup_message():
    send_telegram_message("🚀 OKX SWAP EMA 吞沒監控已啟動 ✅")

scheduler.add_job(send_startup_message, 'date', run_date=datetime.utcnow() + timedelta(seconds=5))
scheduler.start()

# === 主程式 ===
if __name__ == '__main__':
    load_state()
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Flask server running on port {port}")
    check_signals()  # 啟動立即檢查一次
    app.run(host='0.0.0.0', port=port)
