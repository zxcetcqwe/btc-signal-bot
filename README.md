# btc-signal-bot

A rule-based signal bot for the OKX **BTC-USDT-SWAP** perpetual, run live for 5 months
(May–September 2026) on a Hetzner VPS as a `systemd` service. All signals were **paper-tracked**
— no real orders were placed — and every signal was logged with its outcome, which makes this
repository a small, fully documented **negative-result study**: the strategy does not work,
and the data shows why.

> Status: archived. Not intended for live trading.

## What the bot does

Every 15 seconds it:

1. Pulls 1m, 5m and 15m candles, the ticker and the order book (depth 20) for BTC-USDT-SWAP
   from the OKX public API.
2. Classifies the market regime across the three timeframes (`TREND_UP`, `TREND_DOWN`, `RANGE`,
   plus `IMPULSE` / `CHOP` filters) using EMA20/EMA50, RSI(14), ATR(14), relative volume and
   order-book imbalance.
3. Looks for a **breakout of the last ~27 one-minute candles' high/low** and plans an entry
   just beyond the broken level ("breakout + retest"): long above a broken resistance, short below
   a broken support. Two pullback-to-EMA setups also exist in the code but can never reach the
   score threshold, so they never fired. Each candidate is scored 0–10 against regime, R:R,
   volume, distance from EMA20, order-book imbalance and RSI; only score ≥ 7.0 goes on.
4. Requires a minimum reward/risk of **3.0**, a 20-minute cooldown and at most 4 signals per day.
5. Sends the candidate to an OpenAI model with a strict-JSON prompt ("reject if entry looks late /
   R:R is weak / regime contradicts side / a level blocks TP"). **Rejected candidates are dropped
   and only logged; approved ones are sent to Telegram with the model's comment.**
6. Tracks the signal on paper: waits for the entry price, then follows it until TP or SL is hit
   and records the result in R-multiples (`+3` on TP, `-1` on SL) in `signals.csv`. If the entry
   is not reached within 2 hours the signal expires with `0`.

Config lives in `.env` (see the variables at the top of `bot.py`). Secrets are not committed.

## Results (281 signals, 2026-05-02 → 2026-09-28)

| Outcome | Count |
|---------|------:|
| Take-profit (+3R) | 52 |
| Stop-loss (−1R) | 212 |
| Expired (entry not reached in 2 h) | 17 |
| **Net result** | **−56R** |

Win rate on resolved signals: **19.7 %**. With a 3:1 R:R the break-even win rate is 25 % before
fees. The strategy is well below it — and fees make it far worse (see below).

### By side

| Side | Signals | Net R | Mean R |
|------|--------:|------:|-------:|
| LONG | 102 | −8 | −0.08 |
| SHORT | 179 | −48 | −0.27 |

### By regime

| Regime | Signals | Net R | Mean R |
|--------|--------:|------:|-------:|
| RANGE | 111 | −34 | −0.31 |
| TREND_DOWN | 96 | −27 | −0.28 |
| TREND_UP | 74 | +5 | +0.07 |

### By month

| Month | Signals | Net R |
|-------|--------:|------:|
| 2026-05 | 45 | −20 |
| 2026-06 | 80 | −24 |
| 2026-07 | 54 | −5 |
| 2026-08 | 51 | +1 |
| 2026-09 | 51 | −8 |

### Cost of trading (computed afterwards, not by the bot)

| | Median |
|---|---:|
| Risk per trade (entry → SL) | 114 pts ≈ 0.16 % of price |
| Round-trip fee on OKX (0.02 % maker in + 0.05 % taker out) | ≈ 47 pts ≈ **0.42 R** |
| Time from signal to fill | 1.1 min |
| Time in trade | 17 min |
| Net result after fees | **≈ −197 R** (−0.75 R per trade) |

With a stop that small, the break-even win rate after fees is about **42 %**, not 25 %.

Reproduce with:

```bash
python -c "import pandas as pd; d=pd.read_csv('signals.csv'); print(d.status.value_counts()); print(d.result_r.sum())"
```

## What the data says

- **The stop is too small for the costs.** Stops were sized in 1-minute ATR units — a median
  0.16 % of price — while a round trip costs ≈ 0.07 %. Every trade gave away ~0.4 R before
  anything happened. This alone makes the strategy unprofitable regardless of entry quality.
- **"Retest" entries were really momentum entries.** The entry was placed 0.05 ATR beyond the
  level that had just broken, and the median fill came 1.1 minutes after the signal — i.e. on the
  next candle, in the impulse, the opposite of what the setup intended.
- **Breakouts in a range are mostly false breakouts.** 40 % of all signals were taken in
  `RANGE` and they account for more than half of the loss. A single filter — no trading in
  `RANGE` — would have removed most of the damage, but not turned the system profitable.
- **Shorts are the main loss.** Breakdown-retest shorts on BTC arrive late: sell-offs are fast
  and rarely give a clean retest, so the entry is filled after the move and the stop is hit on noise.
- **The only positive slice is noise.** Longs in `TREND_UP` finished at +5R over 74 signals
  with ±3R per trade — statistically indistinguishable from zero.
- **The LLM gate cannot be evaluated from this dataset.** The model acted as a filter: rejected
  candidates were dropped and never recorded, so `signals.csv` only contains what it approved.
  Among the approved, its comments are uniformly positive ("acceptable", "consistent with regime")
  including on all 212 stop-outs. Whether it filtered out anything worse is unknown — the
  rejections live only in the (unpublished) log.
- **No month was meaningfully profitable.** The best month was +1R.
- **Fees were never modelled in the bot.** A fee-rejection filter was planned in May
  (`ENTRY_FEE_RATE`, `MAX_FEE_TO_REWARD` in the audit) and never implemented; the backups named
  `before-fee-rejections` are byte-identical to the final code. That is where the project stalled.

Conclusion: a 1-minute breakout scalp with ATR-sized stops and a fixed 3:1 target has no edge on
BTC-USDT-SWAP, and after fees it cannot have one. The result is stable across five months and
three market regimes.

## What I would do differently

- Compute cost-per-trade *first*: if fees are a large fraction of the stop, stop there.
- Backtest on historical candles before deploying, instead of collecting the result live.
- Record the LLM's rejections in the dataset too, so its value can be measured.
- Log fees and slippage per signal so the net result is exact, not estimated.
- Don't rewrite the whole CSV every loop iteration; append or use SQLite.
- Rotate `bot.log` — it grew to 78 MB over five months.

## Stack

Python 3.12 · pandas / numpy · requests (OKX REST) · OpenAI API · Telegram Bot API ·
Ubuntu 24.04 + systemd on a Hetzner CX23.

## Files

| File | Purpose |
|------|---------|
| `bot.py` | main loop: data, regime detection, setup search, scoring, LLM gate, Telegram, paper outcome tracking |
| `b_watchlist.py` | logs "B-grade" candidates (score 5.8–6.99, below the signal threshold) to `watchlist.csv` |
| `watch_candidates.py` | logs every detected candidate (time, price, side, setup, regime) to `watch_candidates.csv` |
| `analyze_bot_stats.py` | quick stats over `signals.csv` and the log |
| `signals.csv` | full dataset: every signal with entry, SL, TP, regime, outcome and R result |
| `audit_report.txt` | service/log audit snapshot from May 2026 |
| `requirements.txt` | dependencies |

## Running it (if you want to)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
python bot.py
```

Not financial advice. The point of this repo is the dataset and the analysis, not the signals.
