import os
import re
import json
import csv
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone, date

import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "bot.log"
SIGNALS_CSV = BASE_DIR / "signals.csv"

load_dotenv(ENV_PATH)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5").strip()

OKX_INST_ID = os.getenv("OKX_INST_ID", "BTC-USDT-SWAP").strip()
MIN_RR = float(os.getenv("MIN_RR", "3.0"))
MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "4"))
HEARTBEAT_HOURS = float(os.getenv("HEARTBEAT_HOURS", "6"))
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "15"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "20"))

OKX_BASE = "https://www.okx.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("btc-signal-bot")


def now_utc():
    return datetime.now(timezone.utc)


def now_ts():
    return time.time()


def today_str():
    return str(date.today())


def load_state():
    default = {
        "day": today_str(),
        "signals_today": 0,
        "last_signal_ts": 0,
        "last_heartbeat_ts": 0,
        "last_candidate_hash": "",
        "last_error_ts": 0
    }

    if not STATE_PATH.exists():
        return default

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return default

    if state.get("day") != today_str():
        state["day"] = today_str()
        state["signals_today"] = 0
        state["last_candidate_hash"] = ""

    for k, v in default.items():
        state.setdefault(k, v)

    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram env missing")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True
            },
            timeout=20
        )

        if r.status_code != 200:
            log.error("Telegram error: %s | %s", r.status_code, r.text[:500])
            return False

        return True
    except Exception as e:
        log.exception("Telegram exception: %s", e)
        return False



PAPER_COLUMNS = [
    "id",
    "created_utc",
    "created_candle_ts_ms",
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
    "fill_price",
    "opened_utc",
    "resolved_utc",
    "result_r",
    "checks",
    "ai_comment"
]


def ensure_signals_csv():
    if SIGNALS_CSV.exists():
        return

    with open(SIGNALS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PAPER_COLUMNS)
        writer.writeheader()


def load_signal_rows():
    ensure_signals_csv()

    with open(SIGNALS_CSV, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_signal_rows(rows):
    ensure_signals_csv()

    with open(SIGNALS_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PAPER_COLUMNS)
        writer.writeheader()
        for row in rows:
            clean = {k: row.get(k, "") for k in PAPER_COLUMNS}
            writer.writerow(clean)


def parse_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def parse_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def age_minutes(iso_text):
    try:
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now_utc() - dt).total_seconds() / 60
    except Exception:
        return 0


def record_signal(candidate, regime, snapshot, review):
    ensure_signals_csv()

    created = now_utc().isoformat()
    raw_id = json.dumps(
        {
            "created": created,
            "candidate": candidate,
            "regime": regime
        },
        ensure_ascii=False,
        sort_keys=True
    )
    signal_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

    row = {
        "id": signal_id,
        "created_utc": created,
        "created_candle_ts_ms": str(snapshot.get("last_candle_ts", "")),
        "instrument": OKX_INST_ID,
        "side": candidate.get("side", ""),
        "setup": candidate.get("setup", ""),
        "regime": regime,
        "entry": candidate.get("entry", ""),
        "sl": candidate.get("sl", ""),
        "tp": candidate.get("tp", ""),
        "rr": candidate.get("rr", ""),
        "score": candidate.get("score", ""),
        "status": "WAIT_ENTRY",
        "fill_price": "",
        "opened_utc": "",
        "resolved_utc": "",
        "result_r": "",
        "checks": "0",
        "ai_comment": review.get("comment", "")
    }

    rows = load_signal_rows()
    rows.append(row)
    write_signal_rows(rows)

    log.info("Paper signal recorded: %s", row)


