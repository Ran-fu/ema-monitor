from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import random

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 監控幣種設定 ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# === EMA 參數 ===
EMA_FAST = 12
EMA_MID = 30
EMA_SLOW = 55

# 記錄已發送訊號，避免重複
sent_signals = set()

# ====== Bitunix K 線抓取 ======
def fetch_bitunix_klines(symbol, interval='30m', limit=100, retries=3):
    url = f"https://bitunix.com/api/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"[{symbol}] HTTP {resp.status_code}，第 {attempt+1} 次重試")
        except Exception as e:
            print(f"[{symbol}] 抓取例外：{e}，第 {attempt+1} 次重試")
        time.sleep(random.uniform(1, 3))
    print(f"[{symbol}] 多次抓取失敗，略過此幣種")
    return None

# ====== 計算 EMA ======
def calculate_ema(df, period, column='close'):
    return df[column].ewm(span=period, adjust=False).mean()

# ====== 判斷吞沒形態 ======
def check_engulfing(df):
    """
    回傳最後一根 K 線是否為看漲或看跌吞沒
    """
    if len(df) < 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 看漲吞沒
    if last['close'] > last['open'] and prev['close'] < prev['open']:
        if last['open'] < prev['close'] and last['close'] > prev['open']:
            return "看漲吞沒"
    # 看跌吞沒
    if last['close'] < last['open'] and prev['close'] > prev['open']:
        if last['open'] > prev['close'] and last['close'] < prev['open']:
            return "看跌吞沒"
    return None

# ====== 發送 Telegram 訊息 ======
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram 發送失敗：{e}")

# ====== EMA 吞沒策略判斷 ======
def ema_engulfing_check():
    global sent_signals
    top_volume = []

    for symbol in SYMBOLS:
        data = fetch_bitunix_klines(symbol)
        if not data:
            continue

        df = pd.DataFrame(data, columns=[
            'open_time','open','high','low','close','volume','close_time',
            'quote_asset_volume','num_trades'
        ])
        # 轉數字
        for col in ['open','high','low','close','volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 計算 EMA
        df['ema_fast'] = calculate_ema(df, EMA_FAST)
        df['ema_mid'] = calculate_ema(df, EMA_MID)
        df['ema_slow'] = calculate_ema(df, EMA_SLOW)

        # 多空排列判斷
        last = df.iloc[-1]
        if last['ema_fast'] > last['ema_mid'] > last['ema_slow']:
            trend = "多頭排列"
        elif last['ema_fast'] < last['ema_mid'] < last['ema_slow']:
            trend = "空頭排列"
        else:
            trend = "無明確排列"

        # 吞沒形態
        engulfing = check_engulfing(df)
        if engulfing:
            signal_key = f"{symbol}-{engulfing}-{last.name}"
            if signal_key not in sent_signals:
                msg = f"{symbol}\n趨勢：{trend}\n訊號：{engulfing}\n收盤價：{last['close']:.4f}"
                send_telegram(msg)
                sent_signals.add(signal_key)

        # 儲存交易量
        top_volume.append((symbol, last['volume']))

    # 每日交易量 Top3
    top_volume.sort(key=lambda x: x[1], reverse=True)
    top3_symbols = [s[0] for s in top_volume[:3]]
    print(f"[{datetime.now()}] 今日交易量 Top3：{top3_symbols}")

# ====== APScheduler 排程 ======
scheduler = BackgroundScheduler()
scheduler.add_job(ema_engulfing_check, 'cron', minute='2,32')  # 每根 30 分鐘 K 線收盤後 2 分鐘判斷
scheduler.start()

# ====== Flask 基本路由 ======
@app.route('/')
def home():
    return "EMA 吞沒監控系統運行中..."

if __name__ == "__main__":
    # 啟動時自動發訊息
    send_telegram("EMA 吞沒監控系統啟動成功！")
    app.run(host="0.0.0.0", port=5000)
