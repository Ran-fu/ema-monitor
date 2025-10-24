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

# 已發送過的訊號記錄（包含時間）
sent_signals = {}

def cleanup_old_signals(hours=6):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    keys_to_delete = [key for key, ts in sent_signals.items() if ts < cutoff]
    for key in keys_to_delete:
        del sent_signals[key]

# === 抓取 Bitunix 30m K 線資料（可指定 size） ===
def get_klines(symbol, size=1500, retries=3):
    # size 預設給足夠的 candles（30 天約 48*30=1440 根）
    url = f'https://api.bitunix.com/api/v1/market/candles?symbol={symbol}&period=30min&size={size}'
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json().get('data', [])
            if not data:
                raise Exception("無 K 線資料")
            df = pd.DataFrame(data, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.iloc[::-1].reset_index(drop=True)  # 時間正序
            df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['EMA30'] = df['close'].ewm(span=30, adjust=False).mean()
            df['EMA55'] = df['close'].ewm(span=55, adjust=False).mean()
            return df
        except Exception as e:
            print(f"[{symbol}] 抓取失敗：{e}")
            time.sleep(1)
    raise Exception(f"{symbol} 多次抓取失敗")

# === 吞沒形態判斷 ===
def is_bullish_engulfing(df, idx=None):
    # idx 指定要檢查哪根 candle（預設最後一根）
    if idx is None:
        idx = len(df) - 1
    if idx < 1:
        return False
    prev_open, prev_close = df['open'].iloc[idx-1], df['close'].iloc[idx-1]
    last_open, last_close = df['open'].iloc[idx], df['close'].iloc[idx]
    return (prev_close < prev_open) and (last_close > last_open) and (last_close > prev_open) and (last_open < prev_close)

def is_bearish_engulfing(df, idx=None):
    if idx is None:
        idx = len(df) - 1
    if idx < 1:
        return False
    prev_open, prev_close = df['open'].iloc[idx-1], df['close'].iloc[idx-1]
    last_open, last_close = df['open'].iloc[idx], df['close'].iloc[idx]
    return (prev_close > prev_open) and (last_close < last_open) and (last_close < prev_open) and (last_open > prev_close)

# === 傳送 Telegram 訊息 ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"Telegram 發送失敗: {response.text}")
        else:
            print(f"Telegram 發送成功: {text}")
    except Exception as e:
        print(f"Telegram 發送異常：{e}")

# === EMA 策略邏輯（即時檢查） ===
def check_signals():
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查訊號...")
    cleanup_old_signals()

    # 取得 Bitunix 所有交易對
    try:
        url = "https://api.bitunix.com/api/v1/market/symbols"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        instruments = response.json().get('data', [])
        usdt_pairs = [inst['symbol'] for inst in instruments if inst.get('symbol', '').endswith("USDT")]
    except Exception as e:
        print(f"無法取得合約清單：{e}")
        return

    for symbol in usdt_pairs:
        try:
            df = get_klines(symbol, size=200)
            if len(df) < 60:
                continue

            ema12 = df['EMA12'].iloc[-1]
            ema30 = df['EMA30'].iloc[-1]
            ema55 = df['EMA55'].iloc[-1]
            close = df['close'].iloc[-1]
            low = df['low'].iloc[-1]
            high = df['high'].iloc[-1]

            candle_time = df['ts'].iloc[-1].floor('30T').strftime('%Y-%m-%d %H:%M')
            # direction will be appended for uniqueness
            base_key = f"{symbol}-{candle_time}"

            # === 多頭排列 ===
            if ema12 > ema30 > ema55:
                cond = (df['EMA12'] > df['EMA30']) & (df['EMA30'] > df['EMA55'])
                up_df = df[cond]
                if not up_df.empty:
                    start_idx = up_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['low'] <= df_after['EMA55']).any()

                    if (low <= ema30 and low > ema55) and not touched_ema55:
                        if is_bullish_engulfing(df) and base_key + "-bull" not in sent_signals:
                            msg = f"🟢 {symbol}\n看漲吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[base_key + "-bull"] = datetime.utcnow()

            # === 空頭排列 ===
            elif ema12 < ema30 < ema55:
                cond = (df['EMA12'] < df['EMA30']) & (df['EMA30'] < df['EMA55'])
                down_df = df[cond]
                if not down_df.empty:
                    start_idx = down_df.index[0]
                    df_after = df.loc[start_idx:]
                    touched_ema55 = (df_after['high'] >= df_after['EMA55']).any()

                    if (high >= ema30 and high < ema55) and not touched_ema55:
                        if is_bearish_engulfing(df) and base_key + "-bear" not in sent_signals:
                            msg = f"🔴 {symbol}\n看跌吞沒，收盤：{close:.4f} ({candle_time})"
                            send_telegram_message(msg)
                            sent_signals[base_key + "-bear"] = datetime.utcnow()

        except Exception as e:
            print(f"{symbol} 發生錯誤：{e}")
            continue

