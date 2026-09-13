"""
core/candle_store.py — Unified Upstox V3 historical candle fetching.

Every signal module (RVOL, VWAP/POC, volume profile, breakout, etc.) should
fetch candles through here rather than each rolling its own HTTP calls —
this is where the "drop the still-forming candle" correctness logic and the
IST-safe date handling live, once, instead of being reimplemented per module
with a chance of drifting out of sync.
"""

from datetime import datetime, timedelta

from core.auth import upstox_get
from core.config import IST, UPSTOX_BASE


def ist_date_string(dt=None):
    """Format a datetime as YYYY-MM-DD in IST, regardless of the machine's own timezone."""
    if dt is None:
        dt = datetime.now(tz=IST)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt.strftime("%Y-%m-%d")


def _fetch_candles(instrument_key, unit, interval, from_date, to_date):
    url = f"{UPSTOX_BASE}/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    payload = upstox_get(url)
    rows = (payload.get("data") or {}).get("candles") or []

    # Each row: [timestamp, open, high, low, close, volume, open_interest]
    candles = [
        {
            "date": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "open_interest": row[6] if len(row) > 6 else None,
        }
        for row in rows
    ]
    candles.sort(key=lambda c: c["date"])  # Upstox returns newest-first
    return candles


def get_intraday_candles(instrument_key, unit="minutes", interval=5, lookback_days=10):
    """Native intraday candles for the last `lookback_days` calendar days,
    with any still-forming candle dropped. Works for any Upstox-supported
    interval (minutes 1-300, or hours 1-5) — not hardcoded to 5-minute like
    the original breakout scanner, so CVD/footprint work at 1-min and
    breakout screening can stay at 5-min from the same function."""
    to_date = ist_date_string()
    from_date = ist_date_string(datetime.now(tz=IST) - timedelta(days=lookback_days))
    candles = _fetch_candles(instrument_key, unit, interval, from_date, to_date)

    now = datetime.now(tz=IST)
    interval_minutes = interval if unit == "minutes" else interval * 60
    while candles:
        last = candles[-1]
        start = datetime.fromisoformat(last["date"])
        end = start + timedelta(minutes=interval_minutes)
        if end > now:
            candles.pop()  # still-forming candle — not "completed" yet
        else:
            break
    return candles


def get_daily_candles(instrument_key, lookback_days=200):
    """Daily candles for the last `lookback_days` calendar days, excluding
    today's still-open session."""
    to_date = ist_date_string()
    from_date = ist_date_string(datetime.now(tz=IST) - timedelta(days=lookback_days))
    candles = _fetch_candles(instrument_key, "days", 1, from_date, to_date)

    today = ist_date_string()
    return [c for c in candles if ist_date_string(datetime.fromisoformat(c["date"])) != today]
