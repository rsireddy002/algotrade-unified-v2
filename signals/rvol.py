"""
signals/rvol.py — Relative Volume (RVOL).

Validated finding (vp-paper-trader): RVOL predicts magnitude of subsequent
movement (1.17-1.25x more movement in top deciles), NOT direction. Use it
to size expectations or filter for "something's happening", not as a
directional entry signal on its own.
"""

from dataclasses import dataclass


@dataclass
class RvolResult:
    current_volume: float
    average_volume: float
    rvol: float


def compute_rvol(candles, lookback=10):
    """RVOL of the most recent candle vs. the average of the prior
    `lookback` candles. Requires at least lookback + 1 candles."""
    if len(candles) < lookback + 1:
        raise ValueError(f"need at least {lookback + 1} candles, got {len(candles)}")

    current = candles[-1]
    window = candles[-(lookback + 1):-1]
    avg_volume = sum(c["volume"] for c in window) / len(window)
    if avg_volume <= 0:
        rvol = float("inf") if current["volume"] > 0 else 0.0
    else:
        rvol = current["volume"] / avg_volume

    return RvolResult(current_volume=current["volume"], average_volume=avg_volume, rvol=rvol)


def compute_rvol_series(candles, lookback=10):
    """RVOL for every candle from index `lookback` onward (each one vs.
    the average of its own prior `lookback` candles) — for plotting RVOL
    across a session rather than only reading the latest value. Returns a
    list of RvolResult aligned to candles[lookback:], one entry shorter
    than `candles` since the first `lookback` candles have no baseline."""
    if len(candles) < lookback + 1:
        return []
    return [compute_rvol(candles[: i + 1], lookback=lookback) for i in range(lookback, len(candles))]
