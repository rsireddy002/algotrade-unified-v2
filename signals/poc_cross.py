"""
signals/poc_cross.py — Detects a stock's close price crossing the
VWAP/POC proxy level (see vwap.py's validated finding: the running
session VWAP is a reliable POC proxy, <0.1% median gap vs. the real
volume-profile POC).

A "cross" means the PREVIOUS candle's close sat on one side of the
running VWAP at that point in time, and the CURRENT candle's close sits
on the other side — not just "close is currently above/below the
level", which would fire on every single bar once price settles on one
side rather than only at the moment it actually crosses.
"""

from dataclasses import dataclass

from signals.vwap import session_vwap


@dataclass
class PocCrossSignal:
    symbol: str
    direction: str  # "above" or "below" — the side the close just moved to
    candle_date: str
    close: float
    poc_proxy: float


def detect_poc_cross(symbol, candles):
    """Returns a PocCrossSignal if the LATEST candle just crossed the
    running VWAP/POC proxy relative to the candle before it, else None.

    `candles` must be a single session's candles, already sliced to one
    trading day (e.g. via dashboard/app.py's _latest_day_candles) —
    session_vwap does not reset at day boundaries itself, so passing
    multi-day candles here would produce a spurious "cross" at the
    boundary between yesterday's last bar and today's first bar.

    Needs at least 2 candles; returns None otherwise (nothing to compare
    yet, e.g. the first bar of a fresh session)."""
    if len(candles) < 2:
        return None

    vwap_points = session_vwap(candles)
    prev_close, curr_close = candles[-2]["close"], candles[-1]["close"]
    prev_vwap, curr_vwap = vwap_points[-2].vwap, vwap_points[-1].vwap

    if prev_close < prev_vwap and curr_close > curr_vwap:
        return PocCrossSignal(symbol, "above", candles[-1]["date"], curr_close, curr_vwap)
    if prev_close > prev_vwap and curr_close < curr_vwap:
        return PocCrossSignal(symbol, "below", candles[-1]["date"], curr_close, curr_vwap)
    return None
