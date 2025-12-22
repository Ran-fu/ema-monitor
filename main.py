from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, os, json

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# === Telegram ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"  # 可以先填預設，抓到後再改

# === 狀態 ===
sent_signals = {}
today_top3_up = []
today_top3_down = []
today_date = None
STATE_FILE = "state.json"

# === State ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            d = json.load(f)
            sent_signals = {
                k: datetime.fromisoformat(v)
                for k, v in d.get("sent_signals", {}).items()
            }
            if d.get("today_date"):
                today_date = datetime.fromisoformat(d["today_date"]).date()

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "sent_signals": {k: v.isoformat() for k, v in sent_signals.items()},
            "today_date": str(today_date)
        }, f)

# === Telegram 發送（改進版）===
def send_telegram(text):
    if not TELEGRAM_CHAT_ID:
        print("⚠️ 尚未設定 CHAT ID，請先抓取")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
        if resp.status_code != 200:
            print(f"❌ Telegram 發送失敗: {resp.status_code}, {resp.text}")
        else:
            print(f"✅ Telegram 發送成功: {text}")
    except Exception as e:
        print(f"❌ Telegram 發送異常: {e}")

# === 自動抓 Chat ID ===
def fetch_chat_ids():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=10).json()
        if not r.get("ok"):
            print(f"❌ Telegram getUpdates 失敗: {r}")
            return
        results = r.get("result", [])
        if not results:
            print("⚠️ 尚未收到任何訊息，請先對 Bot 發送 /start 或在群組發訊息")
            return
        print("📌 找到以下 Chat ID：")
        for item in results:
            msg = item.get("message")
            if not msg:
                continue
            chat = msg.get("chat")
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            chat_name = chat.get("first_name") if chat_type == "private" else chat.get("title")
            print(f"{chat_type}: {chat_name} → {chat_id}")
    except Exception as e:
        print(f"❌ 抓取 Chat ID 發生異常: {e}")

# === 清理舊訊號 ===
def cleanup_old(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for k in list(sent_signals):
        if sent_signals[k] < cutoff:
            del sent_signals[k]

# === OKX 永續 K 線（含 confirm）===
def get_klines(symbol, bar="30m"):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {
        "instId": f"{symbol}-USDT-SWAP",
        "bar": bar,
        "limit": 200
    }
    r = requests.get(url, params=params, timeout=10).json()
    data = r.get("data", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(
        data,
        columns=["ts","open","high","low","close","vol","c1","c2","confirm"]
    )

    df[["open","high","low","close","vol"]] = \
        df[["open","high","low","close","vol"]].astype(float)

    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"])

    df["ts"] = pd.to_datetime(
        df["ts"], unit="ms", utc=True
    ).dt.tz_convert(tz)

    df = df.sort_values("ts").reset_index(drop=True)

    df["EMA12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["close"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()

    return df

# === 今日 Top3 ===
def update_today_top3():
    global today_top3_up, today_top3_down, today_date
    today = datetime.now(tz).date()
    if today_date == today:
        return

    today_date = today
    r = requests.get(
        "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
        timeout=10
    ).json()

    df = pd.DataFrame(r.get("data", []))
    df = df[df["instId"].str.endswith("USDT-SWAP")]
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    df["open24h"] = pd.to_numeric(df["open24h"], errors="coerce")
    df = df.dropna()
    df["pct"] = (df["last"] - df["open24h"]) / df["open24h"] * 100

    today_top3_up = (
        df.sort_values("pct", ascending=False)
        .head(3)["instId"].str.replace("-USDT-SWAP","").tolist()
    )
    today_top3_down = (
        df.sort_values("pct")
        .head(3)["instId"].str.replace("-USDT-SWAP","").tolist()
    )

# === 主策略 ===
def check_signals():
    cleanup_old()
    update_today_top3()

    symbols = list(set(["BTC","ETH","SOL","XRP"] + today_top3_up + today_top3_down))

    for bar in ["15m", "30m"]:
        for symbol in symbols:
            df = get_klines(symbol, bar)
            if df.empty or len(df) < 60:
                continue

            curr = df.iloc[-1]
            prev = df.iloc[-2]

            # ✅ 只在收盤
            if curr["confirm"] != "1":
                continue

            t = curr["ts"].strftime("%Y-%m-%d %H:%M")
            tag = ""
            if symbol in today_top3_up:
                tag = "【漲幅 Top3】"
            elif symbol in today_top3_down:
                tag = "【跌幅 Top3】"

            bull_key = f"{symbol}-{bar}-{t}-bull"
            bear_key = f"{symbol}-{bar}-{t}-bear"

            if (
                curr["EMA12"] > curr["EMA30"] > curr["EMA55"] and
                prev["close"] < prev["open"] and
                curr["close"] > curr["open"] and
                curr["close"] > prev["open"] and
                curr["open"] < prev["close"] and
                curr["low"] > curr["EMA55"] and
                bull_key not in sent_signals
            ):
                send_telegram(f"🟢 {symbol} {tag}\n[{bar}] 看漲吞沒\n收盤 {curr['close']} ({t})")
                sent_signals[bull_key] = datetime.utcnow()

            if (
                curr["EMA12"] < curr["EMA30"] < curr["EMA55"] and
                prev["close"] > prev["open"] and
                curr["close"] < curr["open"] and
                curr["close"] < prev["open"] and
                curr["open"] > prev["close"] and
                curr["high"] < curr["EMA55"] and
                bear_key not in sent_signals
            ):
                send_telegram(f"🔴 {symbol} {tag}\n[{bar}] 看跌吞沒\n收盤 {curr['close']} ({t})")
                sent_signals[bear_key] = datetime.utcnow()

    save_state()

# === Flask ===
@app.route("/")
def home():
    return "<h3>OKX EMA 吞沒策略（永續合約）運行中</h3>"

@app.route("/ping")
def ping():
    return "pong", 200

# === Scheduler ===
scheduler = BackgroundScheduler(timezone=tz)
scheduler.add_job(check_signals, "cron", minute="2,32")
scheduler.start()

# === 啟動 ===
load_state()
update_today_top3()

# 🚀 啟動訊息
send_telegram("🟢 OKX EMA 吞沒監控已啟動（永續合約 / 收盤確認）")

# 🔹 抓 Chat ID
fetch_chat_ids()  # 啟動時會列印 Bot 收到的訊息和 Chat ID

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
