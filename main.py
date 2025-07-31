from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO
import time
import datetime

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 訊號記錄避免重複推播
notified_symbols = {}

@app.route('/')
def home():
    return '✅ EMA Monitor is running.'

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, data=data)

def send_telegram_photo(image_buf):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", image_buf)}
    data = {"chat_id": TELEGRAM_CHAT_ID}
    requests.post(url, files=files, data=data)

def send_startup_message():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    send_telegram_message(f"🚀 EMA策略監控已啟動！\n時間：{now}")

def get_symbols():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
    res = requests.get(url).json()
    return [t["instId"] for t in res["data"] if t["instId"].endswith("-USDT")]

def get_klines(symbol):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=15m&limit=100"
    res = requests.get(url).json()
    df = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    df = df.iloc[::-1]  # 時間順序反轉
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    return df

def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev["close"] < prev["open"] and curr["close"] > curr["open"] and curr["close"] > prev["open"] and curr["open"] < prev["close"]

def plot_chart(df, symbol, signal_price, stop_loss_price, take_profit_price):
    plt.figure(figsize=(10, 4))
    plt.plot(df["close"], label="Close")
    plt.axhline(signal_price, color="green", linestyle="--", label=f"📈 Signal: {signal_price:.4f}")
    plt.axhline(stop_loss_price, color="red", linestyle="--", label=f"❌ Stop Loss: {stop_loss_price:.4f}")
    plt.axhline(take_profit_price, color="blue", linestyle="--", label=f"🎯 Take Profit: {take_profit_price:.4f}")
    plt.title(symbol)
    plt.legend()
    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

def check_signals():
    symbols = get_symbols()
    for symbol in symbols:
        try:
            df = get_klines(symbol)
            df["EMA12"] = df["close"].ewm(span=12).mean()
            df["EMA30"] = df["close"].ewm(span=30).mean()
            df["EMA55"] = df["close"].ewm(span=55).mean()

            if not (
                df["EMA12"].iloc[-1] > df["EMA30"].iloc[-1] > df["EMA55"].iloc[-1]
            ):
                continue

            close_price = df["close"].iloc[-1]
            if not (
                df["low"].iloc[-1] <= df["EMA30"].iloc[-1] <= df["high"].iloc[-1] and
                df["low"].iloc[-1] > df["EMA55"].iloc[-1]
            ):
                continue

            if not is_bullish_engulfing(df):
                continue

            signal_time = pd.to_datetime(df["ts"].iloc[-1], unit="ms").strftime("%Y-%m-%d %H:%M")

            last_notified = notified_symbols.get(symbol)
            if last_notified and last_notified["direction"] == "long" and last_notified["time"] == signal_time:
                continue  # 避免重複通知

            signal_price = close_price
            stop_loss_price = df["EMA55"].iloc[-1]
            take_profit_price = signal_price + 1.5 * (signal_price - stop_loss_price)

            chart_buf = plot_chart(df, symbol, signal_price, stop_loss_price, take_profit_price)
            send_telegram_photo(chart_buf)

            message = f"""
📈 *多頭訊號偵測*
幣種: {symbol}
時間: {signal_time}
現價: {signal_price:.4f}
止損: {stop_loss_price:.4f}
止盈: {take_profit_price:.4f}
            """
            send_telegram_message(message.strip())
            notified_symbols[symbol] = {"direction": "long", "time": signal_time}

            time.sleep(0.5)

        except Exception as e:
            print(f"錯誤：{symbol} - {e}")
            continue

# APScheduler 任務
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_signals, trigger="interval", minutes=15)

@app.before_first_request
def activate_scheduler():
    send_startup_message()
    scheduler.start()

# ⚠️ UptimeRobot 建議：設定每 5 分鐘 ping 你的網址以保持 Render 活著

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
