# main.py
from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# === Telegram 設定（你給的 token）===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === Bitunix 設定（如有不同請改 base_url / endpoints）===
BITUNIX_BASE = "https://fapi.bitunix.com"

# === 狀態／紀錄 ===
sent_signals = {}  # 用來避免重複發訊
STATE_FILE = "state.json"
today_top3 = []
today_date = None

# === helpers: Telegram ===
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print("TG sent:", msg.splitlines()[0])
    except Exception as e:
        print("TG send error:", e)

# === 取得漲幅榜/跌幅榜（Bitunix tickers） ===
def get_top_movers():
    global today_top3, today_date
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()

        # 支援回傳 data list 或直接 list
        tickers = None
        if isinstance(r, dict) and r.get("data") and isinstance(r["data"], list):
            tickers = r["data"]
        elif isinstance(r, list):
            tickers = r
        else:
            print("Top movers: unknown response format")
            return []

        df = pd.DataFrame(tickers)

        # 嘗試找到 changeRate / change 欄位
        change_col = None
        for c in ["changeRate", "change", "priceChangePercent"]:
            if c in df.columns:
                change_col = c
                break
        if change_col is None:
            print("Top movers: no change column")
            return []

        # normalize symbol field
        sym_col = None
        for c in ["symbol", "instId", "instrument_id"]:
            if c in df.columns:
                sym_col = c
                break
        if sym_col is None:
            print("Top movers: no symbol column")
            return []

        # 確保欄位型態
        df = df[[sym_col, change_col]].dropna()
        df[change_col] = pd.to_numeric(df[change_col], errors="coerce")
        df = df.dropna(subset=[change_col])

        # 取前三漲幅與前三跌幅
        gainers = df.sort_values(change_col, ascending=False).head(3)[sym_col].tolist()
        losers = df.sort_values(change_col, ascending=True).head(3)[sym_col].tolist()

        # 標準化 symbol（例如 BTC_USDT 或 BTC-USDT 或 BTCUSDT -> BTCUSDT）
        def normalize(s):
            s = str(s)
            s = s.replace("-", "").replace("_", "")
            # 若有 USDT 就保留整段（BTCUSDT），否則嘗試加 USDT
            if s.upper().endswith("USDT"):
                return s.upper()
            else:
                return (s.upper() + "USDT")

        gainers = [normalize(s) for s in gainers]
        losers = [normalize(s) for s in losers]

        # 固定加上 BTC ETH SOL
        fixed = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

        symbols = list(dict.fromkeys(gainers + losers + fixed))  # 去重但保留順序

        print("Top movers symbols:", symbols)
        return symbols
    except Exception as e:
        print("get_top_movers error:", e)
        return []

# === 取得 Bitunix K 線（30min） ===
def get_klines_30m(symbol, size=200):
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/historyKlines"
        params = {"symbol": symbol, "period": "30min", "size": size}
        r = requests.get(url, params=params, timeout=10).json()

        # 支援 r["data"] 或直接 list
        data = None
        if isinstance(r, dict) and r.get("data"):
            data = r["data"]
        elif isinstance(r, list):
            data = r
        else:
            print(f"get_klines_30m {symbol}: unknown response")
            return None

        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Taipei")
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        df = df.reset_index(drop=True)
        return df
    except Exception as e:
        print("get_klines_30m error:", e)
        return None

# === 吞沒判斷函式 ===
def is_bullish_engulfing(prev_open, prev_close, curr_open, curr_close):
    return (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)

def is_bearish_engulfing(prev_open, prev_close, curr_open, curr_close):
    return (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)

