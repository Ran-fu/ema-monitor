from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, os, json

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 狀態記錄 ===
sent_signals = {}
today_top3 = []
today_date = None
last_check_time = None
last_timezone_check = None
STATE_FILE = "state.json"

# === 狀態管理 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                sent_signals.update({k: datetime.fromisoformat(v) for k, v in data.get("sent_signals", {}).items()})
                td = data.get("today_date")
                if td:
                    today_date = datetime.fromisoformat(td).date()
            print("🧩 狀態已載入")
        except:
            print("⚠️ 狀態載入失敗")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "sent_signals": {k: v.isoformat() for k, v in sent_signals.items()},
                "today_date": str(today_date)
            }, f)
    except:
        print("⚠️ 狀態保存失敗")

# === Telegram 發送 ===
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
    keys_to_delete = [k for k, ts in sent_signals.items() if ts < cutoff]
    for k in keys_to_delete:
        del sent_signals[k]

# === 取得 K 線資料 ===
def get_klines(symbol, bar="30m", retries=3):
    url = f'https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT-SWAP&bar={bar}&limit=200'
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=10).json()
            data = resp.get('data', [])
            if not data:
                print(f"[{symbol}] 無資料")
                return pd.DataFrame()
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol','c1','c2','c3'])
            df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms').dt.tz_localize('UTC').dt.tz_convert('Asia/Taipei')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗: {e}")
            time.sleep(1)
    return pd.DataFrame()

# === 更新今日成交量 Top3 ===
def update_today_top3():
    global today_top3, today_date
    now_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if today_date != now_date:
        today_date = now_date
        try:
            url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
            resp = requests.get(url, timeout=10).json()
            tickers = resp.get('data', [])
            df_vol = pd.DataFrame(tickers)
            df_vol = df_vol[df_vol['instId'].str.endswith("USDT-SWAP")]
            df_vol['vol24h'] = pd.to_numeric(df_vol['vol24h'], errors='coerce')
            df_vol = df_vol.dropna(subset=['vol24h'])
            df_vol = df_vol.sort_values('vol24h', ascending=False)
            today_top3 = df_vol['instId'].head(3).str.replace("-USDT-SWAP","").tolist()
            print(f"📊 今日 Top3: {today_top3}")
        except Exception as e:
            print(f"⚠️ 更新 Top3 失敗: {e}")

# === 每日清空訊號 ===
def daily_reset():
    global sent_signals
    sent_signals.clear()
    print("🧹 每日訊號已清空")
    update_today_top3()
    save_state()
    send_telegram_message("🧹 今日訊號已清空，Top3 已更新")

# === 檢查吞沒訊號（以收盤K線為準） ===
def check_signals():
    global last_check_time
    cleanup_old_signals()
    update_today_top3()

    main_symbols = ["BTC","ETH","SOL","XRP"]
    watch_symbols = list(set(main_symbols + today_top3))

    for bar in ["15m", "30m"]:
        for symbol in watch_symbols:
            df = get_klines(symbol, bar=bar)
            if df.empty or len(df) < 60:
                continue

            # === 使用最後一根收盤K線判斷吞沒 ===
            prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
            open_, close_, high_, low_ = df['open'].iloc[-1], df['close'].iloc[-1], df['high'].iloc[-1], df['low'].iloc[-1]
            ema12, ema30, ema55 = df['EMA12'].iloc[-1], df['EMA30'].iloc[-1], df['EMA55'].iloc[-1]
            candle_time = df['ts'].iloc[-1].strftime('%Y-%m-%d %H:%M')
            bull_key = f"{symbol}-{bar}-{candle_time}-bull"
            bear_key = f"{symbol}-{bar}-{candle_time}-bear"
            is_top3 = symbol in today_top3

            # === 看漲吞沒（碰或跌破 EMA30 未碰 EMA55） ===
            if ema12 > ema30 > ema55 and (
                (low_ <= ema30 < high_ and low_ > ema55) or
                (low_ <= ema30 and close_ < ema30 and low_ > ema55)
            ) and prev_close < prev_open and close_ > open_ and close_ > prev_open and open_ < prev_close \
              and bull_key not in sent_signals:
                prefix = "🔥 Top3 " if is_top3 else "🟢"
                msg = f"{prefix}{symbol} [{bar}]\n看漲吞沒（收盤K線確認）\n碰或跌破 EMA30 未碰 EMA55\n收盤: {close_} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bull_key] = datetime.utcnow()

            # === 看跌吞沒（碰或突破 EMA30 未碰 EMA55） ===
            if ema12 < ema30 < ema55 and (
                (high_ >= ema30 > low_ and high_ < ema55) or
                (high_ >= ema30 and close_ > ema30 and high_ < ema55)
            ) and prev_close > prev_open and close_ < open_ and close_ < prev_open and open_ > prev_close \
              and bear_key not in sent_signals:
                prefix = "🔥 Top3 " if is_top3 else "🔴"
                msg = f"{prefix}{symbol} [{bar}]\n看跌吞沒（收盤K線確認）\n碰或突破 EMA30 未碰 EMA55\n收盤: {close_} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bear_key] = datetime.utcnow()

    last_check_time = datetime.utcnow()
    save_state()

# === 掉線偵測 ===
def check_health():
    global last_check_time
    now = datetime.utcnow()
    if last_check_time is None:
        last_check_time = now
        return
    diff = (now - last_check_time).total_seconds() / 60
    if diff > 60:
        send_telegram_message(f"⚠️ 系統可能掉線或延遲運行\n最後檢查時間：{last_check_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        last_check_time = now

# === 時區監測（台灣時間） ===
def check_timezone():
    global last_timezone_check
    taiwan_now = datetime.now(ZoneInfo("Asia/Taipei"))
    utc_now = datetime.utcnow()
    diff = abs((taiwan_now - (utc_now + timedelta(hours=8))).total_seconds()) / 60
    if diff > 5:
        send_telegram_message(f"⚠️ 時區異常偵測：與台灣時間偏差 {diff:.1f} 分鐘")
    last_timezone_check = taiwan_now
    print(f"🕓 時區檢查完成：{taiwan_now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")

# === Flask 頁面 ===
@app.route('/')
def home():
    top3_text = ", ".join(today_top3) if today_top3 else "尚未更新"
    return render_template_string(f"""
        <h1>🚀 OKX EMA 吞沒策略運行中 ✅</h1>
        <p>📊 今日 Top3：{top3_text}</p>
        <p>🕒 上次檢查時間：{last_check_time}</p>
        <p>🌏 最近時區檢查：{last_timezone_check}</p>
    """)

@app.route('/ping')
def ping():
    return 'pong', 200

# === 排程設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')
scheduler.add_job(check_signals, 'interval', minutes=15)
scheduler.add_job(check_health, 'interval', minutes=10)
scheduler.add_job(check_timezone, 'interval', minutes=15)
scheduler.add_job(daily_reset, 'cron', hour=0, minute=0)
scheduler.start()

# === 啟動立即執行 ===
load_state()
update_today_top3()
send_telegram_message("🚀 OKX EMA 吞沒監控已啟動 ✅\n(以收盤K線判斷吞沒)\n" + ("今日 Top3: " + ", ".join(today_top3) if today_top3 else "無 Top3"))
check_signals()
check_timezone()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
