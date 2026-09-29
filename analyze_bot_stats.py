import pandas as pd
from pathlib import Path
import re
from collections import Counter

base = Path("/root/btc-signal-bot")
signals_path = base / "signals.csv"
log_path = base / "bot.log"

print("=== SIGNALS ===")

if signals_path.exists():
    df = pd.read_csv(signals_path)
    print("Signals:", len(df))

    if len(df):
        print("\nBy side:")
        print(df["side"].value_counts(dropna=False).to_string())

        print("\nBy setup:")
        print(df["setup"].value_counts(dropna=False).to_string())

        print("\nBy regime:")
        print(df["regime"].value_counts(dropna=False).to_string())

        print("\nBy status:")
        print(df["status"].value_counts(dropna=False).to_string())

        r = pd.to_numeric(df["result_r"], errors="coerce").dropna()
        if len(r):
            print("\nTotal R:", round(r.sum(), 3))
            print("Average R:", round(r.mean(), 3))

        cols = [
            "created_utc", "side", "setup", "regime",
            "entry", "sl", "tp", "rr", "score",
            "status", "result_r", "ai_comment"
        ]
        cols = [c for c in cols if c in df.columns]
        print("\nLast signals:")
        print(df[cols].tail(20).to_string(index=False))
else:
    print("signals.csv not found")

print("\n=== LOG REGIME SAMPLE ===")

if log_path.exists():
    text = log_path.read_text(errors="ignore")
    lines = text.splitlines()[-5000:]

    regimes = Counter()
    rsi_values = []
    vol_values = []

    for line in lines:
        m = re.search(r"regime=([A-Z_]+)", line)
        if m:
            regimes[m.group(1)] += 1

        m = re.search(r"rsi=([0-9.]+)", line)
        if m:
            rsi_values.append(float(m.group(1)))

        m = re.search(r"vol=([0-9.]+)", line)
        if m:
            vol_values.append(float(m.group(1)))

    print("Last log lines analyzed:", len(lines))
    print("\nRegimes:")
    for k, v in regimes.most_common():
        print(k, v)

    if rsi_values:
        print("\nRSI:")
        print("min:", min(rsi_values), "max:", max(rsi_values), "avg:", round(sum(rsi_values)/len(rsi_values), 2))

    if vol_values:
        print("\nVolume ratio:")
        print("min:", min(vol_values), "max:", max(vol_values), "avg:", round(sum(vol_values)/len(vol_values), 2))
else:
    print("bot.log not found")
