from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, json, os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
CHAT_ID = "1634751416"
API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# ===== 訊號過濾 =====
sent_signals = set()
SENT_FILE = "sent_signals.json"

if os.path.exists(SENT_FILE):
    try:
        sent_signals = set(json.load(open(SENT_FILE)))
    except:
        sent_signals = set()

def save_sent():
    json.dump(list(sent_signals), open(SENT_FILE, "w"))


# ================================================================
#                   Bitunix 合約 K 線（穩定版）
# ================================================================
def get_klines(symbol, bar="30m", retries=5):
    urls = [
        "https://contract.mapi.bitunix.com/contract/api/v1/market/kline",
        "https://contract-api.bitunix.com/contract/api/v1/market/kline"
    ]

    interval_map = {
        "15m": "15min",
        "30m": "30min",
    }

    interval = interval_map.get(bar, "30min")
    headers = {"User-Agent": "Mozilla/5.0"}

    for attempt in range(retries):
        for base in urls:
            try:
                url = f"{base}?symbol={symbol}USDT&interval={interval}&limit=200"
                r = requests.get(url, headers=headers, timeout=10).json()

                data = r.get("data")
                if not data:
                    time.sleep(1)
                    continue

                df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
                df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)

                df['ts'] = pd.to_datetime(df['ts'], unit='ms').dt.tz_localize("UTC").dt.tz_convert(tz)

                df = df.iloc[::-1].reset_index(drop=True)

                df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
                df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
                df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()

                return df

            except Exception as e:
                print(f"{symbol} 抓取錯誤: {e}")
                time.sleep(1)
    return pd.DataFrame()


# ================================================================
#                   Top3 漲/跌幅榜（Bitunix）
# ================================================================
def get_top3():
    try:
        url = "https://contract.mapi.bitunix.com/contract/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        data = r.get("data", [])

        df = pd.DataFrame(data)
        df["symbol"] = df["symbol"].astype(str)
        df["change"] = df["change"].astype(float)

        top_gainers = df.sort_values("change", ascending=False).head(3)
        top_losers = df.sort_values("change", ascending=True).head(3)

        gain_msg = "🌈 漲幅榜 Top3:\n" + "\n".join(
            [f"{row['symbol']}：{row['change']}%" for _, row in top_gainers.iterrows()]
        )

        loss_msg = "💀 跌幅榜 Top3:\n" + "\n".join(
            [f"{row['symbol']}：{row['change']}%" for _, row in top_losers.iterrows()]
        )

        return gain_msg + "\n\n" + loss_msg

    except:
        return "漲跌幅榜抓取失敗"


# ================================================================
#                   策略判斷（核心）
# ================================================================
def check_signal(df, symbol, timeframe):

    if len(df) < 60:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # ===== 多空排列 =====
    bull_trend = last["EMA12"] > last["EMA30"] > last["EMA55"]
    bear_trend = last["EMA12"] < last["EMA30"] < last["EMA55"]

    # ===== 回踩 EMA30 不碰 EMA55 =====
    touch_ema30 = last["low"] <= last["EMA30"]
    no_touch_ema55 = last["low"] > last["EMA55"]
    pullback_ok = touch_ema30 and no_touch_ema55

    # ===== 吞沒 =====
    bull_engulf = (
        last["close"] > last["open"] and
        prev["close"] < prev["open"] and
        last["close"] >= prev["open"] and
        last["open"] <= prev["close"]
    )

    bear_engulf = (
        last["close"] < last["open"] and
        prev["close"] > prev["open"] and
        last["close"] <= prev["open"] and
        last["open"] >= prev["close"]
    )

    # ===== 多空訊號 =====
    if bull_trend and pullback_ok and bull_engulf:
        return ("LONG", last["close"], "看漲吞沒", timeframe)

    if bear_trend and pullback_ok and bear_engulf:
        return ("SHORT", last["close"], "看跌吞沒", timeframe)

    return None


# ================================================================
#                    發送 Telegram
# ================================================================
def send_msg(text):
    try:
        requests.post(API_URL, data={
            "chat_id": CHAT_ID,
            "text": text
        }, timeout=10)
    except Exception as e:
        print("Telegram 發送失敗:", e)


# ================================================================
#                    主邏輯：15m + 30m
# ================================================================
def scan_all():
    print("=== 掃描 15m + 30m ===")

    try:
        url = "https://contract.mapi.bitunix.com/contract/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        symbols = [d["symbol"].replace("USDT", "") for d in r.get("data", []) if d["symbol"].endswith("USDT")]
    except:
        symbols = []

    top3_text = get_top3()

    for sym in symbols:
        for tf in ["15m", "30m"]:
            df = get_klines(sym, tf)
            if df.empty:
                continue

            sig = check_signal(df, sym, tf)
            if not sig:
                continue

            direction, price, engulf, timeframe = sig
            key = f"{sym}_{timeframe}_{direction}_{df.iloc[-1]['ts']}"

            if key in sent_signals:
                continue

            sent_signals.add(key)
            save_sent()

            msg = (
                f"📌 Bitunix 合約訊號\n"
                f"週期：{timeframe}\n"
                f"幣種：{sym}USDT\n"
                f"方向：{direction}\n"
                f"型態：{engulf}\n"
                f"收盤價：{price}\n\n"
                f"{top3_text}"
            )
            send_msg(msg)


# ================================================================
#                     每日清空
# ================================================================
def reset_daily():
    global sent_signals
    sent_signals = set()
    save_sent()
    send_msg("📅 已清空今日訊號\n\n" + get_top3())


# ================================================================
#                     排程設定
# ================================================================
scheduler = BackgroundScheduler(timezone=tz)

# 每 30 分收盤後 2 分鐘：02、32
scheduler.add_job(scan_all, 'cron', minute='2,32')

# 每 15 分補強
scheduler.add_job(scan_all, 'interval', minutes=15)

# 每日清空
scheduler.add_job(reset_daily, 'cron', hour=0, minute=1)

scheduler.start()


# Flask 伺服器
@app.route("/")
def index():
    return "Bitunix 15m + 30m EMA 吞沒監控運作中"


if __name__ == "__main__":
    # 啟動時立即發訊息
    send_msg(
        "🚀 Bitunix EMA 吞沒監控已啟動 ✅\n"
        f"監控週期：15m + 30m\n系統時間：{datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    # 啟動時立即掃描一次所有幣種
    scan_all()
    
    # 啟動 Flask 伺服器
    app.run(host="0.0.0.0", port=8080)