# === 主檢查邏輯（30m / 15m 可視需求擴充）===
def check_signals():
    global sent_signals
    try:
        symbols = get_top_movers()
        if not symbols:
            print("No symbols to check.")
            return

        for sym in symbols:
            df = get_klines_30m(sym, size=200)
            if df is None or len(df) < 60:
                print(f"{sym} data insufficient")
                continue

            # 計 EMA
            df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
            df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()
            df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()

            prev = df.iloc[-2]
            curr = df.iloc[-1]

            prev_open = prev["open"]; prev_close = prev["close"]
            curr_open = curr["open"]; curr_close = curr["close"]
            low_ = curr["low"]; high_ = curr["high"]
            ema12 = curr["ema12"]; ema30 = curr["ema30"]; ema55 = curr["ema55"]
            candle_time = curr["ts"].strftime("%Y-%m-%d %H:%M")

            # keys for dedupe
            bull_key = f"{sym}-30m-{candle_time}-bull"
            bear_key = f"{sym}-30m-{candle_time}-bear"

            # 判斷：EMA 多頭排列 或 空頭排列
            # 多頭吞沒（碰或跌破 EMA30 且未碰 EMA55）
            if ema12 > ema30 > ema55:
                cond_a = (low_ <= ema30 < high_ and low_ > ema55)  # 剛好碰到 EMA30（在 range 內）且未碰 EMA55
                cond_b = (low_ <= ema30 and curr_close < ema30 and low_ > ema55)  # 跌破 EMA30 收在下方但未碰 EMA55
                if (cond_a or cond_b) and is_bullish_engulfing(prev_open, prev_close, curr_open, curr_close):
                    if bull_key not in sent_signals:
                        prefix = "🟢"
                        msg = f"{prefix}{sym} [30m]\n看漲吞沒（收盤K線確認）\n碰或跌破 EMA30 未碰 EMA55\n收盤: {curr_close} ({candle_time})"
                        send_telegram(msg)
                        sent_signals[bull_key] = datetime.utcnow().isoformat()

            # 空頭吞沒（碰或突破 EMA30 且未碰 EMA55） —— 注意邏輯同理
            if ema12 < ema30 < ema55:
                cond_a = (high_ >= ema30 > low_ and high_ < ema55)
                cond_b = (high_ >= ema30 and curr_close > ema30 and high_ < ema55)
                if (cond_a or cond_b) and is_bearish_engulfing(prev_open, prev_close, curr_open, curr_close):
                    if bear_key not in sent_signals:
                        prefix = "🔴"
                        msg = f"{prefix}{sym} [30m]\n看跌吞沒（收盤K線確認）\n碰或突破 EMA30 未碰 EMA55\n收盤: {curr_close} ({candle_time})"
                        send_telegram(msg)
                        sent_signals[bear_key] = datetime.utcnow().isoformat()

        # 儲存狀態（避免重複）
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(sent_signals, f)
        except Exception as e:
            print("save state error:", e)

    except Exception as e:
        print("check_signals error:", e)

# === 每日重置 ===
def daily_reset():
    global sent_signals
    sent_signals = {}
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(sent_signals, f)
    except:
        pass
    send_telegram("🧹 今日訊號已清空（每日重置），Top movers 將重新抓取。")

# === 啟動時通知 ===
def startup_notice():
    send_telegram("🚀 Bitunix EMA 吞沒監控已啟動 ✅\n(抓漲幅榜前三、跌幅榜前三、以及 BTC/ETH/SOL)")

# === Flask 頁面（簡單狀態）===
@app.route("/")
def home():
    return render_template_string(f"""
        <h3>Bitunix EMA Monitor</h3>
        <p>Sent signals: {len(sent_signals)}</p>
        <p>Last reset date: {today_date}</p>
    """)

# === 排程設定（台灣時區）===
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(check_signals, "cron", minute="2,32")   # 每根 30 分收盤後 2 分鐘判斷
scheduler.add_job(daily_reset, "cron", hour=0, minute=0)  # 每日 00:00 reset
scheduler.start()

# === 啟動流程 ===
if __name__ == "__main__":
    # load state if exist
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                sent_signals = json.load(f)
        except:
            sent_signals = {}

    startup_notice()
    # run first check immediately (非阻塞)
    try:
        check_signals()
    except Exception as e:
        print("first check error:", e)

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
