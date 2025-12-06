# main.py (整合版：啟動一次推送 Top3 + 符合訊號)
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

# === 狀態檔、變數 ===
STATE_FILE = "state.json"
state = {
    "signals": {},   # 原本 sent_signals
    "meta": {
        "uptime_alert_sent": False
    }
}
last_check_time = None

UPTIME_PING_URL = os.environ.get("UPTIME_PING_URL")

gainers = []
losers = []
today_list = []

# === Telegram 發訊 ===
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print("TG sent:", msg.splitlines()[0])
    except Exception as e:
        print("TG send error:", e)

# === state load/save ===
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
                if isinstance(st, dict) and "signals" in st and "meta" in st:
                    state = st
                else:
                    state = {"signals": st if isinstance(st, dict) else {}, "meta": {"uptime_alert_sent": False}}
        except:
            state = {"signals": {}, "meta": {"uptime_alert_sent": False}}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("save_state error:", e)

# === 漲跌榜 ===
def get_top_movers():
    global gainers, losers, today_list
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/tickers"
        r = requests.get(url, timeout=10).json()
        tickers = r.get("data") if isinstance(r, dict) else r
        if not tickers:
            return today_list

        df = pd.DataFrame(tickers)
        sym_col = next((c for c in ["symbol","instId","instrument_id"] if c in df.columns), None)
        change_col = next((c for c in ["changeRate","change","priceChangePercent"] if c in df.columns), None)
        if not sym_col or not change_col:
            return today_list

        df = df[[sym_col, change_col]].dropna()
        df[change_col] = pd.to_numeric(df[change_col], errors="coerce")

        raw_gainers = df.sort_values(change_col, ascending=False).head(3)[sym_col].tolist()
        raw_losers  = df.sort_values(change_col, ascending=True ).head(3)[sym_col].tolist()

        def norm(s):
            s = str(s).replace("-", "").replace("_", "").upper()
            return s if s.endswith("USDT") else s+"USDT"

        gainers = [norm(s) for s in raw_gainers]
        losers  = [norm(s) for s in raw_losers]
        fixed = ["BTCUSDT","ETHUSDT","SOLUSDT"]
        today_list = list(dict.fromkeys(gainers + losers + fixed))
        return today_list
    except Exception as e:
        print("get_top_movers error:", e)
        return today_list

# === 取得 K 線 ===
def get_klines(symbol, period="30min", size=200):
    try:
        url = f"{BITUNIX_BASE}/api/v1/market/historyKlines"
        params = {"symbol": symbol, "period": period, "size": size}
        r = requests.get(url, params=params, timeout=10).json()
        data = r.get("data") if isinstance(r, dict) else r
        if not data: return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Taipei")
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        return df
    except Exception as e:
        print("get_klines error:", e)
        return None

# === 吞沒判斷 ===
def is_bull(prev_o, prev_c, cur_o, cur_c):
    return prev_c < prev_o and cur_c > cur_o and cur_c > prev_o and cur_o < prev_c
def is_bear(prev_o, prev_c, cur_o, cur_c):
    return prev_c > prev_o and cur_c < cur_o and cur_c < prev_o and cur_o > prev_c

# === 回踩判斷 ===
def bullish_pullback_ok(c):
    touched30 = c["low"] <= c["ema30"] <= c["high"]
    below55 = c["low"] > c["ema55"]
    close_below = c["low"] <= c["ema30"] and c["close"] < c["ema30"] and c["low"] > c["ema55"]
    return (touched30 and below55) or close_below
def bearish_pullback_ok(c):
    touched30 = c["high"] >= c["ema30"] >= c["low"]
    above55 = c["high"] < c["ema55"]
    close_above = c["high"] >= c["ema30"] and c["close"] > c["ema30"] and c["high"] < c["ema55"]
    return (touched30 and above55) or close_above

