# main.py
from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests, pandas as pd, json, os, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === Bitunix 設定 ===
BITUNIX_BASE = "https://fapi.bitunix.com"

# === 狀態 ===
STATE_FILE = "state.json"
sent_signals = {}
today_top3 = []
today_date = None

# === Telegram 發訊 ===
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print("TG sent:", msg.splitlines()[0])
    except Exception as e:
        print("TG send error:", e)

# === 取得漲跌榜前三 + 固定 BTC/ETH/SOL ===
def get_top_movers():
    global today_top3, today_date
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        tickers = r.get("data") if isinstance(r, dict) else r
        if not tickers: return []

        df = pd.DataFrame(tickers)
        # 找欄位
        sym_col = next((c for c in ["symbol","instId","instrument_id"] if c in df.columns), None)
        change_col = next((c for c in ["changeRate","change","priceChangePercent"] if c in df.columns), None)
        if not sym_col or not change_col: return []

        df = df[[sym_col, change_col]].dropna()
        df[change_col] = pd.to_numeric(df[change_col], errors="coerce")
        df = df.dropna(subset=[change_col])

        gainers = df.sort_values(change_col, ascending=False).head(3)[sym_col].tolist()
        losers = df.sort_values(change_col, ascending=True).head(3)[sym_col].tolist()

        # 標準化
        def normalize(s):
            s = str(s).replace("-", "").replace("_","").upper()
            return s if s.endswith("USDT") else s+"USDT"

        gainers = [normalize(s) for s in gainers]
        losers = [normalize(s) for s in losers]

        fixed = ["BTCUSDT","ETHUSDT","SOLUSDT"]
        symbols = list(dict.fromkeys(gainers + losers + fixed))  # 去重保留順序
        today_top3 = symbols
        print("Top movers:", symbols)
        return symbols
    except Exception as e:
        print("get_top_movers error:", e)
        return []

# === 取得 Bitunix K 線 ===
def get_klines_30m(symbol, size=200):
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/historyKlines"
        params = {"symbol": symbol, "period": "30min", "size": size}
        r = requests.get(url, params=params, timeout=10).json()
        data = r.get("data") if isinstance(r, dict) else r
        if not data: return None

        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Taipei")
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df.reset_index(drop=True)
    except Exception as e:
        print("get_klines_30m error:", e)
        return None

# === 吞沒判斷 ===
def is_bullish_engulfing(prev_open, prev_close, curr_open, curr_close):
    return prev_close < prev_open and curr_close > curr_open and curr_close > prev_open and curr_open < prev_close

def is_bearish_engulfing(prev_open, prev_close, curr_open, curr_close):
    return prev_close > prev_open and curr_close < curr_open and curr_close < prev_open and curr_open > prev_close

# === 檢查訊號 ===
def check_signals():
    global sent_signals
    try:
        symbols = get_top_movers()
        if not symbols: return

        for sym in symbols:
            df = get_klines_30m(sym)
            if df is None or len(df)<60: continue

            df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
            df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()
            df["ema55"] = df["close"].ewm(span=55, adjust=False).mean()

            prev = df.iloc[-2]; curr = df.iloc[-1]
            bull_key = f"{sym}-{curr['ts']}-bull"
            bear_key = f"{sym}-{curr['ts']}-bear"

            # 多頭吞沒
            if curr["ema12"]>curr["ema30"]>curr["ema55"]:
                cond = ((curr["low"]<=curr["ema30"]<curr["high"] and curr["low"]>curr["ema55"]) or
                        (curr["low"]<=curr["ema30"] and curr["close"]<curr["ema30"] and curr["low"]>curr["ema55"]))
                if cond and is_bullish_engulfing(prev["open"], prev["close"], curr["open"], curr["close"]):
                    if bull_key not in sent_signals:
                        prefix = "🟢"
                        rank = "漲幅榜" if sym in symbols[:3] else "跌幅榜" if sym in symbols[3:6] else ""
                        msg = f"{prefix}{sym} [30m] ({rank})\n看漲吞沒\n收盤: {curr['close']} ({curr['ts']})"
                        send_telegram(msg)
                        sent_signals[bull_key] = datetime.utcnow().isoformat()

            # 空頭吞沒
            if curr["ema12"]<curr["ema30"]<curr["ema55"]:
                cond = ((curr["high"]>=curr["ema30"]>curr["low"] and curr["high"]<curr["ema55"]) or
                        (curr["high"]>=curr["ema30"] and curr["close"]>curr["ema30"] and curr["high"]<curr["ema55"]))
                if cond and is_bearish_engulfing(prev["open"], prev["close"], curr["open"], curr["close"]):
                    if bear_key not in sent_signals:
                        prefix = "🔴"
                        rank = "漲幅榜" if sym in symbols[:3] else "跌幅榜" if sym in symbols[3:6] else ""
                        msg = f"{prefix}{sym} [30m] ({rank})\n看跌吞沒\n收盤: {curr['close']} ({curr['ts']})"
                        send_telegram(msg)
                        sent_signals[bear_key] = datetime.utcnow().isoformat()

        # 儲存狀態
        try:
            with open(STATE_FILE,"w") as f:
                json.dump(sent_signals, f)
        except:
            pass

    except Exception as e:
        print("check_signals error:", e)

# === 每日重置 ===
def daily_reset():
    global sent_signals
    sent_signals = {}
    try:
        with open(STATE_FILE,"w") as f:
            json.dump(sent_signals,f)
    except: pass
    send_telegram("🧹 今日訊號已清空（每日重置）")

# === Flask endpoints ===
@app.route("/")
def home():
    return render_template_string(f"""
        <h3>Bitunix EMA Monitor</h3>
        <p>Sent signals: {len(sent_signals)}</p>
        <p>Top movers: {', '.join(today_top3)}</p>
    """)

@app.route("/ping")
def ping():
    return "pong", 200

# === 排程 ===
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(check_signals,"cron",minute="2,32")
scheduler.add_job(daily_reset,"cron",hour=0,minute=0)

# === 啟動流程 ===
if __name__=="__main__":
    # load state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                sent_signals = json.load(f)
        except:
            sent_signals = {}

    scheduler.start()
    send_telegram("🚀 Bitunix EMA 吞沒監控已啟動 ✅")
    # 立即第一次檢查
    try:
        check_signals()
    except:
        pass

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)), debug=False)
