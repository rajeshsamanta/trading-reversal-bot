import os
import requests
import pandas as pd
import asyncio
import time
import schedule
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask
from telegram import Bot
import sys
sys.stdout.reconfigure(line_buffering=True)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = "8776819788:AAHfoFM_82byoGtR3q6jB0PKHw5S45GBqJI"          # <-- আপনার নতুন Bot Token এখানে বসান
CHAT_ID = "-1003988993524"                 # আপনার Channel Chat ID

SYMBOL = "BTC-USDT-SWAP"
TIMEFRAME = "15m"
LOWER_TF = "1m"
LIMIT = 300                                # 300 candles
LOWER_LIMIT = 1000                         # 1m candles for delta calculation
VOLUME_LOOKBACK = 50
OI_LOOKBACK = 42

REVERSAL_IMPULSE_LOOKBACK = 12
REVERSAL_MIN_IMPULSE = 1000.0
REVERSAL_SWING_LOOKBACK = 8
REVERSAL_VOLUME_MULTIPLIER = 1.5
REVERSAL_DELTA_SHARE_THRESHOLD = 0.25
REVERSAL_OI_MULTIPLIER = 1.0
REVERSAL_MINIMUM_SCORE = 4

DEEP_BLUE_VOLUME_MULT = 3.0
DEEP_BLUE_DELTA_SHARE = 0.35
OI_ENTRY_MULT = 1.5
OI_EXIT_MULT = 1.0
OI_BUILD_MIN_ABS_15M = 110.0
OI_EXIT_MIN_ABS_15M = 120.0

IN_TZ = ZoneInfo("Asia/Kolkata")