def update_paper_signals(candle_ts_ms, high, low, close):
    if not SIGNALS_CSV.exists():
        return

    rows = load_signal_rows()
    changed = False
    messages = []

    for row in rows:
        status = row.get("status", "")

        if status in ["TP", "SL", "EXPIRED", "CANCELLED"]:
            continue

        created_candle_ts_ms = parse_int(row.get("created_candle_ts_ms"), 0)

        # Не проверяем сигнал на той же свече, на которой он был создан.
        if created_candle_ts_ms and candle_ts_ms <= created_candle_ts_ms:
            continue

        side = row.get("side", "")
        entry = parse_float(row.get("entry"))
        sl = parse_float(row.get("sl"))
        tp = parse_float(row.get("tp"))
        rr = parse_float(row.get("rr"))
        checks = parse_int(row.get("checks"), 0) + 1
        row["checks"] = str(checks)

        # Сигнал не вечный: если entry не дали за 2 часа, отменяем paper-сделку.
        if status == "WAIT_ENTRY" and age_minutes(row.get("created_utc", "")) > 120:
            row["status"] = "EXPIRED"
            row["resolved_utc"] = now_utc().isoformat()
            row["result_r"] = "0"
            changed = True
            messages.append(
                f"BTC PAPER EXPIRED\n"
                f"{side} {row.get('setup')}\n"
                f"Entry was not reached in 2h\n"
                f"Entry: {entry}\n"
                f"SL: {sl}\n"
                f"TP: {tp}"
            )
            continue

        if status == "WAIT_ENTRY":
            entry_touched = low <= entry <= high

            if not entry_touched:
                changed = True
                continue

            row["status"] = "OPEN"
            row["fill_price"] = str(entry)
            row["opened_utc"] = now_utc().isoformat()
            changed = True

            messages.append(
                f"BTC PAPER ENTRY FILLED\n"
                f"{side} {row.get('setup')}\n"
                f"Entry: {entry}\n"
                f"SL: {sl}\n"
                f"TP: {tp}\n"
                f"R:R: 1:{rr}"
            )

        if row.get("status") != "OPEN":
            continue

        if side == "LONG":
            hit_sl = low <= sl
            hit_tp = high >= tp
        elif side == "SHORT":
            hit_sl = high >= sl
            hit_tp = low <= tp
        else:
            continue

        if hit_sl and hit_tp:
            # Консервативно: если в одной свече были и TP, и SL, считаем SL.
            row["status"] = "SL"
            row["result_r"] = "-1"
        elif hit_tp:
            row["status"] = "TP"
            row["result_r"] = str(rr)
        elif hit_sl:
            row["status"] = "SL"
            row["result_r"] = "-1"
        else:
            changed = True
            continue

        row["resolved_utc"] = now_utc().isoformat()
        changed = True

        messages.append(
            f"BTC PAPER RESULT | {row['status']}\n"
            f"{side} {row.get('setup')}\n"
            f"Entry: {entry}\n"
            f"SL: {sl}\n"
            f"TP: {tp}\n"
            f"Result: {row['result_r']}R"
        )

    if changed:
        write_signal_rows(rows)

    for msg in messages:
        telegram_send(msg)