# === 回測模組（以相同策略規則計算勝率） ===
def simulate_trade(df, entry_idx, direction):
    """
    direction: "long" or "short"
    entry_idx: index of the signal candle (we enter at close of that candle)
    SL: EMA55 at entry candle
    TP: entry + (entry - SL)*1.5 for long. (mirror for short)
    Returns: dict { 'result': 'win'/'loss'/'no_hit', 'return_pct': float, 'exit_idx': idx or None }
    """
    entry_price = df['close'].iloc[entry_idx]
    sl = df['EMA55'].iloc[entry_idx]
    if direction == "long":
        # if SL >= entry_price then bad setup (skip)
        if sl >= entry_price:
            return {'result': 'invalid', 'return_pct': 0.0, 'exit_idx': None}
        tp = entry_price + (entry_price - sl) * 1.5
    else:  # short
        if sl <= entry_price:
            return {'result': 'invalid', 'return_pct': 0.0, 'exit_idx': None}
        tp = entry_price - (sl - entry_price) * 1.5

    # scan forward for next candles to see TP/SL hit
    for idx in range(entry_idx+1, len(df)):
        o = df['open'].iloc[idx]
        h = df['high'].iloc[idx]
        l = df['low'].iloc[idx]

        # Check open first (more realistic)
        if direction == "long":
            if o >= tp:
                return {'result': 'win', 'return_pct': (tp - entry_price) / entry_price, 'exit_idx': idx}
            if o <= sl:
                return {'result': 'loss', 'return_pct': (sl - entry_price) / entry_price, 'exit_idx': idx}
            # then check within-candle extremes
            if h >= tp and l <= sl:
                # ambiguous: conservative treat as loss
                return {'result': 'loss', 'return_pct': (sl - entry_price) / entry_price, 'exit_idx': idx}
            if h >= tp:
                return {'result': 'win', 'return_pct': (tp - entry_price) / entry_price, 'exit_idx': idx}
            if l <= sl:
                return {'result': 'loss', 'return_pct': (sl - entry_price) / entry_price, 'exit_idx': idx}
        else:  # short
            if o <= tp:
                return {'result': 'win', 'return_pct': (entry_price - tp) / entry_price, 'exit_idx': idx}
            if o >= sl:
                return {'result': 'loss', 'return_pct': (entry_price - sl) / entry_price, 'exit_idx': idx}
            if h >= sl and l <= tp:
                return {'result': 'loss', 'return_pct': (entry_price - sl) / entry_price, 'exit_idx': idx}
            if l <= tp:
                return {'result': 'win', 'return_pct': (entry_price - tp) / entry_price, 'exit_idx': idx}
            if h >= sl:
                return {'result': 'loss', 'return_pct': (entry_price - sl) / entry_price, 'exit_idx': idx}

    # If neither hit in available data
    return {'result': 'no_hit', 'return_pct': 0.0, 'exit_idx': None}