def to_indian_time(ms):
    if ms:
        return datetime.fromtimestamp(int(ms) / 1000, tz=IN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        return datetime.now(IN_TZ).strftime("%Y-%m-%d %H:%M:%S")

# ============ OKX API ============
def get_market_klines(instId, bar, limit):
    """OKX Futures klines"""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": str(limit)}
    try:
        resp = requests.get(url, params=params, timeout=10)
        print(f"OKX klines status: {resp.status_code}")
        data = resp.json()
        if data.get("code") == "0":
            df = pd.DataFrame(data["data"], columns=["time", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
            for col in ["open", "high", "low", "close", "vol"]:
                df[col] = df[col].astype(float)
            df = df.iloc[::-1].reset_index(drop=True)
            return df
        else:
            print(f"OKX error: {data}")
            return None
    except Exception as e:
        print(f"Error fetching OKX klines: {e}")
        return None

def get_oi_history(bar, limit):
    """OKX Open Interest history"""
    url = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
    params = {"instId": SYMBOL, "period": bar, "limit": str(limit)}
    try:
        resp = requests.get(url, params=params, timeout=10)
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
        else:
            print(f"OKX OI error: {data}")
        records = sorted(records, key=lambda x: x["time"])
        return records
    except Exception as e:
        print(f"Error fetching OKX OI: {e}")
        return []

# ============ DELTA CALCULATION ============
def calculate_delta(df_main):
    lower_df = get_market_klines(SYMBOL, LOWER_TF, LOWER_LIMIT)
    if lower_df is None:
        # fallback: candle direction based approximation
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

# ============ SIGNALS ============
def calculate_signals(df):
    oi_records = get_oi_history(TIMEFRAME, LIMIT)
    oi_map = {rec["time"]: rec["oi"] for rec in oi_records}
    df["time_ms"] = df["time"].astype(int)
    df["oi"] = df["time_ms"].map(oi_map)
    df["oi_delta"] = df["oi"].diff()
    df["oi_abs_base"] = df["oi_delta"].abs().rolling(OI_LOOKBACK).mean()
    df["oi_decrease"] = df["oi_delta"] < 0
    df["oi_increase"] = df["oi_delta"] > 0

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

    df["bull_reversal_candidate"] = df["bull_sweep"] & df["bull_reject"] & (df["bull_score"] >= REVERSAL_MINIMUM_SCORE)
    df["bear_reversal_candidate"] = df["bear_sweep"] & df["bear_reject"] & (df["bear_score"] >= REVERSAL_MINIMUM_SCORE)

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

    df["oi_entry_move_ok"] = (df["oi_delta"].abs() >= df["oi_abs_base"] * OI_ENTRY_MULT) & (df["oi_delta"].abs() >= OI_BUILD_MIN_ABS_15M)
    df["oi_exit_move_ok"] = (df["oi_delta"].abs() >= df["oi_abs_base"] * OI_EXIT_MULT) & (df["oi_delta"].abs() >= OI_EXIT_MIN_ABS_15M)

    df["new_buyers_raw"] = df["oi_entry_move_ok"] & df["oi_increase"] & df["candle_up"]
    df["new_sellers_raw"] = df["oi_entry_move_ok"] & df["oi_increase"] & df["candle_down"]

    df["buyers_exiting_raw"] = df["oi_exit_move_ok"] & df["oi_decrease"] & df["candle_down"]
    df["sellers_exiting_raw"] = df["oi_exit_move_ok"] & df["oi_decrease"] & df["candle_up"]

    # Trap/force exit conditions
    df["trapped_buyers_raw"] = df["candle_up"] & df["oi_decrease"] & (df["delta"] < 0)
    df["trapped_sellers_raw"] = df["candle_down"] & df["oi_decrease"] & (df["delta"] > 0)

    df["bull_shift_raw"] = (
        (df["vol"] >= df["volume_base"] * DEEP_BLUE_VOLUME_MULT) &
        (df["delta_share"] >= DEEP_BLUE_DELTA_SHARE) &
        (df["delta"] > 0) &
        df["candle_up"]
    )
    df["bear_shift_raw"] = (
        (df["vol"] >= df["volume_base"] * DEEP_BLUE_VOLUME_MULT) &
        (df["delta_share"] >= DEEP_BLUE_DELTA_SHARE) &
        (df["delta"] < 0) &
        df["candle_down"]
    )

    return df

# ============ FORMAT HELPERS ============
def strength_text(score, max_score):
    return "Strong" if score >= max_score - 1 else "Medium" if score >= max_score - 2 else "Watch"

def format_volume(v):
    if pd.isna(v):
        return "n/a"
    v = abs(v)
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"{v/1_000:.2f}K"
    else:
        return f"{v:.0f}"

def format_delta(d):
    if pd.isna(d):
        return "n/a"
    sign = "+" if d > 0 else "-" if d < 0 else ""
    if abs(d) >= 1000:
        return f"{sign}{format_volume(abs(d))}"
    else:
        return f"{sign}{d:.0f}"

def flow_text(row):
    if row["candle_up"] and row["delta"] < 0:
        return "Flow: Price up, sellers stronger"
    elif row["candle_down"] and row["delta"] > 0:
        return "Flow: Price down, buyers stronger"
    elif row["delta"] > 0:
        return "Flow: Buyers stronger"
    elif row["delta"] < 0:
        return "Flow: Sellers stronger"
    else:
        return "Flow: Neutral"

def net_flow_text(row):
    if pd.isna(row["delta"]):
        return "Net: n/a"
    elif row["delta"] > 0:
        return f"Net Buyer: {format_volume(row['delta'])}"
    elif row["delta"] < 0:
        return f"Net Seller: {format_volume(row['delta'])}"
    else:
        return "Net: Neutral"

def trap_exit_text(row):
    parts = []
    if row.get("trapped_buyers_raw", False):
        parts.append("Trap / Force Exit: Buyers trapped")
    if row.get("trapped_sellers_raw", False):
        parts.append("Trap / Force Exit: Sellers trapped")
    if row.get("buyers_exiting_raw", False):
        parts.append("Exit: Buyers exiting")
    if row.get("sellers_exiting_raw", False):
        parts.append("Exit: Sellers exiting")
    return " | ".join(parts) if parts else "Trap / Force Exit: none"

# ============ REVERSAL TOOLTIP ============
def build_reversal_tooltip(row, signal_type, timeframe, candle_time):
    if signal_type == "SHORT_COVER":
        title = f"SHORT COVER [{strength_text(row['short_cover_score'], 4)}]"
        detail = "Shorts buying back"
        bias = "UP - squeeze possible"
        invalid = f"below {row['prior_low']:.1f} (flush low)"
        next_text = "UP - next squeeze possible"
        level = f"Squeeze High: {row['prior_high']:.1f}"
        score = f"{int(row['short_cover_score'])}/4"
        confirm = f"BUY only on close above {row['high']:.1f}"
    elif signal_type == "LONG_LIQ":
        title = f"LONG LIQUIDATION [{strength_text(row['long_liq_score'], 4)}]"
        detail = "Longs liquidating"
        bias = "DOWN - continuation risk"
        invalid = f"above {row['prior_high']:.1f} (squeeze high)"
        next_text = "DOWN - continuation risk"
        level = f"Flush Low: {row['prior_low']:.1f}"
        score = f"{int(row['long_liq_score'])}/4"
        confirm = f"SELL only on close below {row['low']:.1f}"
    elif signal_type == "BULL_REVERSAL":
        title = f"BULL REVERSAL [{strength_text(row['bull_score'], 6)}]"
        detail = "+ TRAPPED SELLERS" if (row['candle_down'] and row['oi_decrease'] and row['delta'] > 0) else "Score-based exhaustion signal"
        bias = "UP"
        invalid = f"below {row['low']:.1f} (flush low)"
        next_text = "UP - follow-through possible"
        level = f"Squeeze High: {row['prior_high']:.1f}"
        score = f"{int(row['bull_score'])}/6"
        confirm = f"BUY only on close above {row['high']:.1f}"
    elif signal_type == "BEAR_REVERSAL":
        title = f"BEAR REVERSAL [{strength_text(row['bear_score'], 6)}]"
        detail = "+ TRAPPED BUYERS" if (row['candle_up'] and row['oi_decrease'] and row['delta'] < 0) else "Score-based exhaustion signal"
        bias = "DOWN"
        invalid = f"above {row['high']:.1f} (squeeze high)"
        next_text = "DOWN - follow-through needed"
        level = f"Flush Low: {row['prior_low']:.1f}"
        score = f"{int(row['bear_score'])}/6"
        confirm = f"SELL only on close below {row['low']:.1f}"
    else:
        return ""

    time_str = to_indian_time(candle_time)
    header = f"🕐 {timeframe} | {time_str}\n"

    return (
        header +
        f"{title}\n"
        f"{detail}\n"
        f"Bias: {bias} · Score: {score}\n"
        f"⌛ GET READY - {next_text}\n"
        f"❌ Wrong if close {invalid}\n"
        f"Next: {next_text}\n"
        f"{level}\n"
        f"{confirm}\n"
        f"V {format_volume(row['vol'])} · Δ {format_delta(row['delta'])} · OI {format_delta(row['oi_delta'])}"
    )

# ============ MONEY FLOW TOOLTIP ============
def build_moneyflow_tooltip(row, signal_type, timeframe, candle_time):
    time_str = to_indian_time(candle_time)
    header = f"🕐 {timeframe} | {time_str}\n"

    if signal_type == "NEW_BUYERS":
        title = "🟢 NEW BUYERS ENTRY"
        detail = f"Price: {row['close']:.1f}"
    elif signal_type == "NEW_SELLERS":
        title = "🔴 NEW SELLERS ENTRY"
        detail = f"Price: {row['close']:.1f}"
    elif signal_type == "SELLER_EXIT":
        title = "⚫ SELLER EXIT"
        detail = f"Price: {row['close']:.1f}"
    elif signal_type == "BUYER_EXIT":
        title = "⚫ BUYER EXIT"
        detail = f"Price: {row['close']:.1f}"
    elif signal_type == "BULLISH_FLOW":
        title = "🟢 BULLISH MONEY FLOW"
        detail = f"Price: {row['close']:.1f}"
    elif signal_type == "BEARISH_FLOW":
        title = "🔴 BEARISH MONEY FLOW"
        detail = f"Price: {row['close']:.1f}"
    else:
        return ""

    buy_sell_vol = f"Buy Volume: {format_volume(row['buy_volume'])} · Sell Volume: {format_volume(row['sell_volume'])}"
    oi_line = f"OI Change: {format_delta(row['oi_delta'])}"

    return (
        header +
        f"{title}\n"
        f"{detail}\n"
        f"{flow_text(row)}\n"
        f"{net_flow_text(row)}\n"
        f"{buy_sell_vol}\n"
        f"{oi_line}\n"
        f"{trap_exit_text(row)}\n"
    )

# ============ TELEGRAM SENDER ============
def send_telegram_sync(text):
    bot = Bot(token=TELEGRAM_TOKEN)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.send_message(chat_id=CHAT_ID, text=text))
    loop.close()

# ============ MAIN CHECK ============
last_evaluated_time = None

def check_and_alert():
    global last_evaluated_time
    print(f"[{datetime.now()}] Checking signals...")

    df = get_market_klines(SYMBOL, TIMEFRAME, LIMIT)
    if df is None:
        print("❌ Failed to fetch candles")
        return

    df = calculate_delta(df)
    df = calculate_signals(df)

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
        row = df.iloc[-2]  # last closed candle
        candle_time = int(df["time"].iloc[-2])
    else:
        row = df.iloc[-1]  # last candle already closed
        candle_time = int(df["time"].iloc[-1])

    if last_evaluated_time == candle_time:
        return

    messages = []

    # Reversal signals
    if row["short_cover"]:
        messages.append(build_reversal_tooltip(row, "SHORT_COVER", TIMEFRAME, candle_time))
    if row["long_liq"]:
        messages.append(build_reversal_tooltip(row, "LONG_LIQ", TIMEFRAME, candle_time))
    if row["bull_reversal_candidate"]:
        messages.append(build_reversal_tooltip(row, "BULL_REVERSAL", TIMEFRAME, candle_time))
    if row["bear_reversal_candidate"]:
        messages.append(build_reversal_tooltip(row, "BEAR_REVERSAL", TIMEFRAME, candle_time))

    # Money flow signals
    if row["new_buyers_raw"]:
        messages.append(build_moneyflow_tooltip(row, "NEW_BUYERS", TIMEFRAME, candle_time))
    if row["new_sellers_raw"]:
        messages.append(build_moneyflow_tooltip(row, "NEW_SELLERS", TIMEFRAME, candle_time))
    if row["buyers_exiting_raw"]:
        messages.append(build_moneyflow_tooltip(row, "SELLER_EXIT", TIMEFRAME, candle_time))
    if row["sellers_exiting_raw"]:
        messages.append(build_moneyflow_tooltip(row, "BUYER_EXIT", TIMEFRAME, candle_time))
    if row["bull_shift_raw"]:
        messages.append(build_moneyflow_tooltip(row, "BULLISH_FLOW", TIMEFRAME, candle_time))
    if row["bear_shift_raw"]:
        messages.append(build_moneyflow_tooltip(row, "BEARISH_FLOW", TIMEFRAME, candle_time))

    for msg in messages:
        try:
            send_telegram_sync(msg)
            print("✅ Sent signal to Telegram")
        except Exception as e:
            print(f"❌ Telegram send error: {e}")

    if messages:
        last_evaluated_time = candle_time
    else:
        print("No signals on this candle.")

# ============ FLASK APP ============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

@app.route("/test")
def test():
    try:
        send_telegram_sync(f"🕐 {TIMEFRAME} | Test message from Render (OKX Futures)")
        return "Test message sent"
    except Exception as e:
        return f"Error: {e}"

def run_scheduler():
    schedule.every(5).minutes.do(check_and_alert)
    while True:
        schedule.run_pending()
        time.sleep(1)

import threading
threading.Thread(target=run_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
