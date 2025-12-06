from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests, pandas as pd, json, os, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 狀態紀錄 ===
sent_signals = {}
today_top_list = []
today_date = None
last_check_time = None
STATE_FILE = "state.json"

# === 載入/保存狀態 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                sent_signals.update({k: datetime.fromisoformat(v) for k, v in data.get("sent_signals", {}).items()})
                td = data.get("today_date")
                if td:
                    today_date = datetime.fromisoformat(td).date()
            print("🧩 狀態已載入")
        except:
            print("⚠️ 狀態載入失敗")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "sent_signals": {k: v.isoformat() for k, v in sent_signals.items()},
                "today_date": str(today_date)
            }, f)
    except:
        print("⚠️ 狀態保存失敗")

# === Telegram 發訊 ===
def send_telegram_message(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        print("✅ 發送訊息:", text.splitlines()[0])
    except Exception as e:
        print("❌ Telegram 發訊異常:", e)

# === 清理舊訊號 ===
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [k for k, ts in sent_signals.items() if ts < cutoff]
    for k in keys_to_delete:
        del sent_signals[k]

# === 取得 K 線資料 ===
def get_klines(symbol, period="30m", size=200):
    url = f"https://fapi.bitunix.com/api/v1/market/historyKlines"
    try:
        resp = requests.get(url, params={"symbol": symbol, "period": period, "size": size}, timeout=10).json()
        data = resp.get("data", [])
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","volume"])
        df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Taipei")
        df = df.iloc[::-1].reset_index(drop=True)
        df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
        df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
        df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()
        return df
    except Exception as e:
        print(f"[{symbol}] K線抓取失敗:", e)
        return None

# === 更新每日漲跌榜前三 + BTC/ETH/SOL ===
def update_today_top_list():
    global today_top_list, today_date
    now_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if today_date != now_date:
        today_date = now_date
        try:
            url = "https://fapi.bitunix.com/api/v1/market/tickers"
            r = requests.get(url, timeout=10).json()
            tickers = r.get("data", [])
            df = pd.DataFrame(tickers)
            df["change"] = pd.to_numeric(df.get("change", df.get("changeRate", 0)), errors="coerce")
            df = df.dropna(subset=["change"])
            # 前三漲幅
            top_up = df.sort_values("change", ascending=False).head(3)["symbol"].tolist()
            # 前三跌幅
            top_down = df.sort_values("change", ascending=True).head(3)["symbol"].tolist()
            # 固定 BTC/ETH/SOL
            fixed = ["BTCUSDT","ETHUSDT","SOLUSDT"]
            # 合併並去重
            today_top_list = list(dict.fromkeys(top_up + top_down + fixed))
            print("📊 今日Top榜:", today_top_list)
        except Exception as e:
            print("⚠️ 更新今日Top榜失敗:", e)

# === 每日清空訊號 ===
def daily_reset():
    global sent_signals
    sent_signals.clear()
    update_today_top_list()
    save_state()
    send_telegram_message("🧹 每日訊號已清空，今日Top榜已更新")

# === 判斷吞沒訊號 ===
def is_bull(prev_o, prev_c, cur_o, cur_c):
    return prev_c < prev_o and cur_c > cur_o and cur_c > prev_o and cur_o < prev_c

def is_bear(prev_o, prev_c, cur_o, cur_c):
    return prev_c > prev_o and cur_c < cur_o and cur_c < prev_o and cur_o > prev_c

def bullish_pullback_ok(c):
    touched30 = c["low"] <= c["EMA30"] <= c["high"]
    below55   = c["low"] > c["EMA55"]
    close_below = c["low"] <= c["EMA30"] and c["close"] < c["EMA30"] and c["low"] > c["EMA55"]
    return (touched30 and below55) or close_below

def bearish_pullback_ok(c):
    touched30 = c["high"] >= c["EMA30"] >= c["low"]
    above55   = c["high"] < c["EMA55"]
    close_above = c["high"] >= c["EMA30"] and c["close"] > c["EMA30"] and c["high"] < c["EMA55"]
    return (touched30 and above55) or close_above

# === 處理單一幣種訊號 ===
def process_signal(sym, df, timeframe):
    prev = df.iloc[-2]
    cur  = df.iloc[-1]

    bull_key = f"{sym}-{str(cur['ts'])}-{timeframe}-bull"
    bear_key = f"{sym}-{str(cur['ts'])}-{timeframe}-bear"

    # 標示漲跌榜
    if sym in today_top_list[:3]:
        rank = "🔥 Top3"
    elif sym in today_top_list[3:6]:
        rank = "❄️ Bottom3"
    else:
        rank = ""

    # 多頭訊號
    if cur["EMA12"] > cur["EMA30"] > cur["EMA55"]:
        if bullish_pullback_ok(cur) and is_bull(prev["open"], prev["close"], cur["open"], cur["close"]):
            if bull_key not in sent_signals:
                msg = f"🟢{rank} {sym} [{timeframe}]\n看漲吞沒\n收盤: {cur['close']} ({cur['ts']})"
                send_telegram_message(msg)
                sent_signals[bull_key] = time.time()

    # 空頭訊號
    if cur["EMA12"] < cur["EMA30"] < cur["EMA55"]:
        if bearish_pullback_ok(cur) and is_bear(prev["open"], prev["close"], cur["open"], cur["close"]):
            if bear_key not in sent_signals:
                msg = f"🔴{rank} {sym} [{timeframe}]\n看跌吞沒\n收盤: {cur['close']} ({cur['ts']})"
                send_telegram_message(msg)
                sent_signals[bear_key] = time.time()

# === 檢查所有訊號 ===
def check_signals():
    global last_check_time
    cleanup_old_signals()
    update_today_top_list()

    main_symbols = ["BTCUSDT","ETHUSDT","SOLUSDT"]
    watch_symbols = list(dict.fromkeys(main_symbols + today_top_list))

    for sym in watch_symbols:
        for timeframe in ["15m","30m"]:
            df = get_klines(sym, period=timeframe)
            if df is not None and len(df) >= 60:
                process_signal(sym, df, timeframe)

    last_check_time = datetime.utcnow()
    save_state()

# === 掉線檢查 ===
def check_health():
    global last_check_time
    now = datetime.utcnow()
    if last_check_time is None:
        last_check_time = now
        return
    if (now - last_check_time) > timedelta(minutes=60):
        send_telegram_message(f"⚠️ 系統可能掉線，最後檢查時間：{last_check_time}")
        last_check_time = now

# === 時區檢查 ===
def check_timezone():
    taiwan_now = datetime.now(ZoneInfo("Asia/Taipei"))
    utc_now = datetime.utcnow()
    diff = abs((taiwan_now - (utc_now + timedelta(hours=8))).total_seconds()) / 60
    if diff > 5:
        send_telegram_message(f"⚠️ 時區異常: 與台灣時間偏差 {diff:.1f} 分鐘")
    print(f"🕓 時區檢查完成：{taiwan_now}")

# === Flask 頁面 ===
@app.route('/')
def home():
    top_text = ", ".join(today_top_list) if today_top_list else "尚未更新"
    return render_template_string(f"""
        <h1>🚀 Bitunix EMA 吞沒監控 ✅</h1>
        <p>📊 今日Top榜: {top_text}</p>
        <p>🕒 上次檢查: {last_check_time}</p>
    """)

@app.route('/ping')
def ping():
    return 'pong', 200

# === 排程設定 ===
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(check_signals, 'cron', minute='2,32')
scheduler.add_job(check_health, 'interval', minutes=10)
scheduler.add_job(check_timezone, 'interval', minutes=15)
scheduler.add_job(daily_reset, 'cron', hour=0, minute=0)
scheduler.start()

# === 啟動立即執行 ===
load_state()
update_today_top_list()
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動 ✅")
check_signals()
check_timezone()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
