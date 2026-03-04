from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import os

# ================== 核心設定 ==================
app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# 你提供的設定
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

# Bitunix 設定與偽裝 Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Content-Type": "application/json"
}
BASE_URL = "https://api.bitunix.com"

sent_signals = {}

# ================== Telegram 修復與診斷 ==================
def send_telegram_message(text):
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    payload = {"chat_id": chat_id, "text": text}
    
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code == 200:
            print(f"✅ TG 發送成功")
            return True
        else:
            # 這裡會印出失敗原因，例如：{"ok":false,"error_code":401,"description":"Unauthorized"}
            print(f"❌ TG API 錯誤: {r.text}")
            return False
    except Exception as e:
        print(f"❌ TG 連線失敗 (可能是伺服器網路環境限制): {e}")
        return False

# ================== Bitunix 數據抓取 ==================
def fetch_symbols():
    try:
        url = f"{BASE_URL}/api/v1/futures/common/symbols"
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        if data.get("code") == 0:
            return [i["base_currency"] for i in data.get("data", []) if i["quote_currency"] == "USDT"]
        return []
    except:
        return []

def fetch_klines(symbol, bar="30m", limit=100):
    try:
        url = f"{BASE_URL}/api/v1/futures/market/candles"
        interval_map = {"30m": "30", "4H": "240"}
        params = {
            "symbol": f"{symbol}USDT",
            "interval": interval_map.get(bar, "30"),
            "limit": str(limit)
        }
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        res = r.json()
        if res.get("code") != 0 or not res.get("data"): return None
        
        df = pd.DataFrame(res["data"], columns=["ts","o","h","l","c","vol","amt"])
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        df = df.sort_values("ts").set_index("ts")
        return df
    except:
        return None

# ================== 策略運算 ==================
def add_ema(df):
    df["EMA12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["EMA30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["EMA55"] = df["c"].ewm(span=55, adjust=False).mean()
    return df

def check_signal(symbol):
    df_30m = fetch_klines(symbol, bar="30m")
    if df_30m is None or len(df_30m) < 60: return
    df_30m = add_ema(df_30m)

    curr = df_30m.iloc[-2]
    prev = df_30m.iloc[-3]

    # 策略邏輯
    bull_trend = curr["EMA12"] > curr["EMA30"] > curr["EMA55"]
    bear_trend = curr["EMA12"] < curr["EMA30"] < curr["EMA55"]
    bull_pullback = curr["l"] <= curr["EMA30"] and curr["c"] > curr["EMA30"]
    bear_pullback = curr["h"] >= curr["EMA30"] and curr["c"] < curr["EMA30"]
    
    # 吞沒
    is_bull_engulf = curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"]
    is_bear_engulf = curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["c"] <= prev["o"]
    
    vol_confirm = curr["vol"] > prev["vol"]

    long_signal = bull_trend and bull_pullback and is_bull_engulf and vol_confirm
    short_signal = bear_trend and bear_pullback and is_bear_engulf and vol_confirm

    if not (long_signal or short_signal): return

    # 4H 過濾
    df_4h = fetch_klines(symbol, bar="4H", limit=20)
    if df_4h is not None:
        df_4h = add_ema(df_4h)
        h4_curr = df_4h.iloc[-1]
        if long_signal and not (h4_curr["EMA12"] > h4_curr["EMA55"]): return
        if short_signal and not (h4_curr["EMA12"] < h4_curr["EMA55"]): return

    # 避免重複發送
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    side = "多單 🟢" if long_signal else "空單 🔴"
    msg = (
        f"🎯 Bitunix 訊號: {symbol} {side}\n"
        f"進場價: {curr['c']:.4f}\n"
        f"止損: {curr['EMA55']:.4f}\n"
        f"時間: {curr.name.strftime('%H:%M')}"
    )
    send_telegram_message(msg)

# ================== 排程工作 ==================
def scan_all():
    symbols = fetch_symbols()
    for s in symbols:
        try:
            check_signal(s)
            time.sleep(0.3) # Bitunix 頻率限制較嚴，建議設為 0.3
        except:
            continue

def ping_system():
    now = datetime.now(tz).strftime("%H:%M")
    send_telegram_message(f"✅ 系統在線\n最後掃描: {now}")

scheduler = BackgroundScheduler(timezone=tz)
# 改為收盤後 15 秒掃描，大幅減少滯後
scheduler.add_job(scan_all, "cron", minute="0,30", second="15")
scheduler.add_job(ping_system, "interval", minutes=60)
scheduler.start()

# ================== 運行 ==================
@app.route("/")
def home(): return "Bitunix Bot Running ✅"

if __name__ == "__main__":
    # 啟動時立即測試一次 TG
    print("--- 啟動診斷 ---")
    if send_telegram_message("🤖 策略機器人已啟動"):
        print("Telegram 測試通過")
    else:
        print("Telegram 測試失敗，請檢查控制台錯誤訊息")
    
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
