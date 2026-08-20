def check_compression_alerts():
    global compression_sent
    now = datetime.now(IN_TZ)

    for market, symbol in [("OKX", SYMBOL), ("CME", CME_SYMBOL)]:
        for tf in ["30m", "1H"]:
            # candle data fetch
            if market == "OKX":
                if tf == "30m":
                    bar = "30m"
                    limit = 500
                else:
                    bar = "1H"
                    limit = 300
                df_candle = get_market_klines(symbol, bar, limit)
            else:
                interval = "30m" if tf == "30m" else "1h"
                df_candle = get_yahoo_klines(symbol, interval, "7d")

            if df_candle is None:
                continue
            df_candle = df_candle.sort_values("time").reset_index(drop=True)

            if len(df_candle) < 2:
                continue

            # Determine last closed candle
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

            # ========== Exact POC using 1m data ==========
            weekly_start = open_time - 7*24*60*60*1000
            daily_start = open_time - 24*60*60*1000
            four_hour_start = open_time - 4*60*60*1000

            if market == "OKX":
                # Use OKX 1m history candles
                candles_1m = get_1m_candles_range(weekly_start, open_time)
            else:
                # Use Yahoo 1m data
                candles_1m = get_yahoo_1m_range(symbol, weekly_start, open_time)

            if candles_1m is None:
                continue

            weekly_poc = compute_poc_from_df(candles_1m, weekly_start, open_time)
            daily_poc = compute_poc_from_df(candles_1m, daily_start, open_time)
            four_hour_poc = compute_poc_from_df(candles_1m, four_hour_start, open_time)

            if None in (weekly_poc, daily_poc, four_hour_poc):
                continue

            close = candle["close"]
            if close > weekly_poc and close > daily_poc and close > four_hour_poc:
                direction = "Bullish Cross"
            elif close < weekly_poc and close < daily_poc and close < four_hour_poc:
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