def okx_get(path, params):
    r = requests.get(OKX_BASE + path, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(f"OKX error: {data}")

    return data["data"]


def fetch_ticker():
    data = okx_get("/api/v5/market/ticker", {"instId": OKX_INST_ID})[0]

    return {
        "last": float(data["last"]),
        "bid": float(data["bidPx"]) if data.get("bidPx") else None,
        "ask": float(data["askPx"]) if data.get("askPx") else None,
        "ts": int(data["ts"])
    }


def fetch_candles(bar, limit):
    raw = okx_get(
        "/api/v5/market/candles",
        {
            "instId": OKX_INST_ID,
            "bar": bar,
            "limit": str(limit)
        }
    )

    rows = []
    for x in raw:
        rows.append({
            "ts": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "vol": float(x[5]),
            "vol_ccy": float(x[6]),
            "vol_quote": float(x[7]),
            "confirm": str(x[8])
        })

    df = pd.DataFrame(rows)
    df = df[df["confirm"] == "1"].copy()
    df = df.sort_values("ts").reset_index(drop=True)

    if len(df) < 60:
        raise RuntimeError(f"Too few closed candles for {bar}: {len(df)}")

    return df


def fetch_orderbook_imbalance(depth=20):
    try:
        raw = okx_get(
            "/api/v5/market/books",
            {
                "instId": OKX_INST_ID,
                "sz": str(depth)
            }
        )[0]

        bids = raw.get("bids", [])
        asks = raw.get("asks", [])

        bid_size = sum(float(x[1]) for x in bids)
        ask_size = sum(float(x[1]) for x in asks)

        total = bid_size + ask_size
        if total <= 0:
            return 0.0

        return round((bid_size - ask_size) / total, 4)

    except Exception as e:
        log.warning("Orderbook unavailable: %s", e)
        return 0.0


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean().bfill()


def with_indicators(df):
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["rsi"] = rsi(df["close"], 14)
    df["atr"] = atr(df, 14)
    df["vol_med20"] = df["vol"].rolling(20).median().bfill()
    df["vol_ratio"] = (df["vol"] / df["vol_med20"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1)
    return df


def slope(series, lookback):
    if len(series) <= lookback:
        return 0.0
    return float(series.iloc[-1] - series.iloc[-lookback])


def detect_regime(df1, df5, df15):
    last = df1.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    atr_now = max(float(last["atr"]), 1e-9)
    vol_ratio = float(last["vol_ratio"])

    if body > 2.2 * atr_now and vol_ratio > 1.35:
        return "IMPULSE"

    ema20_5 = float(df5["ema20"].iloc[-1])
    ema50_5 = float(df5["ema50"].iloc[-1])
    ema20_15 = float(df15["ema20"].iloc[-1])
    ema50_15 = float(df15["ema50"].iloc[-1])

    slope5 = slope(df5["ema20"], 5)
    slope15 = slope(df15["ema20"], 4)

    if ema20_5 > ema50_5 and ema20_15 >= ema50_15 and slope5 > 0 and slope15 >= 0:
        return "TREND_UP"

    if ema20_5 < ema50_5 and ema20_15 <= ema50_15 and slope5 < 0 and slope15 <= 0:
        return "TREND_DOWN"

    recent = df1.iloc[-35:]
    range_size = float(recent["high"].max() - recent["low"].min())
    if range_size < 4.0 * atr_now:
        return "CHOP"

    return "RANGE"


def detect_levels(df1):
    hist = df1.iloc[-100:-3].copy()
    recent = df1.iloc[-30:-3].copy()

    return {
        "major_support": float(hist["low"].min()),
        "major_resistance": float(hist["high"].max()),
        "local_support": float(recent["low"].min()),
        "local_resistance": float(recent["high"].max())
    }


def rr_value(side, entry, sl, tp):
    if side == "LONG":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp

    if risk <= 0 or reward <= 0:
        return 0.0

    return reward / risk


def round_price(x):
    return round(float(x), 1)


def make_candidate(side, setup, entry, sl, tp, reason):
    rr = rr_value(side, entry, sl, tp)
    return {
        "side": side,
        "setup": setup,
        "entry": round_price(entry),
        "sl": round_price(sl),
        "tp": round_price(tp),
        "rr": round(float(rr), 2),
        "reason": reason
    }


def score_candidate(c, df1, df5, df15, regime, levels, ob_imbalance):
    score = 0.0
    side = c["side"]
    rr = c["rr"]
    last = df1.iloc[-1]

    price = float(last["close"])
    atr_now = max(float(last["atr"]), 1e-9)
    ema20 = float(last["ema20"])
    vol_ratio = float(last["vol_ratio"])
    rsi_now = float(last["rsi"])

    if side == "LONG" and regime in ["TREND_UP", "RANGE"]:
        score += 1.5
    if side == "SHORT" and regime in ["TREND_DOWN", "RANGE"]:
        score += 1.5
    if regime == "IMPULSE":
        score -= 0.7
    if regime == "CHOP":
        score -= 3.0

    if rr >= 3.0:
        score += 2.0
    elif rr >= 2.5:
        score += 0.8
    else:
        score -= 3.0

    if vol_ratio >= 1.2:
        score += 1.0
    elif vol_ratio >= 1.0:
        score += 0.4
    else:
        score -= 0.4

    dist = abs(price - ema20) / atr_now
    if dist <= 1.3:
        score += 1.0
    elif dist > 2.2:
        score -= 1.2

    if "retest" in c["setup"]:
        score += 1.4
    if "breakout" in c["setup"] or "breakdown" in c["setup"]:
        score += 0.7

    if side == "LONG" and ob_imbalance > 0.10:
        score += 0.4
    if side == "SHORT" and ob_imbalance < -0.10:
        score += 0.4

    if side == "LONG" and rsi_now > 75:
        score -= 0.8
    if side == "SHORT" and rsi_now < 25:
        score -= 0.8

    return round(max(0.0, min(10.0, score)), 2)


def detect_candidate(df1, df5, df15, regime, levels, ob_imbalance):
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

    # 1) Breakout long: цена закрылась выше сопротивления.
    if prev_close <= local_resistance and close > local_resistance + breakout_buffer:
        entry = local_resistance + 0.05 * atr_now
        sl = min(local_resistance - sl_buffer, float(df1["low"].iloc[-8:].min()) - 0.10 * atr_now)
        tp = entry + (entry - sl) * MIN_RR
        candidates.append(make_candidate(
            "LONG",
            "breakout_retest_long",
            entry,
            sl,
            tp,
            "Пробой локального сопротивления; план — вход не в импульс, а на ретесте пробитой зоны."
        ))

    # 2) Breakdown short: цена закрылась ниже поддержки.
    if prev_close >= local_support and close < local_support - breakout_buffer:
        entry = local_support - 0.05 * atr_now
        sl = max(local_support + sl_buffer, float(df1["high"].iloc[-8:].max()) + 0.10 * atr_now)
        tp = entry - (sl - entry) * MIN_RR
        candidates.append(make_candidate(
            "SHORT",
            "breakdown_retest_short",
            entry,
            sl,
            tp,
            "Пробой локальной поддержки; план — вход на ретесте пробитой зоны снизу."
        ))

    # 3) Pullback long.
    if regime == "TREND_UP":
        touched_ema = low <= ema20 or low <= ema50
        reclaimed = close > ema20 and close > open_
        if touched_ema and reclaimed:
            entry = close
            sl = min(low, float(df1["low"].iloc[-8:].min())) - sl_buffer
            tp = entry + (entry - sl) * MIN_RR
            candidates.append(make_candidate(
                "LONG",
                "pullback_long",
                entry,
                sl,
                tp,
                "Откат к EMA в восходящем режиме и закрытие обратно выше EMA20."
            ))

    # 4) Pullback short.
    if regime == "TREND_DOWN":
        touched_ema = high >= ema20 or high >= ema50
        rejected = close < ema20 and close < open_
        if touched_ema and rejected:
            entry = close
            sl = max(high, float(df1["high"].iloc[-8:].max())) + sl_buffer
            tp = entry - (sl - entry) * MIN_RR
            candidates.append(make_candidate(
                "SHORT",
                "pullback_short",
                entry,
                sl,
                tp,
                "Откат к EMA в нисходящем режиме и закрытие обратно ниже EMA20."
            ))

    valid = []
    for c in candidates:
        if c["rr"] < MIN_RR:
            continue

        c["score"] = score_candidate(c, df1, df5, df15, regime, levels, ob_imbalance)

        if c["score"] >= 7.0:
            valid.append(c)

    if not valid:
        return None

    valid.sort(key=lambda x: x["score"], reverse=True)
    return valid[0]


def make_hash(obj):
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError("No JSON object found")

    return json.loads(m.group(0))


def ai_review(candidate, snapshot):
    if not OPENAI_API_KEY:
        return {
            "approved": candidate["score"] >= 8.0,
            "comment": "OpenAI key отсутствует. Fallback: разрешено только для score >= 8.",
            "veto": "NO_OPENAI_KEY"
        }

    prompt = {
        "task": "Review BTC-USDT-SWAP futures signal. Return strict JSON only.",
        "rules": [
            "Do not invent new levels.",
            "Do not promise profit.",
            "Reject if entry looks late.",
            "Reject if R:R is weak.",
            "Reject if regime contradicts side.",
            "Reject if nearby support/resistance blocks TP."
        ],
        "candidate": candidate,
        "market_snapshot": snapshot,
        "required_json": {
            "approved": True,
            "comment": "short explanation in Russian",
            "veto": "NONE or short reason"
        }
    }

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=json.dumps(prompt, ensure_ascii=False)
        )

        raw = resp.output_text.strip()
        data = extract_json(raw)

        return {
            "approved": bool(data.get("approved", False)),
            "comment": str(data.get("comment", ""))[:600],
            "veto": str(data.get("veto", "NONE"))[:200]
        }

    except Exception as e:
        log.exception("OpenAI failed: %s", e)

        return {
            "approved": candidate["score"] >= 8.0,
            "comment": "OpenAI недоступен. Fallback разрешает только score >= 8.",
            "veto": "OPENAI_ERROR"
        }


def format_signal(candidate, regime, snapshot, review):
    return (
        f"BTC SIGNAL | {candidate['side']}\n"
        f"Instrument: {OKX_INST_ID}\n"
        f"Setup: {candidate['setup']}\n"
        f"Regime: {regime}\n\n"
        f"Entry: {candidate['entry']}\n"
        f"SL: {candidate['sl']}\n"
        f"TP: {candidate['tp']}\n"
        f"R:R: 1:{candidate['rr']}\n"
        f"Score: {candidate['score']}/10\n\n"
        f"Why:\n"
        f"- {candidate['reason']}\n"
        f"- RSI 1m: {snapshot['rsi_1m']:.1f}\n"
        f"- ATR 1m: {snapshot['atr_1m']:.1f}\n"
        f"- Volume ratio: {snapshot['vol_ratio_1m']:.2f}\n"
        f"- Orderbook imbalance: {snapshot['orderbook_imbalance']}\n\n"
        f"AI review:\n"
        f"{review.get('comment', '')}\n\n"
        f"Cancel:\n"
        f"Сценарий отменяется при закреплении цены за SL-зоной."
    )


def send_heartbeat_if_needed(state, ticker, regime):
    interval = HEARTBEAT_HOURS * 3600

    if now_ts() - float(state.get("last_heartbeat_ts", 0)) < interval:
        return

    msg = (
        "BTC Signal Bot alive\n"
        f"Instrument: {OKX_INST_ID}\n"
        f"Price: {ticker['last']:.1f}\n"
        f"Regime: {regime}\n"
        f"Signals today: {state.get('signals_today', 0)}/{MAX_SIGNALS_PER_DAY}\n"
        "Status: monitoring"
    )

    if telegram_send(msg):
        state["last_heartbeat_ts"] = now_ts()
        save_state(state)


def cooldown_active(state):
    return now_ts() - float(state.get("last_signal_ts", 0)) < COOLDOWN_MINUTES * 60


def main():
    state = load_state()

    telegram_send(
        "BTC Signal Bot started\n"
        f"Instrument: {OKX_INST_ID}\n"
        f"Model: {OPENAI_MODEL}\n"
        f"MIN_RR: {MIN_RR}\n"
        f"Loop: {LOOP_SECONDS}s\n"
        "Mode: signal-only"
    )

    ensure_signals_csv()
    log.info("signals.csv ready: %s", SIGNALS_CSV)
    log.info("Bot started")

    while True:
        try:
            state = load_state()

            ticker = fetch_ticker()
            df1 = with_indicators(fetch_candles("1m", 180))
            df5 = with_indicators(fetch_candles("5m", 140))
            df15 = with_indicators(fetch_candles("15m", 120))
            ob_imbalance = fetch_orderbook_imbalance(20)

            regime = detect_regime(df1, df5, df15)
            levels = detect_levels(df1)

            last = df1.iloc[-1]
            snapshot = {
                "time_utc": now_utc().isoformat(),
                "last_candle_ts": int(last["ts"]),
                "price": ticker["last"],
                "regime": regime,
                "ema20_1m": float(last["ema20"]),
                "ema50_1m": float(last["ema50"]),
                "rsi_1m": float(last["rsi"]),
                "atr_1m": float(last["atr"]),
                "vol_ratio_1m": float(last["vol_ratio"]),
                "orderbook_imbalance": ob_imbalance,
                "levels": levels
            }

            # PAPER_TRACKING_UPDATE_CALL
            update_paper_signals(
                candle_ts_ms=int(last["ts"]),
                high=float(last["high"]),
                low=float(last["low"]),
                close=float(last["close"])
            )

            log.info(
                "price=%.1f regime=%s rsi=%.1f atr=%.1f vol=%.2f ob=%s",
                ticker["last"],
                regime,
                snapshot["rsi_1m"],
                snapshot["atr_1m"],
                snapshot["vol_ratio_1m"],
                ob_imbalance
            )

            send_heartbeat_if_needed(state, ticker, regime)

            if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
                time.sleep(LOOP_SECONDS)
                continue

            if cooldown_active(state):
                time.sleep(LOOP_SECONDS)
                continue

            candidate = detect_candidate(df1, df5, df15, regime, levels, ob_imbalance)
            if not candidate:
                time.sleep(LOOP_SECONDS)
                continue

            h = make_hash({
                "side": candidate["side"],
                "setup": candidate["setup"],
                "entry": candidate["entry"],
                "sl": candidate["sl"],
                "tp": candidate["tp"]
            })

            if h == state.get("last_candidate_hash"):
                time.sleep(LOOP_SECONDS)
                continue

            review = ai_review(candidate, snapshot)

            if not review.get("approved"):
                log.info("Candidate rejected: %s | %s", candidate, review)
                state["last_candidate_hash"] = h
                save_state(state)
                time.sleep(LOOP_SECONDS)
                continue

            msg = format_signal(candidate, regime, snapshot, review)

            if telegram_send(msg):
                # PAPER_TRACKING_RECORD_CALL
                record_signal(candidate, regime, snapshot, review)
                state["signals_today"] += 1
                state["last_signal_ts"] = now_ts()
                state["last_candidate_hash"] = h
                save_state(state)
                log.info("Signal sent: %s", candidate)

            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            break

        except Exception as e:
            log.exception("Loop error: %s", e)

            state = load_state()
            if now_ts() - float(state.get("last_error_ts", 0)) > 1800:
                telegram_send(f"BTC Signal Bot error\n{type(e).__name__}: {str(e)[:300]}")
                state["last_error_ts"] = now_ts()
                save_state(state)

            time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
