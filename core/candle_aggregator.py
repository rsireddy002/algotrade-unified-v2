"""
core/candle_aggregator.py — Turns a stream of individual ticks (one per
instrument, arriving in real time off core/feed_listener.py's WebSocket)
into rolling 5-minute OHLCV candles, per symbol, held in memory.

Ported from dryarapureddy-tick-ML's candle_aggregator.py — same proven
logic (including the cumulative-day-volume-to-per-tick-delta conversion,
which the Upstox feed requires: it reports total volume traded so far
today, not a per-tick amount). Pure logic, no network/websocket code
here, so it's easy to unit-test with synthetic ticks.

This is the piece that lets the Scanner Grid eventually read live-built
candles instead of re-polling the REST historical-candle endpoint per
symbol per refresh — see scripts/run_live_scanner_feed.py, which owns
one of these and periodically dumps its state to a shared JSON file.
"""

from datetime import datetime
from threading import RLock

from core.config import IST

BAR_SECONDS = 5 * 60  # 5-minute bars, matching the REST candle interval used elsewhere


def bucket_start(ts, bar_seconds=BAR_SECONDS):
    """Floors a timestamp to its containing bar's start time, e.g.
    09:17:42 -> 09:15:00 for 5-min bars."""
    epoch = ts.timestamp()
    floored = epoch - (epoch % bar_seconds)
    return datetime.fromtimestamp(floored, tz=ts.tzinfo)


class CandleAggregator:
    """
    Thread-safe. Call on_tick() from the WebSocket receive loop (which
    runs continuously); call get_candles()/snapshot() from anywhere else
    (e.g. the periodic "dump to file" loop).

    Internal state per symbol:
        {"bars": {bar_start_iso: {...}},
         "last_cum_volume": <float, to compute per-tick volume deltas
                             since Upstox's feed gives CUMULATIVE day
                             volume (vtt), not per-tick volume>}
    """

    def __init__(self, bar_seconds=BAR_SECONDS):
        self.bar_seconds = bar_seconds
        self._data = {}  # symbol -> {"bars": {...}, "last_cum_volume": float}
        self._lock = RLock()  # reentrant: snapshot() calls get_candles() while already holding this

    def on_tick(self, symbol, ltp, cum_volume, timestamp=None):
        """
        symbol: your own symbol string (e.g. "RELIANCE") — map from
            instrument_key to symbol before calling this.
        ltp: last traded price, float.
        cum_volume: TOTAL volume traded so far today for this instrument
            (this is what Upstox's feed's vtt field reports — not a
            per-tick delta). Pass None if unavailable; volume stays 0
            for bars built from ticks that never carried a volume figure.
        timestamp: tz-aware datetime; defaults to now in IST.
        """
        if timestamp is None:
            timestamp = datetime.now(IST)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=IST)

        bstart = bucket_start(timestamp, self.bar_seconds)
        bkey = bstart.isoformat()

        with self._lock:
            state = self._data.setdefault(symbol, {"bars": {}, "last_cum_volume": None})
            bars = state["bars"]

            vol_delta = 0.0
            if cum_volume is not None:
                prev_cum = state["last_cum_volume"]
                if prev_cum is not None and cum_volume >= prev_cum:
                    vol_delta = cum_volume - prev_cum
                state["last_cum_volume"] = cum_volume

            if bkey not in bars:
                bars[bkey] = {
                    "timestamp": bstart, "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp, "volume": vol_delta,
                }
            else:
                bar = bars[bkey]
                bar["high"] = max(bar["high"], ltp)
                bar["low"] = min(bar["low"], ltp)
                bar["close"] = ltp
                bar["volume"] += vol_delta

    def get_candles(self, symbol):
        """Returns this symbol's bars so far today, sorted oldest-first,
        in the SAME dict shape used by core/candle_store.py's REST-fetched
        candles ({"date": iso_string, "open", "high", "low", "close",
        "volume"}) — so anything downstream (RVOL, VWAP, volume profile,
        value area, candle-approx CVD) works unchanged against either
        source."""
        with self._lock:
            state = self._data.get(symbol)
            if not state:
                return []
            bars = sorted(state["bars"].values(), key=lambda b: b["timestamp"])
            return [
                {
                    "date": b["timestamp"].isoformat(),
                    "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"], "volume": b["volume"],
                }
                for b in bars
            ]

    def get_all_symbols(self):
        with self._lock:
            return list(self._data.keys())

    def snapshot(self):
        """Returns {symbol: [candles...]} for every symbol currently
        tracked — what the dump loop periodically writes to the shared
        file."""
        with self._lock:
            return {sym: self.get_candles(sym) for sym in self._data}
