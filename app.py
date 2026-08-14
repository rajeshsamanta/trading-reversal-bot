import os
import requests
import pandas as pd
import asyncio
import time
import schedule
from datetime import datetime, timedelta
from flask import Flask
from telegram import Bot

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = "8776819788:AAHfoFM_82byoGtR3q6jB0PKHw5S45GBqJI"
CHAT_ID = "-1003988993524"

SYMBOL = "BTC-USDT-SWAP"
TIMEFRAME = "15m"
LOWER_TF = "1m"
LIMIT = 100
VOLUME_LOOKBACK = 50
OI_LOOKBACK = 42

REVERSAL_IMPULSE_LOOKBACK = 12
REVERSAL_MIN_IMPULSE = 1000.0
REVERSAL_SWING_LOOKBACK = 8
REVERSAL_VOLUME_MULTIPLIER = 1.5
REVERSAL_DELTA_SHARE_THRESHOLD = 0.25
REVERSAL_OI_MULTIPLIER = 1.0
REVERSAL_MINIMUM_SCORE = 4

# ============ OKX API ============
def get_okx_klines(instId, bar, limit):
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": str(limit)}
    resp = requests.get(url, params=params)
    data = resp.json()
    if data["code"] == "0":
        df = pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = df[col].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    return None

def get_okx_oi_history(bar, limit):
    url = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
    params = {"instId": SYMBOL, "period": bar, "limit": str(limit)}
    resp = requests.get(url, params=params)
    data = resp.json()
    records = []
    if data.get("code") == "0":
        for item in data["data"]:
            if isinstance(item, dict):
                ts = int(item.get("ts") or item.get("time"))
                oi = float(item.get("oi") or item.get("openInterest"))
            elif isinstance(item, list):
                ts = int(item[0])
                oi = float(item[1])
            else:
                continue
            records.append({"time": ts, "oi": oi})
    return records

def calculate_delta(df_main):
    lower_df = get_okx_klines(SYMBOL, LOWER_TF, 500)
    if lower_df is None:
        df_main["buy_volume"] = df_main.apply(lambda r: r["vol"] if r["close"] > r["open"] else r["vol"]*0.5 if r["close"]==r["open"] else 0, axis=1)
        df_main["sell_volume"] = df_main.apply(lambda r: r["vol"] if r["close"] < r["open"] else r["vol"]*0.5 if r["close"]==r["open"] else 0, axis=1)
        df_main["delta"] = df_main["buy_volume"] - df_main["sell_volume"]
        df_main["delta_share"] = abs(df_main["delta"]) / df_main["vol"].clip(lower=1)
        return df_main

    lower_df["dt"] = pd.to_datetime(lower_df["time"].astype(int), unit="ms")
    df_main["dt"] = pd.to_datetime(df_main["time"].astype(int), unit="ms")

    if "H" in TIMEFRAME or "h" in TIMEFRAME:
        main_duration = timedelta(hours=int(TIMEFRAME.replace("H","").replace("h","")))
    elif "D" in TIMEFRAME or "d" in TIMEFRAME:
        main_duration = timedelta(days=1)
    else:
        main_duration = timedelta(minutes=int(TIMEFRAME.replace("m","")))

    buy_list, sell_list = [], []
    for _, row in df_main.iterrows():
        start = row["dt"]
        end = start + main_duration
        sub = lower_df[(lower_df["dt"] >= start) & (lower_df["dt"] < end)]
        buy = sub[sub["close"] > sub["open"]]["vol"].sum()
        sell = sub[sub["close"] < sub["open"]]["vol"].sum()
        doji = sub[sub["close"] == sub["open"]]["vol"].sum() * 0.5
        buy += doji
        sell += doji
        buy_list.append(buy)
        sell_list.append(sell)

    df_main["buy_volume"] = buy_list
    df_main["sell_volume"] = sell_list
    df_main["delta"] = df_main["buy_volume"] - df_main["sell_volume"]
    df_main["delta_share"] = abs(df_main["delta"]) / df_main["vol"].clip(lower=1)
    return df_main

