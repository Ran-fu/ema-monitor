from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, os, json

app = Flask(__name__)

# === Telegram 設定（原本不動） ===
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1634751416")

# === 狀態（原本不動） ===
sent_signals = {}
today_top3_up = []
today_top3_down = []
today_date = None
STATE_FILE = "state.json"
tz = ZoneInfo("Asia/Taipei")

# === 狀態管理 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                sent_signals.update({
                    k: datetime.fromisoformat(v)
                    for k, v in data.get("sent_signals", {}).items()
                })
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

# === Telegram ===
def send_telegram_message(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
        if not r.ok:
            print("❌ TG Error:", r.text)
    except Exception as e:
        print("❌ TG Exception:", e)

# === 清理舊訊號 ===
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for k in list(sent_signals.keys()):
        if sent_signals[k] < cutoff:
            del sent_signals[k]

# === K 線 ===
def get_klines(symbol, bar="30m", retries=3):
    url = f"https://www.okx.com/api/v5/market/history-candles?instId={symbol}-USDT-SWAP&bar={bar}&limit=200"
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10).json()
            data = r.get("data", [])
            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data, columns=[
                'ts','open','high','low','close','vol','c1','c2','c3'
            ])
            df[['open','high','low','close','vol']] = df[
                ['open','high','low','close','vol']
            ].astype(float)

            df['ts'] = pd.to_datetime(df['ts'], unit='ms') \
                .dt.tz_localize('UTC').dt.tz_convert(tz)

            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except:
            time.sleep(1)
    return pd.DataFrame()

# === 反轉 K 線（強化版） ===
def check_reversal_pattern(df, idx, lookback=20):
    if idx - lookback < 0:
        return None

    row = df.iloc[idx]
    o, h, l, c = row['open'], row['high'], row['low'], row['close']
    body = abs(c - o)
    rng = h - l
    if rng == 0:
        return None

    upper = h - max(o, c)
    lower = min(o, c) - l

    is_high = h >= df['high'].iloc[idx-lookback:idx+1].max()
    is_low  = l <= df['low'].iloc[idx-lookback:idx+1].min()

    hammer = (
        body <= rng * 0.3 and
        lower >= body * 2 and
        upper <= body
    )

    gravestone = (
        body <= rng * 0.1 and
        upper >= rng * 0.6 and
        lower <= body
    )

    if is_low and hammer:
        return {"type": "LOW", "color": "bull" if c > o else "bear"}
    if is_high and gravestone:
        return {"type": "HIGH", "color": "bear" if c < o else "bull"}
    return None

# === 結構 & 回踩（新增，不影響原設定） ===
def get_prev_swing_low(df, idx, lookback=20):
    return df['low'].iloc[idx-lookback:idx].min()

def get_prev_swing_high(df, idx, lookback=20):
    return df['high'].iloc[idx-lookback:idx].max()

def is_first_pullback(df, idx, ema_col="EMA30", lookback=20):
    recent = df.iloc[idx-lookback:idx]
    touched = (
        (recent['low'] <= recent[ema_col]) &
        (recent['high'] >= recent[ema_col])
    )
    return touched.sum() <= 1

# === Top3（原本不動） ===
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
    df = df[df['instId'].str.endswith("USDT-SWAP")]
    df['last'] = pd.to_numeric(df['last'])
    df['open24h'] = pd.to_numeric(df['open24h'])
    df['change'] = (df['last'] - df['open24h']) / df['open24h'] * 100

    main = ["BTC","ETH","SOL","XRP"]
    df['sym'] = df['instId'].str.replace("-USDT-SWAP","")

    today_top3_up = df[~df['sym'].isin(main)] \
        .sort_values('change', ascending=False).head(3)['sym'].tolist()

    today_top3_down = df[~df['sym'].isin(main)] \
        .sort_values('change').head(3)['sym'].tolist()

# === 核心檢查 ===
def check_signals():
    cleanup_old_signals()
    update_today_top3()

    symbols = list(set(["BTC","ETH","SOL","XRP"] + today_top3_up + today_top3_down))

    for bar in ["15m", "30m"]:
        for symbol in symbols:
            df = get_klines(symbol, bar)
            if df.empty or len(df) < 80:
                continue

            rev_idx = len(df) - 2
            confirm_idx = len(df) - 1

            o2, c2 = df['open'].iloc[confirm_idx], df['close'].iloc[confirm_idx]
            ema12 = df['EMA12'].iloc[rev_idx]
            ema30 = df['EMA30'].iloc[rev_idx]
            ema55 = df['EMA55'].iloc[rev_idx]

            candle_time = df['ts'].iloc[rev_idx].strftime('%Y-%m-%d %H:%M')

            # === EMA 趨勢 ===
            is_bull = ema12 > ema30 > ema55
            is_bear = ema12 < ema30 < ema55
            if not (is_bull or is_bear):
                continue

            # === EMA 強度（Top3 放寬） ===
            ema_gap = abs(ema12 - ema55) / ema55
            if symbol in today_top3_up + today_top3_down:
                if ema_gap < 0.002:
                    continue
            else:
                if ema_gap < 0.003:
                    continue

            # === 第一次回踩 ===
            if not is_first_pullback(df, rev_idx):
                continue

            # === 反轉 K 線 ===
            rev = check_reversal_pattern(df, rev_idx)
            if not rev:
                continue

            # === 結構過濾 ===
            if rev["type"] == "LOW":
                if not is_bull:
                    continue
                if df['low'].iloc[rev_idx] <= get_prev_swing_low(df, rev_idx):
                    continue

            if rev["type"] == "HIGH":
                if not is_bear:
                    continue
                if df['high'].iloc[rev_idx] >= get_prev_swing_high(df, rev_idx):
                    continue

            # === 第二根確認 ===
            second_color = "bull" if c2 > o2 else "bear"
            if second_color != rev["color"]:
                continue

            key = f"{symbol}-{bar}-{candle_time}-REV-{rev['type']}"
            if key in sent_signals:
                continue

            prefix = "🔥 " if symbol in today_top3_up + today_top3_down else ""
            msg = (
                f"{prefix}{symbol} [{bar}]\n"
                f"EMA 順勢・第一次回踩反轉\n"
                f"結構確認 + 第二根收盤\n"
                f"Close: {c2}\n"
                f"{candle_time}"
            )
            send_telegram_message(msg)
            sent_signals[key] = datetime.utcnow()

    save_state()

# === Flask ===
@app.route("/")
def home():
    return "OKX EMA Monitor Running"

@app.route("/ping")
def ping():
    return "pong", 200

scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, "cron", minute="2,17,32,47")
scheduler.start()

load_state()
update_today_top3()
send_telegram_message("🚀 OKX EMA 吞沒 + 順勢反轉（完整版）已啟動")
check_signals()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
