from flask import Flask
import pandas as pd
import requests
import matplotlib.pyplot as plt
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
from binance.client import Client
import datetime
import time

app = Flask(__name__)

# ========== Telegram 設定 ==========
TELEGRAM_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"
CHAT_ID = "1634751416"

# ========== Binance API 設定 ==========
client = Client()

# ========== 全域變數防止重複通知 ==========
notified_symbols = {}

# ========== 判斷吞沒形態 ==========
def is_bullish_engulfing(df):
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (prev['close'] < prev['open']) and (curr['close'] > curr['open']) and (curr['close'] > prev['open']) and (curr['open'] < prev['close'])

# ========== 畫圖發送 ==========
def send_telegram_message_with_chart(symbol, df, signal_time):
    plt.figure(figsize=(10, 5))
    plt.plot(df['close'], label='Close', color='black')
    plt.plot(df['EMA12'], label='EMA12', color='blue')
    plt.plot(df['EMA30'], label='EMA30', color='green')
    plt.plot(df['EMA55'], label='EMA55', color='red')
    plt.title(f'{symbol} Signal @ {signal_time}')
    plt.legend()
    buf = BytesIO()
    plt.savefig(buf, format
