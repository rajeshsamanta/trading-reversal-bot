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
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = "8776819788:AAHfoFM_82byoGtR3q6jB0PKHw5S45GBqJI"          # <-- এখানে আপনার Bot Token বসান
CHAT_ID = "-1003988993524"                 # আপনার Channel Chat ID

SYMBOL = "BTC-USDT-SWAP"                   # OKX perpetual
CME_SYMBOL = "BTC=F"                       # Yahoo Finance CME Bitcoin

TIMEFRAME = "15m"
LOWER_TF = "1m"
LIMIT = 300
LOWER_LIMIT = 1000
VOLUME_LOOKBACK = 50
OI_LOOKBACK = 42

REVERSAL_IMPULSE_LOOKBACK = 12
REVERSAL_MIN_IMPULSE = 1000.0
REVERSAL_SWING_LOOKBACK = 8
REVERSAL_VOLUME_MULTIPLIER = 1.5
REVERSAL_DELTA_SHARE_THRESHOLD = 0.25
REVERSAL_OI_MULTIPLIER = 1.0
REVERSAL_MINIMUM_SCORE = 4

BEAR_REVERSAL_MIN_IMPULSE = 1050.0
BEAR_REVERSAL_VOLUME_MULTIPLIER = 1.575
BEAR_REVERSAL_DELTA_SHARE_THRESHOLD = 0.2625
BEAR_REVERSAL_OI_MULTIPLIER = 1.05

DEEP_BLUE_VOLUME_MULT = 3.0
DEEP_BLUE_DELTA_SHARE = 0.35

OI_ENTRY_MULT = 2.06
OI_BUILD_MIN_ABS_15M = 151.2

OI_EXIT_MULT = 1.27
OI_EXIT_MIN_ABS_15M = 152.5

DIVERGENCE_EVENT_MULT = 1.2

TRAPPED_OI_MULT = 1.05
TRAPPED_VOLUME_MULT = 1.05
TRAPPED_DELTA_SHARE_MIN = 0.05

POC_BIN_SIZE = 1.0
POC_TIE_BREAK = "Latest"

IN_TZ = ZoneInfo("Asia/Kolkata")
poc_sent = set()
compression_sent = set()

