"""
core/live_candle_reader.py — Reads the shared snapshot file written by
scripts/run_live_scanner_feed.py, for anything that wants live-aggregated
candles instead of a REST call per symbol per refresh.

Deliberately paranoid about staleness: if scripts/run_live_scanner_feed.py
isn't running (never started, crashed, market closed and stopped
intentionally), the file either won't exist or will stop updating. Silently
serving stale candles to a Buy/Sell decision would be worse than just
falling back to REST — so every read checks the snapshot's age and refuses
anything older than MAX_STALENESS_SECONDS, forcing the caller to fall back.
"""

import json
import os
import time

LIVE_CANDLES_PATH = os.environ.get(
    "LIVE_CANDLES_OUTPUT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache", "live_candles.json"),
)
MAX_STALENESS_SECONDS = 30  # run_live_scanner_feed.py dumps every 5s — 30s means something's wrong, not just a slow tick


class LiveFeedUnavailable(Exception):
    """Raised when the live snapshot is missing, unreadable, or stale —
    callers should catch this and fall back to REST candles."""


def _load_snapshot():
    if not os.path.exists(LIVE_CANDLES_PATH):
        raise LiveFeedUnavailable(f"{LIVE_CANDLES_PATH} does not exist — is run_live_scanner_feed.py running?")
    try:
        with open(LIVE_CANDLES_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # A half-written file shouldn't be possible (the writer does
        # atomic temp+rename) but a genuinely corrupt one is still a
        # "don't trust this" situation, not a crash.
        raise LiveFeedUnavailable(f"Could not read {LIVE_CANDLES_PATH}: {exc}")

    age = time.time() - payload.get("generated_at", 0)
    if age > MAX_STALENESS_SECONDS:
        raise LiveFeedUnavailable(
            f"Live snapshot is {age:.0f}s old (max {MAX_STALENESS_SECONDS}s) — "
            "run_live_scanner_feed.py may have stopped or the connection stalled."
        )
    return payload


def get_live_candles(symbol):
    """Returns today's live-aggregated candles for `symbol`, in the same
    {"date","open","high","low","close","volume"} shape as
    get_intraday_candles(). Raises LiveFeedUnavailable if the live feed
    isn't usable right now — callers should catch this and fall back to
    a REST call rather than let it propagate as a generic error."""
    payload = _load_snapshot()
    entry = payload.get("symbols", {}).get(symbol)
    if not entry or not entry.get("candles"):
        raise LiveFeedUnavailable(f"No live candles for {symbol} yet (not subscribed, or zero ticks so far today).")
    return entry["candles"]


def get_live_cvd(symbol):
    """Returns the true tick-rule cumulative delta for `symbol` since
    run_live_scanner_feed.py started (NOT the candle-level approximation
    signals/cvd.py's compute_candle_cvd() uses as a REST-only fallback).
    Raises LiveFeedUnavailable on the same conditions as get_live_candles()."""
    payload = _load_snapshot()
    entry = payload.get("symbols", {}).get(symbol)
    if not entry:
        raise LiveFeedUnavailable(f"No live data for {symbol} yet.")
    return entry.get("cvd", 0.0)


def live_feed_status():
    """For a dashboard status indicator: returns (is_live, age_seconds_or_None,
    symbol_count_or_None) without raising, so a UI can show "live" vs
    "REST fallback" without a try/except at the call site."""
    try:
        payload = _load_snapshot()
    except LiveFeedUnavailable:
        return False, None, None
    age = time.time() - payload.get("generated_at", 0)
    return True, age, len(payload.get("symbols", {}))