def calculate_reversal_signals(df):
    oi_records = get_okx_oi_history(TIMEFRAME, LIMIT)
    oi_map = {rec["time"]: rec["oi"] for rec in oi_records}
    df["time_ms"] = df["time"].astype(int)
    df["oi"] = df["time_ms"].map(oi_map)
    df["oi_delta"] = df["oi"].diff()
    df["oi_abs_base"] = df["oi_delta"].abs().rolling(OI_LOOKBACK).mean()
    df["oi_decrease"] = df["oi_delta"] < 0

    df["volume_base"] = df["vol"].rolling(VOLUME_LOOKBACK).mean()
    df["prior_high"] = df["high"].shift(1).rolling(REVERSAL_SWING_LOOKBACK).max()
    df["prior_low"] = df["low"].shift(1).rolling(REVERSAL_SWING_LOOKBACK).min()
    df["impulse_high"] = df["high"].shift(1).rolling(REVERSAL_IMPULSE_LOOKBACK).max()
    df["impulse_low"] = df["low"].shift(1).rolling(REVERSAL_IMPULSE_LOOKBACK).min()
    df["impulse_range"] = df["impulse_high"] - df["impulse_low"]
    df["impulse_pass"] = df["impulse_range"] >= REVERSAL_MIN_IMPULSE

    df["candle_range"] = (df["high"] - df["low"]).clip(lower=0.01)
    df["lower_wick"] = df[["open","close"]].min(axis=1) - df["low"]
    df["upper_wick"] = df["high"] - df[["open","close"]].max(axis=1)

    df["bull_sweep"] = df["low"] <= df["prior_low"]
    df["bear_sweep"] = df["high"] >= df["prior_high"]
    df["bull_reject"] = (df["close"] >= df["low"] + df["candle_range"]*0.60) | (df["lower_wick"] >= df["candle_range"]*0.45)
    df["bear_reject"] = (df["close"] <= df["low"] + df["candle_range"]*0.40) | (df["upper_wick"] >= df["candle_range"]*0.45)

    df["volume_pass"] = df["vol"] >= df["volume_base"] * REVERSAL_VOLUME_MULTIPLIER
    df["delta_pass"] = df["delta_share"] >= REVERSAL_DELTA_SHARE_THRESHOLD
    df["oi_pass"] = df["oi_delta"].abs() >= df["oi_abs_base"] * REVERSAL_OI_MULTIPLIER

    df["bull_flow_pass"] = (df["delta"] < 0) | ((df["delta"] > 0) & df["oi_decrease"])
    df["bear_flow_pass"] = (df["delta"] > 0) | ((df["delta"] < 0) & df["oi_decrease"])

    df["bull_score"] = (
        df["impulse_pass"].astype(int) +
        df["bull_sweep"].astype(int) +
        df["bull_reject"].astype(int) +
        df["volume_pass"].astype(int) +
        (df["delta_pass"] & df["bull_flow_pass"]).astype(int) +
        (df["oi_pass"] & df["oi_decrease"]).astype(int)
    )
    df["bear_score"] = (
        df["impulse_pass"].astype(int) +
        df["bear_sweep"].astype(int) +
        df["bear_reject"].astype(int) +
        df["volume_pass"].astype(int) +
        (df["delta_pass"] & df["bear_flow_pass"]).astype(int) +
        (df["oi_pass"] & df["oi_decrease"]).astype(int)
    )

    df["bull_candidate"] = df["bull_sweep"] & df["bull_reject"] & (df["bull_score"] >= REVERSAL_MINIMUM_SCORE)
    df["bear_candidate"] = df["bear_sweep"] & df["bear_reject"] & (df["bear_score"] >= REVERSAL_MINIMUM_SCORE)

    df["candle_up"] = df["close"] > df["open"]
    df["candle_down"] = df["close"] < df["open"]
    df["short_cover"] = (
        df["candle_up"] &
        (df["delta"] > 0) &
        df["oi_decrease"] &
        df["delta_pass"] &
        (df["vol"] >= df["volume_base"] * 0.80)
    )
    df["long_liq"] = (
        df["candle_down"] &
        (df["delta"] < 0) &
        df["oi_decrease"] &
        df["delta_pass"] &
        (df["vol"] >= df["volume_base"] * 0.80)
    )

    df["short_cover_score"] = (
        df["candle_up"].astype(int) +
        df["delta_pass"].astype(int) +
        (df["oi_pass"] & df["oi_decrease"]).astype(int) +
        df["volume_pass"].astype(int)
    )
    df["long_liq_score"] = (
        df["candle_down"].astype(int) +
        df["delta_pass"].astype(int) +
        (df["oi_pass"] & df["oi_decrease"]).astype(int) +
        df["volume_pass"].astype(int)
    )
    return df

def strength_text(score, max_score):
    return "Strong" if score >= max_score - 1 else "Medium" if score >= max_score - 2 else "Watch"

