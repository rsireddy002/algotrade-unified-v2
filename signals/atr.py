"""
signals/atr.py — Average True Range, computed on DAILY candles.

Validated fix (hvn-lvn-scanner): ATR switched from 5-minute to daily bars
for realistic stop/target sizing — 5-min ATR badly understates real
overnight-inclusive volatility. Kept as daily-only here on purpose; don't
reintroduce an intraday-bar version without re-validating that decision.
"""

from dataclasses import dataclass


@dataclass
class AtrResult:
    atr: float
    period: int


def compute_atr(daily_candles, period=14):
    """Wilder's ATR over the given daily candles. Requires at least
    period + 1 candles (needs a previous close for the first true range).
    Seeds from a simple average of the first `period` true ranges, then
    smooths forward through every subsequent true range — the standard
    Wilder method, which converges toward the "true" value as more history
    is smoothed through it (so pass generous history, not just period+1
    candles, for an accurate current reading)."""
    if len(daily_candles) < period + 1:
        raise ValueError(f"need at least {period + 1} daily candles, got {len(daily_candles)}")

    true_ranges = []
    for i in range(1, len(daily_candles)):
        high = daily_candles[i]["high"]
        low = daily_candles[i]["low"]
        prev_close = daily_candles[i - 1]["close"]
        true_range = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(true_range)

    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return AtrResult(atr=atr, period=period)
