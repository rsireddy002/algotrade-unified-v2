"""
signals/cvd.py — Cumulative Volume Delta (CVD), fed by core.feed_listener.

Upstox's WebSocket feed gives LTP + last-traded-quantity (ltq) updates,
not raw buy/sell-tagged trade prints. This uses the standard "tick rule"
to classify each print as buy- or sell-initiated: price ticks up from the
previous print → buy-initiated (aggressor lifted the offer); ticks down →
sell-initiated (aggressor hit the bid); unchanged price → classified by
the last known direction (a print at an unchanged price is assumed to
continue the prevailing side, standard tick-rule convention).

This is a real, standard methodology — not a guess — but it's an
approximation: without actual bid/ask-tagged prints, a small fraction of
classifications will be wrong compared to true executed-side data. That's
inherent to tick-rule CVD everywhere, not specific to this implementation.

Feeds from core.feed_listener's decoded messages — pass each decoded
FeedResponse's per-instrument LTPC through record_tick().
"""

from dataclasses import dataclass, field


@dataclass
class CvdState:
    cumulative_delta: float = 0.0
    last_price: float = None
    last_direction: int = 1  # 1 = buy-side, -1 = sell-side; used for unchanged-price ticks
    tick_count: int = 0


class CvdTracker:
    """One tracker per instrument. Call record_tick(price, qty) for every
    LTP update; read .state.cumulative_delta at any point for the running
    session CVD."""

    def __init__(self):
        self.state = CvdState()

    def record_tick(self, price, qty):
        if qty <= 0:
            return  # a zero/negative quantity print carries no volume to classify

        if self.state.last_price is None:
            direction = self.state.last_direction  # first tick — no prior price to compare
        elif price > self.state.last_price:
            direction = 1
        elif price < self.state.last_price:
            direction = -1
        else:
            direction = self.state.last_direction  # unchanged price — continue prevailing side

        self.state.cumulative_delta += direction * qty
        self.state.last_price = price
        self.state.last_direction = direction
        self.state.tick_count += 1

    def record_from_feed_message(self, decoded_message, instrument_key):
        """Convenience wrapper: pull LTP/ltq for one instrument out of a
        decoded core.feed_listener message (as returned by
        decode_feed_message()) and record it."""
        feed = decoded_message.get("feeds", {}).get(instrument_key)
        if not feed:
            return
        full_feed = feed.get("fullFeed", {})
        market_ff = full_feed.get("marketFF") or full_feed.get("indexFF")
        if not market_ff:
            return
        ltpc = market_ff.get("ltpc", {})
        ltp = ltpc.get("ltp")
        ltq = ltpc.get("ltq")
        if ltp is not None and ltq is not None:
            self.record_tick(ltp, ltq)


def compute_candle_cvd(candles):
    """Candle-level CVD approximation for pages that only have historical
    OHLCV candles, not a live tick feed (e.g. a backtest/replay chart).

    This is NOT the same as the tick-rule CVD above — it's a coarser
    approximation: each candle's close is treated as one "tick" (compared
    to the previous candle's close) and the whole candle's volume is
    assigned to that single direction. A candle with a large range that
    actually saw trading on both sides gets collapsed to one signed
    delta. Real intraday tick data (via CvdTracker.record_tick per print)
    is more accurate; use this only where tick data isn't available.

    Returns a list of running cumulative-delta values, one per candle
    (same length as `candles`, first value = ±first candle's volume).
    """
    tracker = CvdTracker()
    series = []
    for c in candles:
        tracker.record_tick(c["close"], c["volume"])
        series.append(tracker.state.cumulative_delta)
    return series
