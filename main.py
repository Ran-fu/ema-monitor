from flask import Flask, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time, os, json

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# === 狀態記錄 ===
sent_signals = {}
top3_up, top3_down = [], []
today_date = None
last_check_time = None
last_timezone_check = None
STATE_FILE = "state.json"

# === 狀態管理 ===
def load_state():
    global sent_signals, today_date
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                s = data.get("sent_signals", {})
                # 可能是空 dict
                for k, v in s.items():
                    try:
                        sent_signals[k] = datetime.fromisoformat(v)
                    except Exception:
                        # 若解析失敗跳過
                        pass
                td = data.get("today_date")
                if td and td != "None":
                    try:
                        today_date = datetime.fromisoformat(td).date()
                    except:
                        today_date = None
            print("🧩 狀態已載入")
        except Exception as e:
            print("⚠️ 狀態載入失敗:", e)

def save_state():
    try:
        tmp_file = STATE_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump({
                "sent_signals": {k: v.isoformat() for k, v in sent_signals.items()},
                "today_date": str(today_date)
            }, f)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        print("⚠️ 狀態保存失敗:", e)

# === Telegram 發送 ===
def send_telegram_message(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.ok:
            print(f"✅ 發送訊息: {text}")
        else:
            print(f"❌ Telegram 發送失敗: {r.status_code} {r.text}")
    except Exception as e:
        print(f"❌ Telegram 發送異常: {e}")

# === 清理舊訊號 ===
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [k for k, ts in sent_signals.items() if ts < cutoff]
    for k in keys_to_delete:
        del sent_signals[k]

# === 抓取 K 線資料（幣安合約） ===
BASE_API = "https://fapi.binance.com"

def get_klines(symbol, bar="30m", retries=3):
    """
    symbol: base symbol, e.g. "BTC" or "BTCUSDT" (we will normalize)
    bar: "15m" or "30m"
    returns DataFrame ordered oldest -> newest (binance default)
    """
    # normalize symbol to base like "BTC"
    sym = symbol.upper().replace("USDT", "")
    interval_map = {"15m":"15m", "30m":"30m"}
    interval = interval_map.get(bar, "30m")
    url = f"{BASE_API}/fapi/v1/klines?symbol={sym}USDT&interval={interval}&limit=200"
    for attempt in range(1, retries + 1):
        try:
            print(f"[DEBUG] Requesting Klines: {url} (attempt {attempt})")
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                print(f"[WARN] Klines HTTP {resp.status_code} for {sym}: {resp.text[:300]}")
                time.sleep(1)
                continue
            data = resp.json()
            if not data:
                print(f"[{sym}] 無資料")
                return pd.DataFrame()
            # Binance returns list of lists
            df = pd.DataFrame(data, columns=[
                'open_time','open','high','low','close','vol','close_time','quote_asset_vol',
                'trades','taker_buy_base','taker_buy_quote','ignore'
            ])
            # convert types
            df[['open','high','low','close','vol']] = df[['open','high','low','close','vol']].astype(float)
            # open_time in ms
            df['ts'] = pd.to_datetime(df['open_time'], unit='ms', utc=True).dt.tz_convert('Asia/Taipei')
            # Keep order oldest -> newest (binance default), so last row is newest closed candle
            df = df.reset_index(drop=True)
            # compute EMAs
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{sym}] 抓取失敗 (attempt {attempt}): {e}")
            time.sleep(1)
    return pd.DataFrame()

# === 今日漲跌幅 Top3 ===
def update_today_top3():
    global top3_up, top3_down, today_date
    now_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if today_date != now_date:
        today_date = now_date
        try:
            url = f"{BASE_API}/fapi/v1/ticker/24hr"
            print(f"[DEBUG] Requesting Top3: {url}")
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                print(f"[WARN] Top3 HTTP {resp.status_code}: {resp.text[:300]}")
                return
            data = resp.json()
            # ensure data is list-like
            if isinstance(data, dict):
                # API returned an object (error?) – log and exit
                print("[WARN] Top3 API returned dict, skipping:", data)
                return
            df = pd.DataFrame(data)
            if df.empty:
                print("⚠️ Top3 API 回傳空資料")
                return
            # keep only USDT pairs
            df = df[df['symbol'].astype(str).str.endswith("USDT")].copy()
            df['priceChangePercent'] = pd.to_numeric(df['priceChangePercent'], errors='coerce')
            df = df.dropna(subset=['priceChangePercent'])
            if df.empty:
                print("⚠️ Top3: no valid priceChangePercent")
                return
            df_up = df.sort_values('priceChangePercent', ascending=False)
            df_down = df.sort_values('priceChangePercent', ascending=True)
            top3_up = df_up['symbol'].head(3).tolist()
            top3_down = df_down['symbol'].head(3).tolist()
            print(f"📈 漲幅前三: {top3_up}")
            print(f"📉 跌幅前三: {top3_down}")
            # 發送 Top3 訊息（條件：非空）
            if top3_up or top3_down:
                msg_top3 = "📈 今日漲幅前三：\n" + ("\n".join(top3_up) if top3_up else "無") \
                           + "\n\n📉 今日跌幅前三：\n" + ("\n".join(top3_down) if top3_down else "無")
                send_telegram_message(msg_top3)
        except Exception as e:
            print("⚠️ 更新 Top3 失敗:", e)

# === 每日清空訊號 ===
def daily_reset():
    global sent_signals
    sent_signals.clear()
    print("🧹 每日訊號已清空")
    update_today_top3()
    save_state()
    send_telegram_message("🧹 今日訊號已清空，Top3 已更新")

