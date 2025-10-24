from flask import Flask, render_template_string, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime, timedelta
import time
import os

app = Flask(__name__)

# === Telegram 設定 ===
TELEGRAM_BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
TELEGRAM_CHAT_ID = "1634751416"

# 固定監控三大幣
FIXED_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
top3_pairs = []
sent_signals = {}

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 抓取 K 線資料 ===
def get_klines(symbol, size=1500, retries=3):
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json().get('data', [])
            if not data:
                raise Exception("無 K 線資料")
            df = pd.DataFrame(data, columns=['ts','open','high','low','close','vol'])
            df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
            df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0.0)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒判斷 ===
def is_bullish_engulfing(df, idx=None):
    if idx is None: idx = len(df)-1
    if idx < 1: return False
    prev_open, prev_close = df['open'].iloc[idx-1], df['close'].iloc[idx-1]
    last_open, last_close = df['open'].iloc[idx], df['close'].iloc[idx]
    return prev_close < prev_open and last_close > last_open and last_close > prev_open and last_open < prev_close

def is_bearish_engulfing(df, idx=None):
    if idx is None: idx = len(df)-1
    if idx < 1: return False
    prev_open, prev_close = df['open'].iloc[idx-1], df['close'].iloc[idx-1]
    last_open, last_close = df['open'].iloc[idx], df['close'].iloc[idx]
    return prev_close > prev_open and last_close < last_open and last_close < prev_open and last_open > prev_close

# === Telegram 發訊 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok: print(f"Telegram 發送成功: {text}")
        else: print(f"Telegram 發送失敗: {r.text}")
    except Exception as e:
        print(f"Telegram 發送異常: {e}")

# === 更新 Top3 ===
def update_top3_by_24h():
    global top3_pairs
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 更新 Top3（24h 成交量）...")
    try:
        url = "https://api.bitunix.com/api/v1/market/symbols"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        instruments = resp.json().get('data', [])
        candidates = [inst['symbol'] for inst in instruments if inst.get('symbol','').endswith("USDT")]
    except Exception as e:
        print("無法取得合約清單:", e)
        return top3_pairs

    vols = []
    for s in candidates:
        try:
            df = get_klines(s, size=60)
            if len(df) >= 48:
                vol_sum = df['vol'].iloc[-48:].sum()
                vols.append((s, vol_sum))
        except: continue

    vols_sorted = sorted(vols, key=lambda x:x[1], reverse=True)
    top3_pairs = [t[0] for t in vols_sorted[:3]]
    print("今天 Top3:", top3_pairs)
    return top3_pairs

# === 即時訊號判斷 ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 檢查訊號...")
    cleanup_old_signals()
    monitored = list(dict.fromkeys(FIXED_PAIRS + top3_pairs))

    for symbol in monitored:
        try:
            df = get_klines(symbol, size=200)
            if len(df) < 60: continue

            ema12 = df['EMA12'].iloc[-1]
            ema30 = df['EMA30'].iloc[-1]
            ema55 = df['EMA55'].iloc[-1]
            close = df['close'].iloc[-1]
            low = df['low'].iloc[-1]
            high = df['high'].iloc[-1]
            candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
            base_key = f"{symbol}-{candle_time}"

            # 多頭排列
            if ema12 > ema30 > ema55:
                cond = (df['EMA12'] > df['EMA30']) & (df['EMA30'] > df['EMA55'])
                up_df = df[cond]
                if not up_df.empty:
                    start_idx = up_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['low'] <= df_after['EMA55']).any()
                    if low <= ema30 and low > ema55 and not touched_ema55:
                        if is_bullish_engulfing(df) and base_key+"-bull" not in sent_signals:
                            msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[base_key+"-bull"] = datetime.utcnow()

            # 空頭排列
            elif ema12 < ema30 < ema55:
                cond = (df['EMA12'] < df['EMA30']) & (df['EMA30'] < df['EMA55'])
                down_df = df[cond]
                if not down_df.empty:
                    start_idx = down_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['high'] >= df_after['EMA55']).any()
                    if high >= ema30 and high < ema55 and not touched_ema55:
                        if is_bearish_engulfing(df) and base_key+"-bear" not in sent_signals:
                            msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[base_key+"-bear"] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")

# === 回測模組（TP/SL 同根 K 線以 open 判斷） ===
def simulate_trade(df, entry_idx, direction):
    entry_price = df['close'].iloc[entry_idx]
    sl = df['EMA55'].iloc[entry_idx]
    if direction=="long":
        if sl >= entry_price: return {'result':'invalid','return_pct':0.0,'exit_idx':None}
        tp = entry_price + (entry_price-sl)*1.5
    else:
        if sl <= entry_price: return {'result':'invalid','return_pct':0.0,'exit_idx':None}
        tp = entry_price - (sl-entry_price)*1.5

    for idx in range(entry_idx+1,len(df)):
        o = df['open'].iloc[idx]; h=df['high'].iloc[idx]; l=df['low'].iloc[idx]
        # 用開盤價先判斷
        if direction=="long":
            if o>=tp: return {'result':'win','return_pct':(tp-entry_price)/entry_price,'exit_idx':idx}
            if o<=sl: return {'result':'loss','return_pct':(sl-entry_price)/entry_price,'exit_idx':idx}
        else:
            if o<=tp: return {'result':'win','return_pct':(entry_price-tp)/entry_price,'exit_idx':idx}
            if o>=sl: return {'result':'loss','return_pct':(entry_price-sl)/entry_price,'exit_idx':idx}
        # 再用 high/low 判斷
        if direction=="long":
            if h>=tp: return {'result':'win','return_pct':(tp-entry_price)/entry_price,'exit_idx':idx}
            if l<=sl: return {'result':'loss','return_pct':(sl-entry_price)/entry_price,'exit_idx':idx}
        else:
            if l<=tp: return {'result':'win','return_pct':(entry_price-tp)/entry_price,'exit_idx':idx}
            if h>=sl: return {'result':'loss','return_pct':(entry_price-sl)/entry_price,'exit_idx':idx}
    return {'result':'no_hit','return_pct':0.0,'exit_idx':None}

def backtest_symbol(symbol, days=30):
    size=days*48+200
    df=get_klines(symbol,size=size)
    results=[]
