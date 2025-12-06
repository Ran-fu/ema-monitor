import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import json

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "你的BotToken"
CHAT_ID = "你的ChatID"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram Error:", e)

# === Bitunix API ===
KLINE_URL = "https://api.bitunix.com/contract/market/kline"
TOP3_URL = "https://api.bitunix.com/contract/market/ticker"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = ["15", "30"]

def fetch_kline(symbol, interval):
    try:
        r = requests.get(KLINE_URL, params={"symbol":symbol,"interval":interval,"limit":200}, timeout=5)
        data = r.json()
        if "data" in data:
            return pd.DataFrame(data["data"], columns=["timestamp","open","high","low","close","volume"])
        else:
            print(f"Kline no data for {symbol} {interval}")
            return None
    except Exception as e:
        print(f"Kline Error: {e}")
        return None

def fetch_top3():
    try:
        r = requests.get(TOP3_URL, params={"symbol":"ALL"}, timeout=5)
        data = r.json()
        if "data" in data:
            return data["data"]  # 可以自行整理成漲幅榜
        else:
            print("Top3 no data")
            return None
    except Exception as e:
        print(f"Top3 Error: {e}")
        return None

# === EMA 計算與訊號判斷 ===
def check_ema_signal(df):
    df["ema12"] = df["close"].astype(float).ewm(span=12).mean()
    df["ema30"] = df["close"].astype(float).ewm(span=30).mean()
    df["ema55"] = df["close"].astype(float).ewm(span=55).mean()
    last = df.iloc[-1]
    signal = None
    # 簡單示例: EMA 多頭排列 + 收盤 > EMA30
    if last["ema12"] > last["ema30"] > last["ema55"]:
        signal = "多頭"
    elif last["ema12"] < last["ema30"] < last["ema55"]:
        signal = "空頭"
    return signal

# === 主程式迴圈 ===
def main_loop():
    while True:
        top3 = fetch_top3()
        if top3:
            print("Top3:", top3[:3])

        for symbol in SYMBOLS:
            for interval in INTERVALS:
                df = fetch_kline(symbol, interval)
                if df is not None and not df.empty:
                    signal = check_ema_signal(df)
                    if signal:
                        msg = f"{symbol} {interval}分 K 線 EMA 訊號: {signal}\n收盤價: {df.iloc[-1]['close']}"
                        print(msg)
                        send_telegram(msg)
        # 每 5 分鐘抓一次
        time.sleep(300)

if __name__ == "__main__":
    send_telegram("Bitunix EMA 監控啟動")
    main_loop()