# === 檢查吞沒訊號（以收盤K線為準） ===
def check_signals():
    global last_check_time
    cleanup_old_signals()
    update_today_top3()

    main_symbols = ["BTC","ETH","SOL","XRP"]
    # top3 lists from API are like "BTCUSDT" -> we will handle normalization later
    watch_symbols = list(set(main_symbols + top3_up + top3_down))

    for bar in ["15m", "30m"]:
        for symbol in watch_symbols:
            # normalize to base symbol like "BTC"
            sym = str(symbol).upper().replace("USDT", "")
            df = get_klines(sym, bar=bar)
            if df.empty or len(df) < 3:
                continue

            # last row is the latest closed candle (oldest->newest order)
            prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
            open_, close_, high_, low_ = df['open'].iloc[-1], df['close'].iloc[-1], df['high'].iloc[-1], df['low'].iloc[-1]
            ema12, ema30, ema55 = df['EMA12'].iloc[-1], df['EMA30'].iloc[-1], df['EMA55'].iloc[-1]
            candle_time = df['ts'].iloc[-1].strftime('%Y-%m-%d %H:%M')
            bull_key = f"{sym}-{bar}-{candle_time}-bull"
            bear_key = f"{sym}-{bar}-{candle_time}-bear"

            # 判斷 Top3（注意： top3_up/down 裡面的元素可能是 "BTCUSDT"）
            prefix = "🟢"
            if (sym + "USDT") in top3_up:
                prefix = "🔥 漲幅Top3 "
            elif (sym + "USDT") in top3_down:
                prefix = "⚡ 跌幅Top3 "

            # === 看漲吞沒（碰或跌破 EMA30 未碰 EMA55） ===
            ema30_touch = (low_ <= ema30 <= high_)
            ema55_not_touch = (low_ > ema55)
            if ema12 > ema30 > ema55 and ema30_touch and ema55_not_touch and \
               prev_close < prev_open and close_ > open_ and close_ > prev_open and open_ < prev_close and \
               (bull_key not in sent_signals):
                msg = f"{prefix}{sym} [{bar}]\n看漲吞沒（收盤K線確認）\n碰或跌破 EMA30 未碰 EMA55\n收盤: {close_} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bull_key] = datetime.utcnow()

            # === 看跌吞沒（碰或突破 EMA30 未碰 EMA55） ===
            ema30_touch = (low_ <= ema30 <= high_)
            ema55_not_touch = (high_ < ema55)
            if ema12 < ema30 < ema55 and ema30_touch and ema55_not_touch and \
               prev_close > prev_open and close_ < open_ and close_ < prev_open and open_ > prev_close and \
               (bear_key not in sent_signals):
                msg = f"{prefix}{sym} [{bar}]\n看跌吞沒（收盤K線確認）\n碰或突破 EMA30 未碰 EMA55\n收盤: {close_} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[bear_key] = datetime.utcnow()

    last_check_time = datetime.utcnow()
    save_state()

# === 掉線偵測 ===
def check_health():
    global last_check_time
    now = datetime.utcnow()
    if last_check_time is None:
        last_check_time = now
        return
    diff = (now - last_check_time).total_seconds() / 60
    if diff > 60:
        send_telegram_message(f"⚠️ 系統可能掉線或延遲運行\n最後檢查時間：{last_check_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        last_check_time = now

# === 時區監測（台灣時間） ===
def check_timezone():
    global last_timezone_check
    try:
        tz_taipei = ZoneInfo("Asia/Taipei")
        tz_utc = ZoneInfo("UTC")
        taiwan_now = datetime.now(tz_taipei)
        utc_now = datetime.now(tz_utc)
        # convert UTC to Taipei for accurate comparison
        expected_taiwan = utc_now.astimezone(tz_taipei)
        diff = abs((taiwan_now - expected_taiwan).total_seconds()) / 60
        if diff > 5:
            send_telegram_message(f"⚠️ 時區異常偵測：與台灣時間偏差 {diff:.1f} 分鐘")
        last_timezone_check = taiwan_now
        print(f"🕓 時區檢查完成：{taiwan_now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8) diff={diff:.1f}min")
    except Exception as e:
        print("⚠️ 時區檢查失敗:", e)

# === Flask 頁面 ===
@app.route('/')
def home():
    up_text = ", ".join(top3_up) if top3_up else "尚未更新"
    down_text = ", ".join(top3_down) if top3_down else "尚未更新"
    return render_template_string(f"""
        <h1>🚀 幣安 EMA 吞沒策略運行中 ✅</h1>
        <p>📈 今日漲幅前三：{up_text}</p>
        <p>📉 今日跌幅前三：{down_text}</p>
        <p>🕒 上次檢查時間：{last_check_time}</p>
        <p>🌏 最近時區檢查：{last_timezone_check}</p>
    """)

@app.route('/ping')
def ping():
    return 'pong', 200

# === 排程設定 ===
scheduler = BackgroundScheduler()
scheduler.add_job(check_signals, 'cron', minute='2,32')   # 在每根 30m 收盤後 2 分、32 分判斷
scheduler.add_job(check_health, 'interval', minutes=10)
scheduler.add_job(check_timezone, 'interval', minutes=15)
scheduler.add_job(daily_reset, 'cron', hour=0, minute=0)
scheduler.start()

# === 啟動立即執行 ===
load_state()
update_today_top3()
send_telegram_message("🚀 幣安 EMA 吞沒監控已啟動 ✅\n(以收盤K線判斷吞沒)\n" +
                      ("今日 Top3 漲幅: " + ", ".join(top3_up) if top3_up else "無 Top3"))
check_signals()
check_timezone()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    # gunicorn will invoke main:app so this block not used in Render, but kept for local run
    app.run(host='0.0.0.0', port=port)