# === 處理訊號 (單 timeframe) ===
def process_signal(sym, df, timeframe):
    df["ema12"] = df["close"].ewm(span=12).mean()
    df["ema30"] = df["close"].ewm(span=30).mean()
    df["ema55"] = df["close"].ewm(span=55).mean()

    prev = df.iloc[-2]
    cur = df.iloc[-1]
    signals = []

    # 標示漲跌榜
    if sym in gainers: rank = "漲幅榜"
    elif sym in losers: rank = "跌幅榜"
    else: rank = ""

    # 多頭
    if cur["ema12"] > cur["ema30"] > cur["ema55"]:
        if bullish_pullback_ok(cur) and is_bull(prev["open"], prev["close"], cur["open"], cur["close"]):
            signals.append(f"🟢{sym} [{timeframe}] {rank} 看漲吞沒 收盤:{cur['close']}")

    # 空頭
    if cur["ema12"] < cur["ema30"] < cur["ema55"]:
        if bearish_pullback_ok(cur) and is_bear(prev["open"], prev["close"], cur["open"], cur["close"]):
            signals.append(f"🔴{sym} [{timeframe}] {rank} 看跌吞沒 收盤:{cur['close']}")
    return signals

# === 檢查訊號 (15m+30m) ===
def check_signals():
    global last_check_time, state
    all_signals = []
    symbols = get_top_movers()
    if not symbols: return all_signals
    for sym in symbols:
        try:
            df30 = get_klines(sym, "30min", 200)
            if df30 is not None and len(df30)>=60:
                all_signals += process_signal(sym, df30, "30m")
            df15 = get_klines(sym, "15min", 200)
            if df15 is not None and len(df15)>=60:
                all_signals += process_signal(sym, df15, "15m")
        except:
            continue
    last_check_time = datetime.utcnow()
    save_state()
    return all_signals

# === 啟動一次推送 Top3 + EMA訊號 ===
def startup_message():
    try:
        get_top_movers()
        msg = "📈 今日 Top Movers\n"
        msg += "漲幅榜: " + ", ".join(gainers) + "\n"
        msg += "跌幅榜: " + ", ".join(losers) + "\n"
        msg += "固定追蹤: BTCUSDT, ETHUSDT, SOLUSDT\n\n"

        signals = check_signals()
        if signals:
            msg += "🔔 EMA 吞沒訊號:\n" + "\n".join(signals)
        else:
            msg += "🔔 EMA 吞沒訊號: 無"

        send_telegram(msg)
    except Exception as e:
        print("startup_message error:", e)

# === 其他功能保留 ===
def monitor_health():
    global last_check_time, state
    try:
        now = datetime.utcnow(); problem=False; reason=[]
        if last_check_time is None or (now-last_check_time)>timedelta(minutes=10):
            problem=True; reason.append("長時間未執行 check_signals")
        try:
            port=int(os.environ.get("PORT",8080))
            resp=requests.get(f"http://127.0.0.1:{port}/ping", timeout=5)
            if resp.status_code!=200: problem=True; reason.append(f"/ping 回傳 {resp.status_code}")
        except Exception as e: problem=True; reason.append(f"/ping 連線錯誤: {e}")
        if UPTIME_PING_URL:
            try:
                r=requests.get(UPTIME_PING_URL, timeout=10)
                if r.status_code!=200: problem=True; reason.append(f"UPTIME_PING_URL 回傳 {r.status_code}")
            except Exception as e: problem=True; reason.append(f"UPTIME_PING_URL 連線錯誤: {e}")

        if problem and not state["meta"].get("uptime_alert_sent", False):
            send_telegram("⛔ 機器人健康異常\n原因:"+ "; ".join(reason))
            state["meta"]["uptime_alert_sent"]=True; save_state()
        elif not problem and state["meta"].get("uptime_alert_sent", False):
            send_telegram("✅ 機器人健康已恢復")
            state["meta"]["uptime_alert_sent"]=False; save_state()
    except Exception as e:
        print("monitor_health error:", e)

def daily_reset():
    global state
    state["signals"] = {}
    save_state()
    send_telegram("🧹 每日訊號已清空")

@app.route("/")
def home():
    return render_template_string(f"""
        <h3>Bitunix EMA Monitor (15m+30m)</h3>
        <p>Sent signals: {len(state['signals'])}</p>
        <p>Top movers: {today_list}</p>
        <p>Last check: {last_check_time}</p>
    """)
@app.route("/ping")
def ping(): return "pong", 200

scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(check_signals, "cron", minute="2,17,32,47")
scheduler.add_job(daily_reset, "cron", hour=0, minute=0)
scheduler.add_job(monitor_health, "interval", minutes=5)

if __name__ == "__main__":
    load_state()
    scheduler.start()
    send_telegram("🚀 Bitunix EMA 監控（15m+30m + Uptime）已啟動")
    startup_message()  # 啟動時一次發送 Top3 + EMA訊號
    try: app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)), debug=False)
    except Exception as e: print("flask run error:", e)
