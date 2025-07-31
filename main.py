from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import matplotlib.pyplot as plt
from io import BytesIO
import os
from datetime import datetime
import time

app = Flask(__name__)

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 記錄已發送的訊號，避免重複
sent_signals = {}

@app.route('/')
def home():
    return '✅ EMA Monitor is running.'

@app.route('/ping', methods=['GET', 'HEAD'])
def ping():
    return 'pong', 200

def fetch_klines(symbol, interval="15m", limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}"
    response = requests.get(url)
    data = response.json()
    if "data" not in data:
        return None
    df = pd.DataFrame(data["data"], columns=[
        "timestamp", "open", "high", "low", "close", "volume", "volCcy", "volCcyQuote", "confirm", "chg", "chgPct", "fundingRate"
    ])
    df = df.iloc[::-1]  # 反轉為正序
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    return df

def calculate_ema_signals(df):
    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 多頭條件
    bullish = (
        last["EMA12"] > last["EMA30"] > last["EMA55"] and
        last["low"] < last["EMA30"] and
        last["low"] > last["EMA55"] and
        last["close"] > last["open"] and
        prev["close"] < prev["open"] and
        last["open"] < prev["close"]
    )

    # 空頭條件
    bearish = (
        last["EMA12"] < last["EMA30"] < last["EMA55"] and
        last["high"] > last["EMA30"] and
        last["high"] < last["EMA55"] and
        last["close"] < last["open"] and
        prev["close"] > prev["open"] and
        last["open"] > prev["close"]
    )

    if bullish:
        return "bullish", last["close"], last["EMA55"]
    elif bearish:
        return "bearish", last["close"], last["EMA55"]
    else:
        return None, None, None

def plot_chart(df, symbol, entry, sl, tp):
    plt.figure(figsize=(10, 4))
    plt.plot(df["close"], label="Close", color="black")
    plt.plot(df["EMA12"], label="EMA12", color="orange", linewidth=0.8)
    plt.plot(df["EMA30"], label="EMA30", color="blue", linewidth=0.8)
    plt.plot(df["EMA55"], label="EMA55", color="red", linewidth=0.8)
    plt.axhline(entry, color="green", linestyle="--", label="Entry")
    plt.axhline(sl, color="red", linestyle="--", label="Stop Loss")
    plt.axhline(tp, color="purple", linestyle="--", label="Take Profit")
    plt.title(symbol)
    plt.legend()
    filename = f"{symbol}_chart.png"
    plt.savefig(filename)
    plt.close()
    return filename

def send_signal_to_telegram(symbol, is_bullish, entry_price, stop_loss, take_profit, chart_path):
    direction = "多頭 📈" if is_bullish else "空頭 📉"
    text = (
        f"[{direction}訊號] {symbol}\n"
        f"進場價格：{entry_price:.4f}\n"
        f"止損價格：{stop_loss:.4f}\n"
        f"止盈價格：{take_profit:.4f}\n"
        f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    with open(chart_path, 'rb') as photo:
        files = {'photo': photo}
        data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': text}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", files=files, data=data)

def check_signals():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
    response = requests.get(url)
    data = response.json()

    if "data" not in data:
        print("取得幣種資料失敗")
        return

    for item in data["data"]:
        instId = item["instId"]
        if not instId.endswith("USDT"):
            continue

        df = fetch_klines(instId)
        if df is None or df.empty:
            continue

        signal, entry_price, stop_loss = calculate_ema_signals(df)
        if signal:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            key = f"{instId}_{signal}_{now}"
            if key in sent_signals:
                continue
            sent_signals[key] = True

            tp = entry_price + 1.5 * (entry_price - stop_loss) if signal == "bullish" else entry_price - 1.5 * (stop_loss - entry_price)
            chart_path = plot_chart(df, instId, entry_price, stop_loss, tp)
            send_signal_to_telegram(instId, signal == "bullish", entry_price, stop_loss, tp, chart_path)
            os.remove(chart_path)

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_signals, 'interval', minutes=15)
    scheduler.start()
    # 啟動時發送訊息
    startup_msg = f"✅ 監控系統啟動成功\n時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  data={"chat_id": TELEGRAM_CHAT_ID, "text": startup_msg})
    app.run(host="0.0.0.0", port=8080)