def build_tooltip(row, signal_type):
    if signal_type == "SHORT_COVER":
        title = f"SHORT COVER [{strength_text(row['short_cover_score'], 4)}]"
        detail = "Shorts buying back"
        bias = "UP - squeeze possible"
        invalid = f"below {row['prior_low']:.1f} (flush low)"
        next_text = "UP - next squeeze possible"
        level = f"Squeeze High: {row['prior_high']:.1f}"
        score = f"{int(row['short_cover_score'])}/4"
    elif signal_type == "LONG_LIQ":
        title = f"LONG LIQUIDATION [{strength_text(row['long_liq_score'], 4)}]"
        detail = "Longs liquidating"
        bias = "DOWN - continuation risk"
        invalid = f"above {row['prior_high']:.1f} (squeeze high)"
        next_text = "DOWN - continuation risk"
        level = f"Flush Low: {row['prior_low']:.1f}"
        score = f"{int(row['long_liq_score'])}/4"
    elif signal_type == "BULL_REVERSAL":
        title = f"BULL REVERSAL [{strength_text(row['bull_score'], 6)}]"
        detail = "+ TRAPPED SELLERS" if (row['candle_down'] and row['oi_decrease'] and row['delta'] > 0) else "Score-based exhaustion signal"
        bias = "UP"
        invalid = f"below {row['low']:.1f} (flush low)"
        next_text = "UP - follow-through possible"
        level = f"Squeeze High: {row['prior_high']:.1f}"
        score = f"{int(row['bull_score'])}/6"
    elif signal_type == "BEAR_REVERSAL":
        title = f"BEAR REVERSAL [{strength_text(row['bear_score'], 6)}]"
        detail = "+ TRAPPED BUYERS" if (row['candle_up'] and row['oi_decrease'] and row['delta'] < 0) else "Score-based exhaustion signal"
        bias = "DOWN"
        invalid = f"above {row['high']:.1f} (squeeze high)"
        next_text = "DOWN - follow-through needed"
        level = f"Flush Low: {row['prior_low']:.1f}"
        score = f"{int(row['bear_score'])}/6"
    else:
        return ""

    return (
        f"{title}\n"
        f"{detail}\n"
        f"Bias: {bias} · Score: {score}\n"
        f"⌛ GET READY - {next_text}\n"
        f"❌ Wrong if close {invalid}\n"
        f"Next: {next_text}\n"
        f"{level}\n"
        f"V {row['vol']:.0f} · Δ {row['delta']:.0f} · OI {row['oi_delta']:.0f}"
    )

def send_telegram_sync(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=text))
    loop.close()

last_evaluated_time = None

def check_and_alert():
    global last_evaluated_time
    df = get_okx_klines(SYMBOL, TIMEFRAME, LIMIT)
    if df is None:
        return

    df = calculate_delta(df)
    df = calculate_reversal_signals(df)

    if len(df) < 2:
        return

    period_ms = (
        int(pd.Timedelta(TIMEFRAME).total_seconds() * 1000)
        if "m" in TIMEFRAME or "H" in TIMEFRAME or "h" in TIMEFRAME
        else 86400000
    )
    current_ms = int(time.time() * 1000)
    last_start = int(df["time"].iloc[-1])

    if current_ms < last_start + period_ms:
        row = df.iloc[-2]
        candle_time = int(df["time"].iloc[-2])
    else:
        row = df.iloc[-1]
        candle_time = int(df["time"].iloc[-1])

    if last_evaluated_time == candle_time:
        return

    signal_sent = False
    if row["short_cover"]:
        send_telegram_sync(build_tooltip(row, "SHORT_COVER"))
        signal_sent = True
    if row["long_liq"]:
        send_telegram_sync(build_tooltip(row, "LONG_LIQ"))
        signal_sent = True
    if row["bull_candidate"]:
        send_telegram_sync(build_tooltip(row, "BULL_REVERSAL"))
        signal_sent = True
    if row["bear_candidate"]:
        send_telegram_sync(build_tooltip(row, "BEAR_REVERSAL"))
        signal_sent = True

    if signal_sent:
        last_evaluated_time = candle_time

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_scheduler():
    schedule.every(5).minutes.do(check_and_alert)
    while True:
        schedule.run_pending()
        time.sleep(1)

# Scheduler কে background thread-এ চালু করুন
import threading
threading.Thread(target=run_scheduler, daemon=True).start()

# Flask app চালানোর জন্য (Render-এ Gunicorn ব্যবহার হয়)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
