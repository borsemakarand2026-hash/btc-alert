"""
BTC round-level 15m breakout alert -> Telegram
Data source: Delta Exchange India public API (no API key needed)

Rules (same as the TradingView script, alerts on EVERY signal):
  BUY : closed 15m candle crosses a round level (open below, close above),
        high > previous high, close > previous high, close > VWAP
  SELL: closed 15m candle crosses a round level (open above, close below),
        low < previous low, close < previous low, close < VWAP
VWAP resets daily at 00:00 UTC.

Usage:
  python signal_alert.py           # normal run (checks new closed candles)
  python signal_alert.py --test    # sends a test Telegram message
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------- Settings ----------------
BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "BTCUSD"
RESOLUTION = "15m"
TF_SEC = 900
STEP = 1000              # round level step: 75000, 74000, ...
MODE = "strict"          # "strict" = open one side, close other side | "loose" = level inside candle range
RR = 2.0                 # reference target 1 : RR (shown in the message only)
LOOKBACK_CANDLES = 300   # ~3 days of 15m candles (needed for daily VWAP)
MAX_ALERTS_PER_RUN = 8   # safety cap
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------- Data ----------------
def fetch_candles():
    end = int(time.time())
    start = end - LOOKBACK_CANDLES * TF_SEC
    params = {"resolution": RESOLUTION, "symbol": SYMBOL, "start": str(start), "end": str(end)}
    r = requests.get(f"{BASE_URL}/v2/history/candles", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    result = data.get("result") or []
    candles = [
        {
            "time": int(c["time"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume") or 0),
        }
        for c in result
    ]
    candles.sort(key=lambda x: x["time"])
    return candles


def add_vwap(candles):
    """Daily-anchored VWAP (hlc3), resets at 00:00 UTC."""
    day, cum_pv, cum_v = None, 0.0, 0.0
    for c in candles:
        d = c["time"] // 86400
        if d != day:
            day, cum_pv, cum_v = d, 0.0, 0.0
        tp = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += tp * c["volume"]
        cum_v += c["volume"]
        c["vwap"] = cum_pv / cum_v if cum_v > 0 else c["close"]


# ---------------- Signal logic ----------------
def check_signal(c, p):
    """c = candle being checked, p = previous candle. Returns (side, level) or None."""
    lvl_bull = math.floor(c["close"] / STEP) * STEP
    lvl_bear = math.ceil(c["close"] / STEP) * STEP
    lvl_near = math.floor(c["close"] / STEP + 0.5) * STEP

    if MODE == "strict":
        bull_cross = c["open"] < lvl_bull < c["close"]
        bear_cross = c["open"] > lvl_bear > c["close"]
        bull_lvl, bear_lvl = lvl_bull, lvl_bear
    else:
        inside = c["low"] <= lvl_near <= c["high"]
        bull_cross = bear_cross = inside
        bull_lvl = bear_lvl = lvl_near

    bull_break = c["close"] > p["high"] and c["high"] > p["high"]
    bear_break = c["close"] < p["low"] and c["low"] < p["low"]

    if bull_cross and bull_break and c["close"] > c["vwap"]:
        return "BUY", bull_lvl
    if bear_cross and bear_break and c["close"] < c["vwap"]:
        return "SELL", bear_lvl
    return None


def build_message(side, level, c):
    entry = c["close"]
    if side == "BUY":
        sl = c["low"]
        risk = entry - sl
        tp = entry + RR * risk
        icon = "🟢"
    else:
        sl = c["high"]
        risk = sl - entry
        tp = entry - RR * risk
        icon = "🔴"

    open_utc = datetime.fromtimestamp(c["time"], tz=timezone.utc)
    ist = open_utc + timedelta(hours=5, minutes=30)
    close_ist = ist + timedelta(seconds=TF_SEC)

    lines = [
        f"{icon} {side} signal - {SYMBOL} (15m)",
        f"Round level: {level:,.0f}",
        f"Entry (candle close): {entry:,.1f}",
        f"Stop loss: {sl:,.1f}",
    ]
    if risk > 0:
        lines.append(f"Target (1:{RR:g}): {tp:,.1f}")
    lines.append(f"VWAP: {c['vwap']:,.1f}")
    lines.append(f"Candle closed: {close_ist.strftime('%d %b %H:%M')} IST")
    return "\n".join(lines)


# ---------------- Telegram ----------------
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[no Telegram credentials set] would send:\n" + text + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text},
        timeout=20,
    )
    r.raise_for_status()


# ---------------- State ----------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------- Main ----------------
def run():
    candles = fetch_candles()
    if len(candles) < 3:
        print("Not enough candles returned.")
        return
    add_vwap(candles)

    now = int(time.time())
    closed_idx = [i for i, c in enumerate(candles) if c["time"] + TF_SEC <= now]
    if len(closed_idx) < 2:
        print("Not enough closed candles.")
        return

    state = load_state()
    last_checked = state.get("last_checked")
    if last_checked is None:
        to_check = closed_idx[-1:]          # first run: only the latest closed candle
    else:
        to_check = [i for i in closed_idx if candles[i]["time"] > last_checked]
    to_check = to_check[-MAX_ALERTS_PER_RUN:]

    sent = 0
    for i in to_check:
        if i == 0:
            continue
        sig = check_signal(candles[i], candles[i - 1])
        if sig:
            send_telegram(build_message(sig[0], sig[1], candles[i]))
            sent += 1

    state["last_checked"] = candles[closed_idx[-1]]["time"]
    save_state(state)
    print(f"Checked {len(to_check)} candle(s), sent {sent} alert(s).")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_telegram("✅ Test message: your BTC signal alert bot is working.")
        print("Test message sent.")
    else:
        run()