def backtest_symbol(symbol, days=30):
    # 30 分線數 = days * 48 (24h*2)
    size = days * 48 + 200  # 多抓一些 cushion
    df = get_klines(symbol, size=size)

    results = []
    # we need at least 60 candles for ema calc & pattern detection
    for idx in range(60, len(df)):
        # align time key for uniqueness if needed
        ema12 = df['EMA12'].iloc[idx]
        ema30 = df['EMA30'].iloc[idx]
        ema55 = df['EMA55'].iloc[idx]
        low = df['low'].iloc[idx]
        high = df['high'].iloc[idx]

        # check multiday condition: find earliest index in current contiguous multi-EMA region
        # for simpler approach, replicate same logic as live for the candle at idx
        # 多頭
        if ema12 > ema30 > ema55:
            # find contiguous start index where EMA condition started
            cond = (df['EMA12'] > df['EMA30']) & (df['EMA30'] > df['EMA55'])
            up_df = df[cond]
            if up_df.empty:
                continue
            start_idx = up_df.index[0]
            df_after = df.loc[start_idx:idx]  # up to current idx
            touched_ema55 = (df_after['low'] <= df_after['EMA55']).any()
            if (low <= ema30 and low > ema55) and not touched_ema55:
                # engulfing?
                if is_bullish_engulfing(df, idx=idx):
                    # simulate trade
                    sim = simulate_trade(df, idx, "long")
                    if sim['result'] != 'invalid':
                        results.append(sim)
        # 空頭
        elif ema12 < ema30 < ema55:
            cond = (df['EMA12'] < df['EMA30']) & (df['EMA30'] < df['EMA55'])
            down_df = df[cond]
            if down_df.empty:
                continue
            start_idx = down_df.index[0]
            df_after = df.loc[start_idx:idx]
            touched_ema55 = (df_after['high'] >= df_after['EMA55']).any()
            if (high >= ema30 and high < ema55) and not touched_ema55:
                if is_bearish_engulfing(df, idx=idx):
                    sim = simulate_trade(df, idx, "short")
                    if sim['result'] != 'invalid':
                        results.append(sim)

    # calculate stats
    wins = sum(1 for r in results if r['result'] == 'win')
    losses = sum(1 for r in results if r['result'] == 'loss')
    no_hits = sum(1 for r in results if r['result'] == 'no_hit')
    trades = wins + losses  # only counted trades with decisive outcome
    total_signals = len(results)

    avg_return = 0.0
    if trades > 0:
        avg_return = sum(r['return_pct'] for r in results if r['result'] in ('win','loss')) / trades

    win_rate = (wins / trades * 100) if trades > 0 else 0.0

    return {
        'symbol': symbol,
        'total_signals': total_signals,
        'trades': trades,
        'wins': wins,
        'losses': losses,
        'no_hits': no_hits,
        'win_rate': round(win_rate, 2),
        'avg_return_pct': round(avg_return * 100, 2)
    }

def backtest_all(days=30, symbols=None):
    # default symbols: BTC/ETH/SOL USDT
    if symbols is None:
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    summaries = []
    for s in symbols:
        try:
            print(f"Backtesting {s} for {days} days...")
            res = backtest_symbol(s, days=days)
            summaries.append(res)
        except Exception as e:
            print(f"{s} 回測失敗：{e}")
            summaries.append({'symbol': s, 'error': str(e)})

    # build summary text
    lines = [f"📊 回測結果（過去 {days} 天，30 分線）\n"]
    for r in summaries:
        if 'error' in r:
            lines.append(f"{r['symbol']}: 回測失敗：{r['error']}")
        else:
            lines.append(
                f"{r['symbol']}: 總訊號 {r['total_signals']}, 決定性交易 {r['trades']}, 勝 {r['wins']}, 敗 {r['losses']}, 無結果 {r['no_hits']}, 勝率 {r['win_rate']}%, 平均報酬 {r['avg_return_pct']}%"
            )
    text = "\n".join(lines)
    send_telegram_message(text)
    return summaries

# === Flask 網頁 ===
@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>EMA 吞沒策略</title>
        <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    </head>
    <body>
        <h1>🚀 EMA 吞沒策略伺服器運行中 (Bitunix)</h1>
        <p>最後啟動時間：{{now}}</p>
    </body>
    </html>
    """, now=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'))

@app.route('/ping')
def ping():
    return 'pong'

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

# === 啟動伺服器與排程 ===
scheduler = BackgroundScheduler()
# 即時檢查：同你原本設定（每小時的 2 分和 32 分）
scheduler.add_job(check_signals, 'cron', minute='2,32')
# 每日回測（假設伺服器為 UTC，UTC 00:00 = 臺灣 08:00）
scheduler.add_job(lambda: backtest_all(days=30), 'cron', hour=0, minute=0)
scheduler.start()

# 啟動時：先發送啟動訊息，接著執行一次回測，再執行一次檢查
send_telegram_message("🚀 Bitunix EMA 吞沒監控已啟動 - (含 30 天回測排程，每日 08:00 本地時間發送)")
try:
    # 執行一次回測並發送結果
    backtest_all(days=30)
except Exception as e:
    print("啟動回測發生錯誤：", e)

# 執行一次即時檢查
check_signals()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