def to_indian_time(ms):
    if ms:
        return datetime.fromtimestamp(int(ms) / 1000, tz=IN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        return datetime.now(IN_TZ).strftime("%Y-%m-%d %H:%M:%S")

# ============ OKX API ============
def get_market_klines(instId, bar, limit):
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

def get_1m_candles_range(start_ms, end_ms):
    url = "https://www.okx.com/api/v5/market/history-candles"
    all_candles = []
    cursor = str(end_ms)
    while True:
        params = {
            "instId": SYMBOL,
            "bar": "1m",
            "limit": "100",
            "before": cursor,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if data.get("code") != "0":
                print(f"History candles error: {data}")
                break
            rows = data.get("data", [])
            if not rows:
                break
            for r in rows:
                ts = int(r[0])
                if ts < start_ms:
                    break
                all_candles.append(r)
            earliest_ts = int(rows[-1][0])
            if earliest_ts <= start_ms or len(rows) < 100:
                break
            cursor = str(earliest_ts)
        except Exception as e:
            print(f"Error fetching 1m history: {e}")
            break

    if not all_candles:
        return None
    df = pd.DataFrame(all_candles, columns=["time", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("time").reset_index(drop=True)
    return df

def get_oi_history(bar, limit):
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

# ============ YAHOO FINANCE API ============
def get_yahoo_klines(symbol, interval, range_str):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
    params = {
        "range": range_str,
        "interval": interval,
        "includePrePost": "false"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        data = resp.json()
        chart = data.get("chart", {})
        result = chart.get("result", [])
        if not result:
            print(f"Yahoo no result for {symbol}")
            return None
        r = result[0]
        timestamps = r.get("timestamp", [])
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
        rows = []
        for i, ts in enumerate(timestamps):
            if None in (opens[i], highs[i], lows[i], closes[i]):
                continue
            rows.append({
                "time": int(ts) * 1000,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "vol": float(volumes[i]) if volumes[i] else 0.0
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        return df
    except Exception as e:
        print(f"Yahoo fetch error: {e}")
        return None

def get_yahoo_1m_range(symbol, start_ms, end_ms):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
    params = {
        "range": "7d",
        "interval": "1m",
        "includePrePost": "false"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        chart = data.get("chart", {})
        result = chart.get("result", [])
        if not result:
            print(f"Yahoo 1m no result for {symbol}")
            return None
        r = result[0]
        timestamps = r.get("timestamp", [])
        quote = r.get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
        rows = []
        for i, ts in enumerate(timestamps):
            ts_ms = int(ts) * 1000
            if ts_ms < start_ms or ts_ms >= end_ms:
                continue
            if None in (opens[i], highs[i], lows[i], closes[i]):
                continue
            rows.append({
                "time": ts_ms,
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "vol": float(volumes[i]) if volumes[i] else 0.0
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        return df
    except Exception as e:
        print(f"Yahoo 1m fetch error: {e}")
        return None

# ============ DELTA CALCULATION ============
def calculate_delta(df_main):
    lower_df = get_market_klines(SYMBOL, LOWER_TF, LOWER_LIMIT)
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

# ============ POC COMPUTATION ============
def compute_poc_from_df(df_candles, start_ms, end_ms):
    if df_candles is None or len(df_candles) == 0:
        return None
    df = df_candles.copy()
    df["time_ms"] = df["time"].astype(int)
    mask = (df["time_ms"] >= start_ms) & (df["time_ms"] < end_ms)
    window = df[mask]
    if len(window) == 0:
        return None

    step = max(POC_BIN_SIZE, 1.0)
    volume_by_bin = {}
    for _, row in window.iterrows():
        hlc3 = (row["high"] + row["low"] + row["close"]) / 3.0
        bin_key = int(round(hlc3 / step))
        vol = row["vol"]
        volume_by_bin[bin_key] = volume_by_bin.get(bin_key, 0.0) + vol

    if not volume_by_bin:
        return None

    max_vol = max(volume_by_bin.values())
    max_bins = [k for k, v in volume_by_bin.items() if v == max_vol]
    best_bin = max(max_bins) if POC_TIE_BREAK == "Latest" else max(max_bins)
    return best_bin * step

# ============ POC ALERTS ============
def check_poc_alerts():
    global poc_sent
    now = datetime.now(IN_TZ)
    due_sessions = []

    if now.weekday() == 0:
        session = now.replace(hour=5, minute=30, second=0, microsecond=0)
        if 0 <= (now - session).total_seconds() <= 15 * 60:
            due_sessions.append(("WEEKLY", session))

    session = now.replace(hour=5, minute=30, second=0, microsecond=0)
    if 0 <= (now - session).total_seconds() <= 15 * 60:
        due_sessions.append(("DAILY", session))

    for h, m in [(1, 30), (5, 30), (9, 30), (13, 30), (17, 30), (21, 30)]:
        session = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if 0 <= (now - session).total_seconds() <= 15 * 60:
            due_sessions.append(("4H", session))

    grouped = defaultdict(list)
    for ptype, session in due_sessions:
        key = session.strftime("%Y-%m-%d %H:%M")
        if (ptype, key) in poc_sent:
            continue
        grouped[session].append(ptype)

    if not grouped:
        return

    for session, types in grouped.items():
        if "WEEKLY" in types:
            start_ms = int((session - timedelta(days=7)).timestamp() * 1000)
        elif "DAILY" in types:
            start_ms = int((session - timedelta(days=1)).timestamp() * 1000)
        else:
            start_ms = int((session - timedelta(hours=4)).timestamp() * 1000)

        end_ms = int(session.timestamp() * 1000)
        candles = get_1m_candles_range(start_ms, end_ms)
        if candles is None:
            print(f"❌ POC candles fetch failed for {session}")
            continue

        lines = []
        for ptype in types:
            poc = compute_poc_from_df(candles, start_ms, end_ms)
            if poc is not None:
                color_map = {"WEEKLY": "🟠", "DAILY": "🔵", "4H": "🩵"}
                color = color_map.get(ptype, "⚪")
                label = f"{color} {ptype} POC: ${poc:,.2f}"
                lines.append(label)

        if lines:
            msg = f"🕐 {session.strftime('%I:%M %p')} | {session.strftime('%Y-%m-%d')}\n"
            for label in lines:
                msg += "━━━━━━━━━━━━━━━\n"
                msg += label + "\n"
            msg += "━━━━━━━━━━━━━━━"
            try:
                send_telegram_sync(msg)
                print(f"✅ Sent POC alert for {session.strftime('%Y-%m-%d %H:%M')}")
                for ptype in types:
                    poc_sent.add((ptype, session.strftime("%Y-%m-%d %H:%M")))
            except Exception as e:
                print(f"❌ POC send error: {e}")

# ============ SIGNAL CALCULATION ============
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

    df["bear_impulse_pass"] = df["impulse_range"] >= BEAR_REVERSAL_MIN_IMPULSE
    df["bear_volume_pass"] = df["vol"] >= df["volume_base"] * BEAR_REVERSAL_VOLUME_MULTIPLIER
    df["bear_delta_pass"] = df["delta_share"] >= BEAR_REVERSAL_DELTA_SHARE_THRESHOLD
    df["bear_oi_pass"] = df["oi_delta"].abs() >= df["oi_abs_base"] * BEAR_REVERSAL_OI_MULTIPLIER

    df["bull_score"] = (
        df["impulse_pass"].astype(int) +
        df["bull_sweep"].astype(int) +
        df["bull_reject"].astype(int) +
        df["volume_pass"].astype(int) +
        (df["delta_pass"] & df["bull_flow_pass"]).astype(int) +
        (df["oi_pass"] & df["oi_decrease"]).astype(int)
    )

    df["bear_score"] = (
        df["bear_impulse_pass"].astype(int) +
        df["bear_sweep"].astype(int) +
        df["bear_reject"].astype(int) +
        df["bear_volume_pass"].astype(int) +
        (df["bear_delta_pass"] & df["bear_flow_pass"]).astype(int) +
        (df["bear_oi_pass"] & df["oi_decrease"]).astype(int)
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

    df["vol_greater_than_prev"] = df["vol"] > df["vol"].shift(1)

    df["new_buyers_raw"] = df["oi_entry_move_ok"] & df["oi_increase"] & df["candle_up"]
    df["new_sellers_raw"] = df["oi_entry_move_ok"] & df["oi_increase"] & df["candle_down"]

    df["buyers_exiting_raw"] = df["oi_exit_move_ok"] & df["oi_decrease"] & df["candle_down"] & df["vol_greater_than_prev"]
    df["sellers_exiting_raw"] = df["oi_exit_move_ok"] & df["oi_decrease"] & df["candle_up"] & df["vol_greater_than_prev"]

    df["trapped_buyers_raw"] = (
        df["candle_up"] &
        df["oi_decrease"] &
        (df["delta"] < 0) &
        (df["oi_delta"].abs() >= df["oi_abs_base"] * TRAPPED_OI_MULT) &
        (df["vol"] >= df["volume_base"] * TRAPPED_VOLUME_MULT) &
        (df["delta"].abs() / df["vol"].clip(lower=1) >= TRAPPED_DELTA_SHARE_MIN)
    )
    df["trapped_sellers_raw"] = (
        df["candle_down"] &
        df["oi_decrease"] &
        (df["delta"] > 0) &
        (df["oi_delta"].abs() >= df["oi_abs_base"] * TRAPPED_OI_MULT) &
        (df["vol"] >= df["volume_base"] * TRAPPED_VOLUME_MULT) &
        (df["delta"].abs() / df["vol"].clip(lower=1) >= TRAPPED_DELTA_SHARE_MIN)
    )

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

    df["flow_disagrees"] = (df["candle_up"] & (df["delta"] < 0)) | (df["candle_down"] & (df["delta"] > 0))
    df["divergence_move_ok"] = df["oi_delta"].abs() >= df["oi_abs_base"] * DIVERGENCE_EVENT_MULT
    df["bullish_divergence"] = df["divergence_move_ok"] & df["candle_down"] & (df["delta"] > 0)
    df["bearish_divergence"] = df["divergence_move_ok"] & df["candle_up"] & (df["delta"] < 0)

    df["bull_confirmed"] = df["bull_reversal_candidate"].shift(1) & (df["close"] > df["high"].shift(1))
    df["bear_confirmed"] = df["bear_reversal_candidate"].shift(1) & (df["close"] < df["low"].shift(1))
    df["bull_failed"] = df["bull_reversal_candidate"].shift(1) & (df["close"] < df["low"].shift(1))
    df["bear_failed"] = df["bear_reversal_candidate"].shift(1) & (df["close"] > df["high"].shift(1))

    return df

# ============ HELPERS ============
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
        return f"{sign}{abs(d):.0f}"

def flow_text_en(row):
    if row["candle_up"] and row["delta"] < 0:
        return "Flow: Mismatch — Price up, sellers stronger (Sell absorption)"
    elif row["candle_down"] and row["delta"] > 0:
        return "Flow: Mismatch — Price down, buyers stronger (Buy absorption)"
    elif row["delta"] > 0:
        return "Flow: Buyers stronger"
    elif row["delta"] < 0:
        return "Flow: Sellers stronger"
    else:
        return "Flow: Neutral"

def flow_text_hi(row):
    if row["candle_up"] and row["delta"] < 0:
        return "प्रवाह: बेमेल — कीमत ऊपर, विक्रेता मजबूत (सेल अवशोषण)"
    elif row["candle_down"] and row["delta"] > 0:
        return "प्रवाह: बेमेल — कीमत नीचे, खरीदार मजबूत (खरीद अवशोषण)"
    elif row["delta"] > 0:
        return "प्रवाह: खरीदार मजबूत"
    elif row["delta"] < 0:
        return "प्रवाह: विक्रेता मजबूत"
    else:
        return "प्रवाह: तटस्थ"

def flow_text_bn(row):
    if row["candle_up"] and row["delta"] < 0:
        return "প্রবাহ: মিসম্যাচ — দাম উপরে, বিক্রেতারা শক্তিশালী (সেল শোষণ)"
    elif row["candle_down"] and row["delta"] > 0:
        return "প্রবাহ: মিসম্যাচ — দাম নিচে, ক্রেতারা শক্তিশালী (বাই শোষণ)"
    elif row["delta"] > 0:
        return "প্রবাহ: ক্রেতারা শক্তিশালী"
    elif row["delta"] < 0:
        return "প্রবাহ: বিক্রেতারা শক্তিশালী"
    else:
        return "প্রবাহ: নিরপেক্ষ"

def net_flow_text_en(row):
    if pd.isna(row["delta"]):
        return "Net: n/a"
    elif row["delta"] > 0:
        return f"Net Buyer: {format_volume(row['delta'])}"
    elif row["delta"] < 0:
        return f"Net Seller: {format_volume(row['delta'])}"
    else:
        return "Net: Neutral"

def net_flow_text_hi(row):
    if pd.isna(row["delta"]):
        return "नेट: n/a"
    elif row["delta"] > 0:
        return f"नेट खरीदार: {format_volume(row['delta'])}"
    elif row["delta"] < 0:
        return f"नेट विक्रेता: {format_volume(row['delta'])}"
    else:
        return "नेट: तटस्थ"

def net_flow_text_bn(row):
    if pd.isna(row["delta"]):
        return "নেট: n/a"
    elif row["delta"] > 0:
        return f"নেট ক্রেতা: {format_volume(row['delta'])}"
    elif row["delta"] < 0:
        return f"নেট বিক্রেতা: {format_volume(row['delta'])}"
    else:
        return "নেট: নিরপেক্ষ"

def trap_exit_text_en(row):
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

def trap_exit_text_hi(row):
    parts = []
    if row.get("trapped_buyers_raw", False):
        parts.append("ट्रैप / फोर्स एग्जिट: खरीदार फंसे")
    if row.get("trapped_sellers_raw", False):
        parts.append("ट्रैप / फोर्स एग्जिट: विक्रेता फंसे")
    if row.get("buyers_exiting_raw", False):
        parts.append("एग्जिट: खरीदार बाहर")
    if row.get("sellers_exiting_raw", False):
        parts.append("एग्जिट: विक्रेता बाहर")
    return " | ".join(parts) if parts else "ट्रैप / फोर्स एग्जिट: कोई नहीं"

def trap_exit_text_bn(row):
    parts = []
    if row.get("trapped_buyers_raw", False):
        parts.append("ট্র্যাপ / ফোর্স এক্সিট: ক্রেতারা আটকা পড়েছে")
    if row.get("trapped_sellers_raw", False):
        parts.append("ট্র্যাপ / ফোর্স এক্সিট: বিক্রেতারা আটকা পড়েছে")
    if row.get("buyers_exiting_raw", False):
        parts.append("এক্সিট: ক্রেতারা বেরিয়ে যাচ্ছে")
    if row.get("sellers_exiting_raw", False):
        parts.append("এক্সিট: বিক্রেতারা বেরিয়ে যাচ্ছে")
    return " | ".join(parts) if parts else "ট্র্যাপ / ফোর্স এক্সিট: কেউ না"

# ============ MESSAGE BUILDERS ============
def build_reversal_tooltip(row, signal_type, timeframe, candle_time):
    time_str = to_indian_time(candle_time)
    header = f"🕐 {timeframe} | {time_str}\n"

    if signal_type == "SHORT_COVER":
        title_en = f"SHORT COVER [{strength_text(row['short_cover_score'], 4)}]"
        detail_en = "Shorts buying back"
        bias_en = "UP - squeeze possible"
        invalid_en = f"below {row['prior_low']:.1f} (flush low)"
        next_text_en = "UP - next squeeze possible"
        level_en = f"Squeeze High: {row['prior_high']:.1f}"
        score_en = f"{int(row['short_cover_score'])}/4"
        confirm_en = f"BUY only on close above {row['high']:.1f}"
    elif signal_type == "LONG_LIQ":
        title_en = f"LONG LIQUIDATION [{strength_text(row['long_liq_score'], 4)}]"
        detail_en = "Longs liquidating"
        bias_en = "DOWN - continuation risk"
        invalid_en = f"above {row['prior_high']:.1f} (squeeze high)"
        next_text_en = "DOWN - continuation risk"
        level_en = f"Flush Low: {row['prior_low']:.1f}"
        score_en = f"{int(row['long_liq_score'])}/4"
        confirm_en = f"SELL only on close below {row['low']:.1f}"
    elif signal_type == "BULL_REVERSAL":
        title_en = f"BULL REVERSAL [{strength_text(row['bull_score'], 6)}]"
        detail_en = "+ TRAPPED SELLERS" if (row['candle_down'] and row['oi_decrease'] and row['delta'] > 0) else "Score-based exhaustion signal"
        bias_en = "UP"
        invalid_en = f"below {row['low']:.1f} (flush low)"
        next_text_en = "UP - follow-through possible"
        level_en = f"Squeeze High: {row['prior_high']:.1f}"
        score_en = f"{int(row['bull_score'])}/6"
        confirm_en = f"BUY only on close above {row['high']:.1f}"
    elif signal_type == "BEAR_REVERSAL":
        title_en = f"BEAR REVERSAL [{strength_text(row['bear_score'], 6)}]"
        detail_en = "+ TRAPPED BUYERS" if (row['candle_up'] and row['oi_decrease'] and row['delta'] < 0) else "Score-based exhaustion signal"
        bias_en = "DOWN"
        invalid_en = f"above {row['high']:.1f} (squeeze high)"
        next_text_en = "DOWN - follow-through needed"
        level_en = f"Flush Low: {row['prior_low']:.1f}"
        score_en = f"{int(row['bear_score'])}/6"
        confirm_en = f"SELL only on close below {row['low']:.1f}"
    elif signal_type == "CONFIRMED_BULL":
        title_en = "CONFIRMED BULL REVERSAL"
        detail_en = "Closed above the reversal candle high - follow-through in"
        bias_en = "UP"
        invalid_en = f"below {row['low']:.1f} (flush low)"
        next_text_en = "UP - follow-through / squeeze possible"
        level_en = f"Squeeze High: {row['high']:.1f}"
        score_en = f"{int(row['bull_score'])}/6"
        confirm_en = f"Buy confirmed above {row['high']:.1f}"
    elif signal_type == "CONFIRMED_BEAR":
        title_en = "CONFIRMED BEAR REVERSAL"
        detail_en = "Closed below the reversal candle low - follow-through in"
        bias_en = "DOWN"
        invalid_en = f"above {row['high']:.1f} (squeeze high)"
        next_text_en = "DOWN - continuation risk"
        level_en = f"Flush Low: {row['low']:.1f}"
        score_en = f"{int(row['bear_score'])}/6"
        confirm_en = f"Sell confirmed below {row['low']:.1f}"
    elif signal_type == "BULL_FAILED":
        title_en = "REVX - BULL REVERSAL FAILED"
        detail_en = "Price failed the bullish reversal setup"
        bias_en = "DOWN / CONTINUATION RISK"
        invalid_en = f"below {row['low']:.1f}"
        next_text_en = "DOWN - continuation risk"
        level_en = f"Flush Low: {row['low']:.1f}"
        score_en = f"{int(row['bull_score'])}/6"
        confirm_en = f"Buy only above {row['high']:.1f}"
    elif signal_type == "BEAR_FAILED":
        title_en = "REVX - BEAR REVERSAL FAILED"
        detail_en = "Price failed the bearish reversal setup"
        bias_en = "UP / CONTINUATION RISK"
        invalid_en = f"above {row['high']:.1f}"
        next_text_en = "UP - squeeze possible"
        level_en = f"Squeeze High: {row['high']:.1f}"
        score_en = f"{int(row['bear_score'])}/6"
        confirm_en = f"Sell only below {row['low']:.1f}"
    else:
        return ""

    english_msg = (
        header +
        f"{title_en}\n"
        f"{detail_en}\n"
        f"Bias: {bias_en} · Score: {score_en}\n"
        f"⌛ GET READY - {next_text_en}\n"
        f"❌ Wrong if close {invalid_en}\n"
        f"Next: {next_text_en}\n"
        f"{level_en}\n"
        f"{confirm_en}\n"
        f"V {format_volume(row['vol'])} · Δ {format_delta(row['delta'])} · OI {format_delta(row['oi_delta'])}"
    )

    def translate_to_hi(text):
        text = text.replace("SHORT COVER", "शॉर्ट कवर")
        text = text.replace("LONG LIQUIDATION", "लॉन्ग लिक्विडेशन")
        text = text.replace("BULL REVERSAL", "बुल रिवर्सल")
        text = text.replace("BEAR REVERSAL", "बियर रिवर्सल")
        text = text.replace("CONFIRMED BULL REVERSAL", "कन्फर्म्ड बुल रिवर्सल")
        text = text.replace("CONFIRMED BEAR REVERSAL", "कन्फर्म्ड बियर रिवर्सल")
        text = text.replace("REVX - BULL REVERSAL FAILED", "REVX - बुल रिवर्सल फेल")
        text = text.replace("REVX - BEAR REVERSAL FAILED", "REVX - बियर रिवर्सल फेल")
        text = text.replace("Shorts buying back", "शॉर्ट सेलर खरीद रहे हैं")
        text = text.replace("Longs liquidating", "लॉन्ग लिक्विडेट हो रहे हैं")
        text = text.replace("Score-based exhaustion signal", "स्कोर-आधारित थकावट संकेत")
        text = text.replace("UP - squeeze possible", "ऊपर - स्क्वीज़ संभव")
        text = text.replace("DOWN - continuation risk", "नीचे - निरंतरता जोखिम")
        text = text.replace("UP - follow-through possible", "ऊपर - फॉलो-थ्रू संभव")
        text = text.replace("DOWN - follow-through needed", "नीचे - फॉलो-थ्रू आवश्यक")
        text = text.replace("Squeeze High:", "स्क्वीज़ हाई:")
        text = text.replace("Flush Low:", "फ्लश लो:")
        text = text.replace("BUY only on close above", "केवल ऊपर बंद होने पर खरीदें")
        text = text.replace("SELL only on close below", "केवल नीचे बंद होने पर बेचें")
        text = text.replace("Buy confirmed above", "ऊपर खरीद की पुष्टि")
        text = text.replace("Sell confirmed below", "नीचे बिक्री की पुष्टि")
        text = text.replace("Buy only above", "केवल ऊपर खरीदें")
        text = text.replace("Sell only below", "केवल नीचे बेचें")
        return text

    def translate_to_bn(text):
        text = text.replace("SHORT COVER", "শর্ট কভার")
        text = text.replace("LONG LIQUIDATION", "লং লিকুইডেশন")
        text = text.replace("BULL REVERSAL", "বুল রিভার্সাল")
        text = text.replace("BEAR REVERSAL", "বিয়ার রিভার্সাল")
        text = text.replace("CONFIRMED BULL REVERSAL", "কনফার্মড বুল রিভার্সাল")
        text = text.replace("CONFIRMED BEAR REVERSAL", "কনফার্মড বিয়ার রিভার্সাল")
        text = text.replace("REVX - BULL REVERSAL FAILED", "REVX - বুল রিভার্সাল ব্যর্থ")
        text = text.replace("REVX - BEAR REVERSAL FAILED", "REVX - বিয়ার রিভার্সাল ব্যর্থ")
        text = text.replace("Shorts buying back", "শর্ট সেলাররা কিনছে")
        text = text.replace("Longs liquidating", "লং লিকুইডেট হচ্ছে")
        text = text.replace("Score-based exhaustion signal", "স্কোর-ভিত্তিক ক্লান্তি সংকেত")
        text = text.replace("UP - squeeze possible", "আপ - স্কুইজ সম্ভব")
        text = text.replace("DOWN - continuation risk", "ডাউন - ধারাবাহিকতার ঝুঁকি")
        text = text.replace("UP - follow-through possible", "আপ - ফলো-থ্রু সম্ভব")
        text = text.replace("DOWN - follow-through needed", "ডাউন - ফলো-থ্রু প্রয়োজন")
        text = text.replace("Squeeze High:", "স্কুইজ হাই:")
        text = text.replace("Flush Low:", "ফ্লাশ লো:")
        text = text.replace("BUY only on close above", "শুধুমাত্র উপরে ক্লোজ হলে কিনুন")
        text = text.replace("SELL only on close below", "শুধুমাত্র নিচে ক্লোজ হলে বিক্রি করুন")
        text = text.replace("Buy confirmed above", "উপরে কেনা নিশ্চিত")
        text = text.replace("Sell confirmed below", "নিচে বিক্রি নিশ্চিত")
        text = text.replace("Buy only above", "শুধুমাত্র উপরে কিনুন")
        text = text.replace("Sell only below", "শুধুমাত্র নিচে বিক্রি করুন")
        return text

    hindi_msg = translate_to_hi(english_msg)
    bengali_msg = translate_to_bn(english_msg)
    return f"[ENGLISH]\n{english_msg}\n\n[HINDI]\n{hindi_msg}\n\n[BENGALI]\n{bengali_msg}"

def build_moneyflow_tooltip(row, signal_type, timeframe, candle_time):
    time_str = to_indian_time(candle_time)
    header = f"🕐 {timeframe} | {time_str}\n"

    if signal_type == "SELLER_EXIT":
        title_en = "⚫ SELLER EXIT"
        detail_en = f"Price: {row['close']:.1f}"
        flow_en = "Flow: Buyers stronger"
        exit_en = "Exit: Sellers Exiting"
        oi_en = f"OI Change: {format_delta(row['oi_delta'])}"
        english_msg = header + f"{title_en}\n{detail_en}\n{flow_en}\n{oi_en}\n{exit_en}\n"
    elif signal_type == "BUYER_EXIT":
        title_en = "⚫ BUYER EXIT"
        detail_en = f"Price: {row['close']:.1f}"
        flow_en = "Flow: Sellers Stronger"
        exit_en = "Exit: Buyer Exiting"
        oi_en = f"OI Change: {format_delta(row['oi_delta'])}"
        english_msg = header + f"{title_en}\n{detail_en}\n{flow_en}\n{oi_en}\n{exit_en}\n"
    else:
        if signal_type == "NEW_BUYERS":
            title_en = "🟢 NEW BUYERS ENTRY"
        elif signal_type == "NEW_SELLERS":
            title_en = "🔴 NEW SELLERS ENTRY"
        elif signal_type == "BULLISH_FLOW":
            title_en = "🟢 BULLISH MONEY FLOW"
        elif signal_type == "BEARISH_FLOW":
            title_en = "🔴 BEARISH MONEY FLOW"
        elif signal_type == "BULLISH_DIVERGENCE":
            title_en = "🟢 OI DIVERGENCE - BULLISH"
        elif signal_type == "BEARISH_DIVERGENCE":
            title_en = "🔴 OI DIVERGENCE - BEARISH"
        elif signal_type == "TRAPPED_BUYERS":
            title_en = "⚠️ TRAPPED BUYERS"
        elif signal_type == "TRAPPED_SELLERS":
            title_en = "⚠️ TRAPPED SELLERS"
        else:
            return ""

        buy_sell_en = f"Buy Volume: {format_volume(row['buy_volume'])} · Sell Volume: {format_volume(row['sell_volume'])}"
        oi_en = f"OI Change: {format_delta(row['oi_delta'])}"
        english_msg = (
            header +
            f"{title_en}\n"
            f"Price: {row['close']:.1f}\n"
            f"{flow_text_en(row)}\n"
            f"{net_flow_text_en(row)}\n"
            f"{buy_sell_en}\n"
            f"{oi_en}\n"
            f"{trap_exit_text_en(row)}\n"
        )

    def translate_to_hi(text):
        text = text.replace("SELLER EXIT", "सेलर एग्ज़िट")
        text = text.replace("BUYER EXIT", "बायर एग्ज़िट")
        text = text.replace("NEW BUYERS ENTRY", "नए खरीदार प्रवेश")
        text = text.replace("NEW SELLERS ENTRY", "नए विक्रेता प्रवेश")
        text = text.replace("BULLISH MONEY FLOW", "बुलिश मनी फ्लो")
        text = text.replace("BEARISH MONEY FLOW", "बेयरिश मनी फ्लो")
        text = text.replace("OI DIVERGENCE - BULLISH", "OI डाइवर्जेंस - बुलिश")
        text = text.replace("OI DIVERGENCE - BEARISH", "OI डाइवर्जेंस - बेयरिश")
        text = text.replace("TRAPPED BUYERS", "फंसे खरीदार")
        text = text.replace("TRAPPED SELLERS", "फंसे विक्रेता")
        text = text.replace("Price:", "कीमत:")
        text = text.replace("Flow: Buyers stronger", "प्रवाह: खरीदार मजबूत")
        text = text.replace("Flow: Sellers Stronger", "प्रवाह: विक्रेता मजबूत")
        text = text.replace("Exit: Sellers Exiting", "एग्ज़िट: विक्रेता बाहर")
        text = text.replace("Exit: Buyer Exiting", "एग्ज़िट: खरीदार बाहर")
        text = text.replace("OI Change:", "OI बदलाव:")
        text = text.replace("Buy Volume:", "खरीद मात्रा:")
        text = text.replace("Sell Volume:", "बिक्री मात्रा:")
        text = text.replace("Net Buyer:", "नेट खरीदार:")
        text = text.replace("Net Seller:", "नेट विक्रेता:")
        return text

    def translate_to_bn(text):
        text = text.replace("SELLER EXIT", "সেলার এক্সিট")
        text = text.replace("BUYER EXIT", "বায়ার এক্সিট")
        text = text.replace("NEW BUYERS ENTRY", "নতুন ক্রেতা প্রবেশ")
        text = text.replace("NEW SELLERS ENTRY", "নতুন বিক্রেতা প্রবেশ")
        text = text.replace("BULLISH MONEY FLOW", "বুলিশ মানি ফ্লো")
        text = text.replace("BEARISH MONEY FLOW", "বিয়ারিশ মানি ফ্লো")
        text = text.replace("OI DIVERGENCE - BULLISH", "OI ডাইভারজেন্স - বুলিশ")
        text = text.replace("OI DIVERGENCE - BEARISH", "OI ডাইভারজেন্স - বিয়ারিশ")
        text = text.replace("TRAPPED BUYERS", "আটকে পড়া ক্রেতা")
        text = text.replace("TRAPPED SELLERS", "আটকে পড়া বিক্রেতা")
        text = text.replace("Price:", "মূল্য:")
        text = text.replace("Flow: Buyers stronger", "প্রবাহ: ক্রেতারা শক্তিশালী")
        text = text.replace("Flow: Sellers Stronger", "প্রবাহ: বিক্রেতারা শক্তিশালী")
        text = text.replace("Exit: Sellers Exiting", "এক্সিট: বিক্রেতারা বেরিয়ে যাচ্ছে")
        text = text.replace("Exit: Buyer Exiting", "এক্সিট: ক্রেতারা বেরিয়ে যাচ্ছে")
        text = text.replace("OI Change:", "OI পরিবর্তন:")
        text = text.replace("Buy Volume:", "ক্রয় ভলিউম:")
        text = text.replace("Sell Volume:", "বিক্রয় ভলিউম:")
        text = text.replace("Net Buyer:", "নেট ক্রেতা:")
        text = text.replace("Net Seller:", "নেট বিক্রেতা:")
        return text

    hindi_msg = translate_to_hi(english_msg)
    bengali_msg = translate_to_bn(english_msg)
    return f"[ENGLISH]\n{english_msg}\n\n[HINDI]\n{hindi_msg}\n\n[BENGALI]\n{bengali_msg}"

# ============ COMPRESSION ALERT ============
def check_compression_alerts():
    global compression_sent
    now = datetime.now(IN_TZ)

    for market, symbol in [("OKX", SYMBOL), ("CME", CME_SYMBOL)]:
        for tf in ["30m", "1H"]:
            if market == "OKX":
                bar = "30m" if tf == "30m" else "1H"
                limit = 500 if tf == "30m" else 300
                df_candle = get_market_klines(symbol, bar, limit)
            else:
                interval = "30m" if tf == "30m" else "1h"
                df_candle = get_yahoo_klines(symbol, interval, "7d")

            if df_candle is None:
                continue
            df_candle = df_candle.sort_values("time").reset_index(drop=True)

            if len(df_candle) < 2:
                continue

            candle_duration = 30*60*1000 if tf == "30m" else 60*60*1000
            current_ms = int(time.time() * 1000)
            last_open = int(df_candle.iloc[-1]["time"])
            if current_ms < last_open + candle_duration + 5*60*1000:
                if len(df_candle) < 3:
                    continue
                candle = df_candle.iloc[-2]
                open_time = int(candle["time"])
            else:
                candle = df_candle.iloc[-1]
                open_time = int(candle["time"])

            key = f"{market}-{tf}-{open_time}"
            if key in compression_sent:
                continue

            weekly_start = open_time - 7*24*60*60*1000
            daily_start = open_time - 24*60*60*1000
            four_hour_start = open_time - 4*60*60*1000

            if market == "OKX":
                candles_1m = get_1m_candles_range(weekly_start, open_time)
            else:
                candles_1m = get_yahoo_1m_range(symbol, weekly_start, open_time)

            if candles_1m is None:
                continue

            weekly_poc = compute_poc_from_df(candles_1m, weekly_start, open_time)
            daily_poc = compute_poc_from_df(candles_1m, daily_start, open_time)
            four_hour_poc = compute_poc_from_df(candles_1m, four_hour_start, open_time)

            if None in (weekly_poc, daily_poc, four_hour_poc):
                continue

            open_price = candle["open"]
            close_price = candle["close"]

            if (open_price < weekly_poc and open_price < daily_poc and open_price < four_hour_poc and
                close_price > weekly_poc and close_price > daily_poc and close_price > four_hour_poc):
                direction = "Bullish Cross"
            elif (open_price > weekly_poc and open_price > daily_poc and open_price > four_hour_poc and
                  close_price < weekly_poc and close_price < daily_poc and close_price < four_hour_poc):
                direction = "Bearish Cross"
            else:
                continue

            open_dt = datetime.fromtimestamp(open_time / 1000, tz=IN_TZ)
            time_str = open_dt.strftime("%I:%M %p")
            date_str = open_dt.strftime("%d/%m/%Y")
            header = f"🕐 {tf} | {time_str} | {date_str}"

            market_label = "Perpetual Compression Valid" if market == "OKX" else "CME Compression Valid"
            poc_lines = (
                f"Weekly POC: ${weekly_poc:,.2f}\n"
                f"Daily POC: ${daily_poc:,.2f}\n"
                f"4H POC: ${four_hour_poc:,.2f}"
            )

            english = f"{header}\n{market_label}\nDirection: {direction}\n{poc_lines}"
            hindi = english.replace("Bullish Cross", "बुलिश क्रॉस").replace("Bearish Cross", "बेयरिश क्रॉस")
            hindi = hindi.replace("Perpetual Compression Valid", "परपेचुअल कंप्रेशन वैध").replace("CME Compression Valid", "सीएमई कंप्रेशन वैध")
            bengali = english.replace("Bullish Cross", "বুলিশ ক্রস").replace("Bearish Cross", "বিয়ারিশ ক্রস")
            bengali = bengali.replace("Perpetual Compression Valid", "পারপেচুয়াল কমপ্রেশন ভ্যালিড").replace("CME Compression Valid", "সিএমই কমপ্রেশন ভ্যালিড")

            final_msg = f"[ENGLISH]\n{english}\n\n[HINDI]\n{hindi}\n\n[BENGALI]\n{bengali}"

            try:
                send_telegram_sync(final_msg)
                compression_sent.add(key)
                print(f"✅ Sent {market} {tf} compression alert")
            except Exception as e:
                print(f"❌ Compression send error: {e}")

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

    if len(df) >= 3:
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

        if last_evaluated_time != candle_time:
            messages = []

            if row["short_cover"]:
                messages.append(build_reversal_tooltip(row, "SHORT_COVER", TIMEFRAME, candle_time))
            if row["long_liq"]:
                messages.append(build_reversal_tooltip(row, "LONG_LIQ", TIMEFRAME, candle_time))
            if row["bull_reversal_candidate"]:
                messages.append(build_reversal_tooltip(row, "BULL_REVERSAL", TIMEFRAME, candle_time))
            if row["bear_reversal_candidate"]:
                messages.append(build_reversal_tooltip(row, "BEAR_REVERSAL", TIMEFRAME, candle_time))

            if row["bull_confirmed"]:
                messages.append(build_reversal_tooltip(row, "CONFIRMED_BULL", TIMEFRAME, candle_time))
            if row["bear_confirmed"]:
                messages.append(build_reversal_tooltip(row, "CONFIRMED_BEAR", TIMEFRAME, candle_time))
            if row["bull_failed"]:
                messages.append(build_reversal_tooltip(row, "BULL_FAILED", TIMEFRAME, candle_time))
            if row["bear_failed"]:
                messages.append(build_reversal_tooltip(row, "BEAR_FAILED", TIMEFRAME, candle_time))

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

            if row["bullish_divergence"]:
                messages.append(build_moneyflow_tooltip(row, "BULLISH_DIVERGENCE", TIMEFRAME, candle_time))
            if row["bearish_divergence"]:
                messages.append(build_moneyflow_tooltip(row, "BEARISH_DIVERGENCE", TIMEFRAME, candle_time))

            if row["trapped_buyers_raw"]:
                messages.append(build_moneyflow_tooltip(row, "TRAPPED_BUYERS", TIMEFRAME, candle_time))
            if row["trapped_sellers_raw"]:
                messages.append(build_moneyflow_tooltip(row, "TRAPPED_SELLERS", TIMEFRAME, candle_time))

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

    # POC alerts
    check_poc_alerts()

    # Compression alerts
    check_compression_alerts()

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
