import csv
import time
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import bot

OUT = Path("/root/btc-signal-bot/watchlist.csv")
STATE = Path("/root/btc-signal-bot/watchlist_state.json")

MIN_B_SCORE = 5.8
MAX_B_SCORE = 6.99

COLUMNS = [
    "id",
    "time_utc",
    "instrument",
    "side",
    "setup",
    "regime",
    "entry",
    "sl",
    "tp",
    "rr",
    "score",
    "status",
    "result_r",
    "price",
    "rsi",
    "atr",
    "vol_ratio",
    "ob",
    "reason"
]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def ensure_file():
    if OUT.exists():
        return
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writeheader()


def load_state():
    if not STATE.exists():
        return {"seen": []}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": []}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def make_id(c):
    raw = json.dumps({
        "side": c["side"],
        "setup": c["setup"],
        "entry": c["entry"],
        "sl": c["sl"],
        "tp": c["tp"],
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def write_row(row):
    ensure_file()
    with open(OUT, "a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)


def candidates(df1, df5, df15, regime, levels, ob):
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

    out = []

    breakout_buffer = 0.10 * atr_now
    sl_buffer = 0.35 * atr_now

    if prev_close <= local_resistance and close > local_resistance + breakout_buffer:
        entry = local_resistance + 0.05 * atr_now
        sl = min(local_resistance - sl_buffer, float(df1["low"].iloc[-8:].min()) - 0.10 * atr_now)
        tp = entry + (entry - sl) * bot.MIN_RR
        out.append(bot.make_candidate("LONG", "breakout_retest_long", entry, sl, tp, "B-watch breakout long"))

    if prev_close >= local_support and close < local_support - breakout_buffer:
        entry = local_support - 0.05 * atr_now
        sl = max(local_support + sl_buffer, float(df1["high"].iloc[-8:].max()) + 0.10 * atr_now)
        tp = entry - (sl - entry) * bot.MIN_RR
        out.append(bot.make_candidate("SHORT", "breakdown_retest_short", entry, sl, tp, "B-watch breakdown short"))

    if regime == "TREND_UP":
        touched_ema = low <= ema20 or low <= ema50
        reclaimed = close > ema20 and close > open_
        if touched_ema and reclaimed:
            entry = close
            sl = min(low, float(df1["low"].iloc[-8:].min())) - sl_buffer
            tp = entry + (entry - sl) * bot.MIN_RR
            out.append(bot.make_candidate("LONG", "pullback_long", entry, sl, tp, "B-watch pullback long"))

    if regime == "TREND_DOWN":
        touched_ema = high >= ema20 or high >= ema50
        rejected = close < ema20 and close < open_
        if touched_ema and rejected:
            entry = close
            sl = max(high, float(df1["high"].iloc[-8:].max())) + sl_buffer
            tp = entry - (sl - entry) * bot.MIN_RR
            out.append(bot.make_candidate("SHORT", "pullback_short", entry, sl, tp, "B-watch pullback short"))

    for c in out:
        c["score"] = bot.score_candidate(c, df1, df5, df15, regime, levels, ob)
        yield c


def main():
    ensure_file()
    state = load_state()
    seen = set(state.get("seen", []))

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

            added = 0

            for c in candidates(df1, df5, df15, regime, levels, ob):
                if c["rr"] < bot.MIN_RR:
                    continue

                if not (MIN_B_SCORE <= c["score"] <= MAX_B_SCORE):
                    continue

                signal_id = make_id(c)
                if signal_id in seen:
                    continue

                seen.add(signal_id)
                added += 1

                write_row({
                    "id": signal_id,
                    "time_utc": now_utc(),
                    "instrument": bot.OKX_INST_ID,
                    "side": c["side"],
                    "setup": c["setup"],
                    "regime": regime,
                    "entry": c["entry"],
                    "sl": c["sl"],
                    "tp": c["tp"],
                    "rr": c["rr"],
                    "score": c["score"],
                    "status": "WATCH",
                    "result_r": "",
                    "price": ticker["last"],
                    "rsi": round(float(last["rsi"]), 2),
                    "atr": round(float(last["atr"]), 2),
                    "vol_ratio": round(float(last["vol_ratio"]), 2),
                    "ob": ob,
                    "reason": c["reason"],
                })

            state["seen"] = list(seen)[-500:]
            save_state(state)

            print(now_utc(), "regime=", regime, "price=", ticker["last"], "added_B=", added)

            time.sleep(15)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print("ERR", type(e).__name__, str(e))
            time.sleep(15)


if __name__ == "__main__":
    main()
