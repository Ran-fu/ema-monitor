import os
import time
import requests
import pandas as pd
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from zoneinfo import ZoneInfo
import threading

app = Flask(__name__)
tz = ZoneInfo("Asia/Taipei")

# ==================== 配置 (已驗證) ====================
TELEGRAM_BOT_TOKEN = "8464878708:AAE4PmcsAa5Xk1g8w0eZb4o67wLPbNA885Q"
TELEGRAM_CHAT_ID = "1634751416"
sent_signals = {}

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if response.status_code != 200:
            print(f"Telegram API 錯誤: {response.text}")
    except Exception as e:
        print(f"發送訊息異常: {e}")

def fetch_klines(instId, bar="30m", limit=100):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": instId, "bar": bar, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        if not data: return None
        
        df = pd.DataFrame(data, columns=["ts","o","h","l","c","v","volCcy","vol","confirm"])
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
        df["ts"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(tz)
        return df.sort_values("ts").set_index("ts")
    except Exception as e:
        print(f"抓取 {instId} 數據失敗: {e}")
        return None

def check_signal(instId):
    df = fetch_klines(instId, "30m")
    if df is None or len(df) < 60: return
    
    df["E12"] = df["c"].ewm(span=12, adjust=False).mean()
    df["E30"] = df["c"].ewm(span=30, adjust=False).mean()
    df["E55"] = df["c"].ewm(span=55, adjust=False).mean()
    
    curr, prev = df.iloc[-2], df.iloc[-3]
    symbol = instId.replace("-USDT-SWAP", "")

    long_cond = (curr["E12"] > curr["E30"] > curr["E55"]) and \
                (curr["l"] <= curr["E30"] and curr["l"] > curr["E55"]) and \
                (curr["c"] > curr["o"] and prev["c"] < prev["o"] and curr["c"] >= prev["o"] and curr["o"] <= prev["c"])
    
    short_cond = (curr["E12"] < curr["E30"] < curr["E55"]) and \
                 (curr["h"] >= curr["E30"] and curr["h"] < curr["E55"]) and \
                 (curr["c"] < curr["o"] and prev["c"] > prev["o"] and curr["o"] >= prev["c"] and curr["c"] <= prev["o"])
    
    if not (long_cond or short_cond): return

    df4h = fetch_klines(instId, "4H", 60)
    if df4h is not None:
        e12_4h = df4h["c"].ewm(span=12, adjust=False).mean().iloc[-1]
        e55_4h = df4h["c"].ewm(span=55, adjust=False).mean().iloc[-1]
        if long_cond and not (e12_4h > e55_4h): return
        if short_cond and not (e12_4h < e55_4h): return

    key = f"{symbol}_{curr.name}"
    if key in sent_signals: return
    sent_signals[key] = True

    entry, sl = curr["c"], curr["E55"]
    risk = abs(entry - sl)
    tp1, tp2 = (entry + risk, entry + risk*1.5) if long_cond else (entry - risk, entry - risk*1.5)
    
    side = "🟢 多單 (Long)" if long_cond else "🔴 空單 (Short)"
    msg = (f"🎯 OKX 訊號: {symbol} {side}\n"
           f"━━━━━━━━━━━━━━\n"
           f"🔹 進場: {entry:.4f}\n"
           f"🔻 止損: {sl:.4f}\n"
           f"✅ TP1 (1:1): {tp1:.4f}\n"
           f"🚀 TP2 (1:1.5): {tp2:.4f}\n"
           f"━━━━━━━━━━━━━━\n"
           f"⏰ 時間: {curr.name.strftime('%m/%d %H:%M')}")
    send_telegram_message(msg)

def scan_all():
    print(f"[{datetime.now(tz)}] 開始 OKX 全幣種掃描...")
    try:
        url = "https://www.okx.com/api/v5/public/instruments"
        r = requests.get(url, params={"instType": "SWAP"}, timeout=15)
        symbols = [i["instId"] for i in r.json().get("data", []) if i["instId"].endswith("-USDT-SWAP")]
        
        for s in symbols:
            check_signal(s)
            time.sleep(0.1) 
        print(f"OKX 掃描完成。")
    except Exception as e:
        print(f"掃描出錯: {e}")

# ==================== 排程器邏輯 (移出 __main__) ====================
scheduler = BackgroundScheduler(timezone=tz)

def start_scheduler():
    # 如果已經啟動過就不再重複執行 (防止 Flask 重載導致多個排程器)
    if not scheduler.running:
        # 每小時檢查
        scheduler.add_job(
            lambda: send_telegram_message("✅ OKX 策略監控中 (系統正常運行)"), 
            "interval", 
            minutes=60,
            id="health_check"
        )
        # 定時掃描 (第 2 分鐘與第 32 分鐘)
        scheduler.add_job(
            scan_all, 
            "cron", 
            minute="2,32",
            id="main_scan"
        )
        scheduler.start()
        print("--- [APScheduler] 背景排程器已啟動 ---")
        # 啟動時先發一次確認訊息 (Debug 用)
        send_telegram_message("🚀 OKX 機器人已成功部署並啟動監控！")

# 立即執行啟動
start_scheduler()

# ==================== Flask 路由 ====================
@app.route("/")
def home():
    return f"OKX EMA Strategy Bot is Running. Server Time: {datetime.now(tz)}", 200

@app.route("/test")
def manual_test():
    threading.Thread(target=scan_all).start()
    return "已手動觸發背景掃描測試，請查看 Logs 或 TG 訊息。", 200

if __name__ == "__main__":
    # 本地測試時執行，Render 上若使用 gunicorn 則會跳過此段，但前面已經 start_scheduler 了
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
