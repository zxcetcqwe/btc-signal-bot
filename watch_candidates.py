import csv
import time
from pathlib import Path
from datetime import datetime, timezone

import bot

OUT = Path("/root/btc-signal-bot/watch_candidates.csv")

COLUMNS = [
    "time_utc",
    "price",
    "side",
    "setup",
    "regime",
    "entry",
    "sl",
    "tp",
    "rr",
    "score",
    "reason",
    "rsi",
    "atr",
    "vol_ratio",
    "ob",
]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def ensure_file():
    if OUT.exists():
        return
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writeheader()


def write_row(row):
    ensure_file()
    with open(OUT, "a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)


def make_all_candidates(df1, df5, df15, regime, levels, ob):
    last = df1.iloc[-1]
    prev = df1.iloc[-2]

    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])
    prev_close = float(prev["close"])
    atr_now = max(float(last["atr"]), 1e-9)
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])

    local_support = levels["local_support"]
    local_resistance = levels["local_resistance"]

    candidates = []

    breakout_buffer = 0.10 * atr_now
    sl_buffer = 0.35 * atr_now

    if prev_close <= local_resistance and close > local_resistance + breakout_buffer:
        entry = local_resistance + 0.05 * atr_now
        sl = min(local_resistance - sl_buffer, float(df1["low"].iloc[-8:].min()) - 0.10 * atr_now)
        tp = entry + (entry - sl) * bot.MIN_RR
        candidates.append(bot.make_candidate(
            "LONG",
            "breakout_retest_long",
            entry,
            sl,
            tp,
            "breakout_long_raw"
        ))

    if prev_close >= local_support and close < local_support - breakout_buffer:
        entry = local_support - 0.05 * atr_now
        sl = max(local_support + sl_buffer, float(df1["high"].iloc[-8:].max()) + 0.10 * atr_now)
        tp = entry - (sl - entry) * bot.MIN_RR
        candidates.append(bot.make_candidate(
            "SHORT",
            "breakdown_retest_short",
            entry,
            sl,
            tp,
            "breakdown_short_raw"
        ))

    if regime == "TREND_UP":
        touched_ema = low <= ema20 or low <= ema50
        reclaimed = close > ema20 and close > open_
        if touched_ema and reclaimed:
            entry = close
            sl = min(low, float(df1["low"].iloc[-8:].min())) - sl_buffer
            tp = entry + (entry - sl) * bot.MIN_RR
            candidates.append(bot.make_candidate(
                "LONG",
                "pullback_long",
                entry,
                sl,
                tp,
                "pullback_long_raw"
            ))

    if regime == "TREND_DOWN":
        touched_ema = high >= ema20 or high >= ema50
        rejected = close < ema20 and close < open_
        if touched_ema and rejected:
            entry = close
            sl = max(high, float(df1["high"].iloc[-8:].max())) + sl_buffer
            tp = entry - (sl - entry) * bot.MIN_RR
            candidates.append(bot.make_candidate(
                "SHORT",
                "pullback_short",
                entry,
                sl,
                tp,
                "pullback_short_raw"
            ))

    for c in candidates:
        c["score"] = bot.score_candidate(c, df1, df5, df15, regime, levels, ob)

        reasons = []
        if c["rr"] < bot.MIN_RR:
            reasons.append("rr_below_min")
        if c["score"] < 7.0:
            reasons.append("score_below_7")
        if not reasons:
            reasons.append("PASS_A_CANDIDATE")

        yield c, ",".join(reasons)


def main():
    ensure_file()

    while True:
        try:
            ticker = bot.fetch_ticker()
            df1 = bot.with_indicators(bot.fetch_candles("1m", 180))
            df5 = bot.with_indicators(bot.fetch_candles("5m", 140))
            df15 = bot.with_indicators(bot.fetch_candles("15m", 120))
            ob = bot.fetch_orderbook_imbalance(20)

            regime = bot.detect_regime(df1, df5, df15)
            levels = bot.detect_levels(df1)

            last = df1.iloc[-1]

            found = 0
            for c, reason in make_all_candidates(df1, df5, df15, regime, levels, ob):
                found += 1
                write_row({
                    "time_utc": now_utc(),
                    "price": ticker["last"],
                    "side": c["side"],
                    "setup": c["setup"],
                    "regime": regime,
                    "entry": c["entry"],
                    "sl": c["sl"],
                    "tp": c["tp"],
                    "rr": c["rr"],
                    "score": c["score"],
                    "reason": reason,
                    "rsi": round(float(last["rsi"]), 2),
                    "atr": round(float(last["atr"]), 2),
                    "vol_ratio": round(float(last["vol_ratio"]), 2),
                    "ob": ob,
                })

            print(now_utc(), "regime=", regime, "price=", ticker["last"], "raw_candidates=", found)

            time.sleep(15)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print("ERR", type(e).__name__, str(e))
            time.sleep(15)


if __name__ == "__main__":
    main()
