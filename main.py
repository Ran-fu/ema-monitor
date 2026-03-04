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

# Telegram 設定
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"

sent_signals = {}

# ================== 工具函數 ==================
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # 設定 timeout 防止 TG 伺服器延遲卡住主程式
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except:
        pass

def fetch_symbols():
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=10)
        data = r.json()
        # 取得所有 USDT 永續合約代碼
        return [i["instId"] for i in data.get("data", []) if i["instId"].endswith("-USDT-SWAP")]
    except:
        return []

def fetch_klines(instId, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": instId, "bar": bar, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","vol","x1","x2","x3"])
        # 時間轉換與格式化
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        return df.sort_values("ts").set_index("ts")
    except:
        return None

# ================== 策略邏輯 ==================
def check_signal(instId):
    df = fetch_klines(instId, "30m")
    if df is None or len(df) < 60: return
    
    # 計算 EMA 指標
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    curr, prev = df.iloc[-2], df.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    # 多空核心邏輯：EMA排列 + 回踩30均線 + 吞沒形態
    long_cond = (curr["E12"] > curr["E30"] > curr["E55"]) and (curr["l"] <= curr["E30"]) and (curr["c"] >= prev["o"])
    short_cond = (curr["E12"] < curr["E30"] < curr["E55"]) and (curr["h"] >= curr["E30"]) and (curr["c"] <= prev["o"])

    if not (long_cond or short_cond): return

    # 4H 大趨勢確認 (過濾震盪與逆勢單)
    df4h = fetch_klines(instId, "4H", 20)
    if df4h is not None:
        df4h["E12"] = df4h["c"].ewm(span=12, adjust=False).mean()
        df4h["E55"] = df4h["c"].ewm(span=55, adjust=False).mean()
        h4 = df4h.iloc[-1]
        if long_cond and not (h4["E12"] > h4["E55"]): return
        if short_cond and not (h4["E12"] < h4["E55"]): return

    # 檢查是否已針對此 K 線發送過訊號
    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    side = "多單 🟢" if long_cond else "空單 🔴"
    msg = (
        f"🚀 OKX 訊號: {symbol} {side}\n"
        f"進場參考: {curr['c']}\n"
        f"止損參考: {curr['E55']:.4f}\n"
        f"時間週期: 30m (4H已過濾)"
    )
    send_telegram_message(msg)

# ================== 排程工作 ==================
def scan_all():
    symbols = fetch_symbols()
    for s in symbols:
        try:
            check_signal(s)
            time.sleep(0.1) # 避免觸發 OKX API 限流
        except:
            continue

scheduler = BackgroundScheduler(timezone=tz)

# --- 時間改回原本設定：每 30 分鐘的第 2 分鐘執行 ---
scheduler.add_job(scan_all, "cron", minute="2,32")
# 每小時系統存活報告
scheduler.add_job(lambda: send_telegram_message("✅ 策略系統穩定監控中 (OKX)"), "interval", minutes=60)

scheduler.start()

# ================== Flask 服務 ==================
@app.route("/")
def home():
    return "OKX CTA Bot is Running"

if __name__ == "__main__":
    # 獲取 Port (Render 必備)
    port = int(os.environ.get("PORT", 5000))
    
    # 啟動時先在後台啟動一次初始掃描 (可選)，並發送啟動成功通知
    send_telegram_message("🤖 OKX 趨勢策略機器人部署成功！\n排程時間：每小時 02, 32 分。")
    
    app.run(host="0.0.0.0", port=port)
