import requests
import pandas as pd
from datetime import datetime, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 已發送訊號紀錄
sent_signals = {}

# 固定監控幣種
FIXED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# 清理舊訊號
def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# 取得 Bitunix K 線
def get_klines(symbol, size=100, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    for _ in range(retries):
        try:
            r = requests.get(url, timeout=10)
            data = r.json()['data']
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
            df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
    return None

# 吞沒判斷
def is_bullish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df):
    prev_open, prev_close = df['open'].iloc[-2], df['close'].iloc[-2]
    last_open, last_close = df['open'].iloc[-1], df['close'].iloc[-1]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# Telegram 發訊
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            print("✅", text)
        else:
            print("❌ Telegram 發送失敗:", r.text)
    except Exception as e:
        print("❌ Telegram 發送異常:", e)

# 取得成交量 Top3
def get_top3_volume_symbols():
    try:
        url = "https://api.bitunix.com/api/v1/market/tickers"
        r = requests.get(url, timeout=10)
        data = r.json()['data']
        df = pd.DataFrame(data)
        df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0)
        df_usdt = df[df['symbol'].str.endswith("USDT")]
        return df_usdt.nlargest(3, 'vol')['symbol'].tolist()
    except Exception as e:
        print("取得 Top3 交易量幣種失敗:", e)
        return []

# 核心策略
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 檢查訊號...")
    cleanup_old_signals()
    
    symbols = list(set(FIXED_SYMBOLS + get_top3_volume_symbols()))
    
    for symbol in symbols:
        df = get_klines(symbol)
        if df is None or len(df) < 60:
            continue
        
        ema12, ema30, ema55 = df['EMA12'].iloc[-1], df['EMA30'].iloc[-1], df['EMA55'].iloc[-1]
        low, high, close = df['low'].iloc[-1], df['high'].iloc[-1], df['close'].iloc[-1]
        candle_time = df['ts'].iloc[-1].strftime("%Y-%m-%d %H:%M")
        key_bull, key_bear = f"{symbol}-{candle_time}-bull", f"{symbol}-{candle_time}-bear"

        # 多頭排列
        if ema12 > ema30 > ema55 and low <= ema30 and low > ema55:
            if is_bullish_engulfing(df) and key_bull not in sent_signals:
                msg = f"🟢 {symbol} 看漲吞沒 收盤：{close:.4f} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[key_bull] = datetime.utcnow()

        # 空頭排列
        if ema12 < ema30 < ema55 and high >= ema30 and high < ema55:
            if is_bearish_engulfing(df) and key_bear not in sent_signals:
                msg = f"🔴 {symbol} 看跌吞沒 收盤：{close:.4f} ({candle_time})"
                send_telegram_message(msg)
                sent_signals[key_bear] = datetime.utcnow()

# 排程啟動
scheduler = BlockingScheduler(timezone="UTC")
scheduler.add_job(check_signals, 'cron', minute='2,32')
send_telegram_message("🚀 Bitunix EMA 吞沒 Worker 已啟動")
check_signals()
scheduler.start()
