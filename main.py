from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import time, json, os

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
CHAT_ID = "1634751416"
API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# ===== 訊號去重 =====
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
#                   Telegram 發送函式 (回報狀態)
# ================================================================
def send_msg(text):
    try:
        r = requests.post(API_URL, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print("Telegram 發送狀態:", r.status_code, r.text)
        return r.status_code == 200
    except Exception as e:
        print("Telegram 發送失敗:", e)
        return False

# ================================================================
#                   Bitunix 合約 K 線
# ================================================================
def get_klines(symbol, bar="30m", retries=5):
    urls = [
        "https://contract.mapi.bitunix.com/contract/api/v1/market/kline",
        "https://contract-api.bitunix.com/contract/api/v1/market/kline"
    ]
    interval_map = {"15m":"15min","30m":"30min"}
    interval = interval_map.get(bar,"30min")
    headers = {"User-Agent":"Mozilla/5.0"}

    for attempt in range(retries):
        for base in urls:
            try:
                url = f"{base}?symbol={symbol}USDT&interval={interval}&limit=200"
                r = requests.get(url, headers=headers, timeout=10).json()
                data = r.get("data")
                if not data: time.sleep(1); continue

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
#                   策略判斷
# ================================================================
def check_signal(df, symbol, timeframe):
    if len(df) < 60: return None
    last = df.iloc[-1]
    prev = df.iloc[-2]

    bull_trend = last["EMA12"] > last["EMA30"] > last["EMA55"]
    bear_trend = last["EMA12"] < last["EMA30"] < last["EMA55"]

    pullback_ok = last["low"] <= last["EMA30"] and last["low"] > last["EMA55"]

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

    if bull_trend and pullback_ok and bull_engulf:
        return ("LONG", last["close"], "看漲吞沒", timeframe)
    if bear_trend and pullback_ok and bear_engulf:
        return ("SHORT", last["close"], "看跌吞沒", timeframe)
    return None

# ================================================================
#                   Top3 漲跌榜
# ================================================================
def get_top3():
    try:
        url = "https://contract.mapi.bitunix.com/contract/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        data = r.get("data", [])
        df = pd.DataFrame(data)
        df["symbol"] = df["symbol"].astype(str)
        df["change"] = df["change"].astype(float)

        top_gainers = df.sort_values("change", ascending=False).head(3)["symbol"].tolist()
        top_losers  = df.sort_values("change", ascending=True).head(3)["symbol"].tolist()
        top3_text = (
            "🌈 漲幅榜 Top3:\n" + "\n".join(top_gainers) + "\n\n" +
            "💀 跌幅榜 Top3:\n" + "\n".join(top_losers)
        )
        return top3_text
    except:
        return "漲跌幅榜抓取失敗"

# ================================================================
#                   掃描指定幣種 (Top3 + BTC/ETH/SOL)
# ================================================================
def scan_all(force=False):
    print("=== 掃描 Top3 + BTC/ETH/SOL ===")
    try:
        url = "https://contract.mapi.bitunix.com/contract/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        data = r.get("data", [])
        df = pd.DataFrame(data)
        df["symbol"] = df["symbol"].astype(str)
        df["change"] = df["change"].astype(float)

        top_gainers = df.sort_values("change", ascending=False).head(3)["symbol"].tolist()
        top_losers  = df.sort_values("change", ascending=True).head(3)["symbol"].tolist()
        main_coins = ["BTCUSDT","ETHUSDT","SOLUSDT"]

        symbols = list(set(top_gainers + top_losers + main_coins))
        top3_text = (
            "🌈 漲幅榜 Top3:\n" + "\n".join(top_gainers) + "\n\n" +
            "💀 跌幅榜 Top3:\n" + "\n".join(top_losers)
        )
    except:
        symbols = ["BTCUSDT","ETHUSDT","SOLUSDT"]
        top3_text = "漲跌幅榜抓取失敗"

    for sym in symbols:
        sym_short = sym.replace("USDT","")
        for tf in ["15m", "30m"]:
            df_k = get_klines(sym_short, tf)
            if df_k.empty: continue

            sig = check_signal(df_k, sym_short, tf)
            if not sig: continue

            direction, price, engulf, timeframe = sig
            key = f"{sym}_{timeframe}_{direction}_{df_k.iloc[-1]['ts']}"

            if key in sent_signals and not force:
                continue

            sent_signals.add(key)
            save_sent()

            msg = (
                f"📌 Bitunix 合約訊號\n"
                f"週期：{timeframe}\n"
                f"幣種：{sym}\n"
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
#                     Telegram 存活訊息
# ================================================================
def send_alive_ping():
    send_msg("💡 Bitunix EMA 監控系統存活中 ✅")

# ================================================================
#                     排程設定
# ================================================================
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(scan_all, 'cron', minute='2,32')        # 30分收盤後 2分
scheduler.add_job(scan_all, 'interval', minutes=15)       # 每15分鐘補強掃描
scheduler.add_job(reset_daily, 'cron', hour=0, minute=1)  # 每日清空
scheduler.add_job(send_alive_ping, 'interval', hours=1)   # 每小時 Telegram ping
scheduler.start()

# ================================================================
#                     Flask 監控頁面
# ================================================================
@app.route("/")
def index():
    return "Bitunix 15m + 30m EMA 吞沒監控運作中"

@app.route("/ping")
def ping():
    return "OK"

# ================================================================
#                     啟動即掃描與 Telegram 測試
# ================================================================
if __name__ == "__main__":
    test_result = send_msg("🚀 Bitunix EMA 監控啟動測試 ✅")
    if test_result:
        print("Telegram 測試訊息已發送成功")
    else:
        print("Telegram 測試訊息發送失敗")

    scan_all(force=True)
    app.run(host="0.0.0.0", port=8080)
