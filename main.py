from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import time
import datetime
import pytz

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# EMA 計算
def calculate_ema(prices, period):
    ema = []
    k = 2 / (period + 1)
    ema.append(sum(prices[:period]) / period)
    for price in prices[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

# 發送訊息
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"傳送失敗：{e}")

# 判斷吞沒形態
def is_bullish_engulfing(k1, k2):
    return k1['close'] < k1['open'] and k2['close'] > k2['open'] and k2['close'] > k1['open'] and k2['open'] < k1['close']

def is_bearish_engulfing(k1, k2):
    return k1['close'] > k1['open'] and k2['close'] < k2['open'] and k2['close'] < k1['open'] and k2['open'] > k1['close']

# 主策略邏輯
def check_signals():
    try:
        res = requests.get("https://www.okx.com/api/v5/market/tickers?instType=SPOT")
        tickers = res.json().get("data", [])
        usdt_pairs = [t["instId"] for t in tickers if t["instId"].endswith("USDT")]

        for symbol in usdt_pairs:
            url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=30m&limit=100"
            r = requests.get(url)
            candles = r.json().get("data", [])
            candles.reverse()

            closes = [float(c[4]) for c in candles]
            if len(closes) < 60:
                continue

            ema12 = calculate_ema(closes, 12)
            ema30 = calculate_ema(closes, 30)
            ema55 = calculate_ema(closes, 55)

            ema12_c, ema30_c, ema55_c = ema12[-1], ema30[-1], ema55[-1]

            c1 = {"open": float(candles[-2][1]), "close": float(candles[-2][4])}
            c2 = {"open": float(candles[-1][1]), "close": float(candles[-1][4])}
            current_price = c2['close']

            # 多頭訊號
            if ema12_c > ema30_c > ema55_c and abs(current_price - ema30_c) / ema30_c < 0.005 and current_price > ema55_c and is_bullish_engulfing(c1, c2):
                tp = round(current_price * 1.015, 4)
                sl = round(ema55_c, 4)
                send_telegram_message(
                    f"📈 多頭訊號：{symbol}\n進場價：{current_price}\n止盈價：{tp}\n止損價：{sl}"
                )

            # 空頭訊號
            elif ema12_c < ema30_c < ema55_c and abs(current_price - ema30_c) / ema30_c < 0.005 and current_price < ema55_c and is_bearish_engulfing(c1, c2):
                tp = round(current_price * 0.985, 4)
                sl = round(ema55_c, 4)
                send_telegram_message(
                    f"📉 空頭訊號：{symbol}\n進場價：{current_price}\n止盈價：{tp}\n止損價：{sl}"
                )

    except Exception as e:
        print(f"錯誤：{e}")

# 每 5 分鐘執行
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, "interval", minutes=5)
scheduler.start()

# 啟動時立即發送通知
tz = pytz.timezone("Asia/Taipei")
now = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
send_telegram_message(f"🚀 EMA 監控系統已啟動！\n目前時間：{now}\n即將開始監控 OKX 現貨 USDT 對幣種...")

@app.route('/')
def index():
    return "EMA Monitor is running!"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
