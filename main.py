from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = 1634751416

# === 幣種設定 ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
INTERVAL = "30m"
LIMIT = 100

# === 建立 session + 重試機制 ===
def create_session():
    session = requests.Session()
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

session = create_session()

# === Telegram 發送訊息 ===
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print(f"[Telegram] 發送失敗：{e}")

# === 抓取 K 線 ===
def fetch_klines(symbol, interval=INTERVAL, limit=LIMIT):
    url = f"https://api.bitunix.io/api/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[{symbol}] K線抓取失敗：{e}")
        return None

# === EMA 判斷範例 ===
def check_ema_signals():
    for symbol in SYMBOLS:
        data = fetch_klines(symbol)
        if not data:
            print(f"[{symbol}] 無法取得資料，略過")
            continue

        df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume'])
        df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)

        df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
        df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()

        last = df.iloc[-1]
        if last['EMA12'] > last['EMA30'] > last['EMA55']:
            send_telegram(f"[{symbol}] 多頭排列，收盤價: {last['close']}")
        elif last['EMA12'] < last['EMA30'] < last['EMA55']:
            send_telegram(f"[{symbol}] 空頭排列，收盤價: {last['close']}")

# === 排程，每 30 分鐘跑一次 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_ema_signals, 'cron', minute='2,32')  # 收盤後 2 分鐘
scheduler.start()

# === Flask route 保活 ===
@app.route("/")
def home():
    return "EMA 監控系統運行中"

@app.route("/ping")
def ping():
    return "pong"

if __name__ == "__main__":
    send_telegram("[系統] EMA 監控系統啟動成功")
    app.run(host="0.0.0.0", port=10000)
