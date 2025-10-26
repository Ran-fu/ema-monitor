from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 固定監控幣種（Bitunix 合約 USDT 對）===
FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# === Bitunix 取得 K 線 ===
def get_klines(symbol, size=1500, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                raise Exception("無 K 線資料")
            df = pd.DataFrame(data, columns=["ts","open","high","low","close","vol"])
            df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
            df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            df = df.iloc[::-1].reset_index(drop=True)
            df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
            df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
            df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    return (prev["close"] < prev["open"] and
            curr["close"] > curr["open"] and
            curr["close"] > prev["open"] and
            curr["open"] < prev["close"])

def is_bearish_engulfing(df, i):
    if i < 1: return False
    prev = df.iloc[i-1]
    curr = df.iloc[i]
    return (prev["close"] > prev["open"] and
            curr["close"] < curr["open"] and
            curr["open"] > prev["close"] and
            curr["close"] < prev["open"])

# === Telegram 發訊 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        print("✅ Telegram 發送成功：", text)
    except Exception as e:
        print("❌ Telegram 發送失敗：", e)

# === 模擬 TP/SL ===
def simulate_trade(df, entry_idx, direction):
    entry_price = df["close"].iloc[entry_idx]
    sl = df["EMA55"].iloc[entry_idx]
    if direction == "long":
        if sl >= entry_price:
            return {"result": "invalid", "exit_idx": None}
        tp = entry_price + (entry_price - sl) * 1.5
    else:
        if sl <= entry_price:
            return {"result": "invalid", "exit_idx": None}
        tp = entry_price - (sl - entry_price) * 1.5

    for i in range(entry_idx + 1, len(df)):
        o, h, l = df.iloc[i][["open","high","low"]]
        if direction == "long":
            if o >= tp: return {"result":"win","exit_idx":i}
            if o <= sl: return {"result":"loss","exit_idx":i}
            if h >= tp: return {"result":"win","exit_idx":i}
            if l <= sl: return {"result":"loss","exit_idx":i}
        else:
            if o <= tp: return {"result":"win","exit_idx":i}
            if o >= sl: return {"result":"loss","exit_idx":i}
            if l <= tp: return {"result":"win","exit_idx":i}
            if h >= sl: return {"result":"loss","exit_idx":i}
    return {"result":"none","exit_idx":None}

# === 回測 ===
def backtest_df(df):
    results = []
    for i in range(60, len(df)):
        ema12, ema30, ema55 = df.iloc[i][["EMA12","EMA30","EMA55"]]
        low = df["low"].iloc[i]
        high = df["high"].iloc[i]

        if ema12 > ema30 > ema55 and low <= ema30 and low > ema55 and is_bullish_engulfing(df, i):
            res = simulate_trade(df, i, "long")
            results.append(res["result"])
        elif ema12 < ema30 < ema55 and high >= ema30 and high < ema55 and is_bearish_engulfing(df, i):
            res = simulate_trade(df, i, "short")
            results.append(res["result"])
    total = len([r for r in results if r in ["win","loss"]])
    wins = results.count("win")
    losses = results.count("loss")
    winrate = (wins/total*100) if total>0 else 0
    return {"trades":total,"wins":wins,"losses":losses,"winrate":winrate}

def backtest_symbol(symbol, days=30):
    df = get_klines(symbol, size=days*48 + 200)
    return backtest_df(df)

# === 取得當日交易量 Top3 幣種 ===
def get_top3_volume_symbols():
    try:
        url = "https://api.bitunix.com/api/v1/market/tickers"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        df = pd.DataFrame(data)
        df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
        df_usdt = df[df["symbol"].str.endswith("USDT")]
        top3 = df_usdt.nlargest(3, "vol")["symbol"].tolist()
        return top3
    except Exception as e:
        print("取得 Top3 交易量幣種失敗：", e)
        return []

# === 自動訊號偵測 ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查 Bitunix 訊號")
    symbols = list(set(FIXED_SYMBOLS + get_top3_volume_symbols()))
    for symbol in symbols:
        try:
            df = get_klines(symbol, size=200)
            ema12, ema30, ema55 = df.iloc[-1][["EMA12","EMA30","EMA55"]]
            low = df["low"].iloc[-1]
            high = df["high"].iloc[-1]
            close = df["close"].iloc[-1]
            time_str = df["ts"].iloc[-1].strftime("%Y-%m-%d %H:%M")

            if ema12 > ema30 > ema55 and low <= ema30 and low > ema55 and is_bullish_engulfing(df, len(df)-1):
                msg = f"🟢 {symbol}\n看漲吞沒\n收盤價：{close:.4f}\n時間：{time_str}"
                send_telegram_message(msg)
            elif ema12 < ema30 < ema55 and high >= ema30 and high < ema55 and is_bearish_engulfing(df, len(df)-1):
                msg = f"🔴 {symbol}\n看跌吞沒\n收盤價：{close:.4f}\n時間：{time_str}"
                send_telegram_message(msg)
        except Exception as e:
            print(f"{symbol} 錯誤：{e}")

# === Flask 路由 ===
@app.route("/")
def home():
    return "✅ Bitunix EMA 吞沒監控系統運作中"

@app.route("/backtest")
def backtest_all():
    results = {}
    for s in FIXED_SYMBOLS:
        r = backtest_symbol(s, days=30)
        results[s] = r
    text_lines = ["📊 Bitunix 近30天回測結果："]
    for s, r in results.items():
        text_lines.append(f"{s}: {r['winrate']:.1f}% 勝率 | {r['wins']}/{r['trades']} 策略交易")
    return "\n".join(text_lines)

# === 定時任務 ===
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(check_signals, "cron", minute="2,32")
scheduler.start()

if __name__ == "__main__":
    print("🚀 Bitunix EMA 吞沒監控系統啟動中...")
    # 使用 Scheduler 延遲執行第一次檢查，避免阻塞
    scheduler.add_job(check_signals, "date", run_date=datetime.utcnow() + timedelta(seconds=10))
    app.run(host="0.0.0.0", port=8080)
