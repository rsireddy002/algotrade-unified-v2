"""
signals/breakout.py — Breakout screen, ported from breakout-scanner-streamlit.

Same 7-check logic (check 6, market cap, remains dropped — Upstox doesn't
expose fundamentals) and the same calibrated thresholds validated against
live data earlier — but now built on core/candle_store.py and
core/instruments.py instead of its own standalone HTTP calls, so it shares
the same retry/rate-limit handling and instrument resolution as every
other signal in this platform.
"""

from dataclasses import dataclass

from core.candle_store import get_daily_candles, get_intraday_candles
from core.instruments import resolve_equity_key

CANDLE_UNIT = "minutes"
CANDLE_INTERVAL = 5

CONSOLIDATION_LOOKBACK = 10
CONSOLIDATION_MAX_RANGE_PCT = 1.5
BREAKOUT_ABOVE_RANGE_PCT = 0.3
BREAKOUT_SIZE_MIN_PCT = 0.3
RELATIVE_VOLUME_MIN = 1.5
LIQUIDITY_MIN_AVG_VOL = 500_000
PRICE_LEVEL_WITHIN_PCT = 10


@dataclass
class BreakoutResult:
    ticker: str
    price: float
    breakout_pct: float
    breakout_size_pct: float
    relative_volume: float
    pct_from_high: float
    consolidation_high: float
    consolidation_low: float
    signal_time: str


def screen_ticker(symbol, instrument_key=None):
    """Run the breakout screen on one ticker. Returns a BreakoutResult, or
    None if it fails any check. `instrument_key` can be passed directly to
    skip resolution (e.g. for NIFTY/BANKNIFTY futures via
    core.instruments.resolve_futures_key) — otherwise resolves as an NSE
    equity symbol."""
    if instrument_key is None:
        instrument_key = resolve_equity_key(symbol)

    candles = get_intraday_candles(instrument_key, unit=CANDLE_UNIT, interval=CANDLE_INTERVAL)
    if len(candles) < CONSOLIDATION_LOOKBACK + 1:
        return None

    current = candles[-1]
    lookback = candles[-(CONSOLIDATION_LOOKBACK + 1):-1]

    consolidation_high = max(max(c["open"], c["close"]) for c in lookback)
    consolidation_low = min(min(c["open"], c["close"]) for c in lookback)
    consolidation_range_pct = (consolidation_high - consolidation_low) / consolidation_low * 100
    if consolidation_range_pct > CONSOLIDATION_MAX_RANGE_PCT:
        return None

    if current["close"] < consolidation_high * (1 + BREAKOUT_ABOVE_RANGE_PCT / 100):
        return None
    breakout_pct = (current["close"] - consolidation_high) / consolidation_high * 100

    breakout_size_pct = abs(current["close"] - current["open"]) / current["open"] * 100
    if breakout_size_pct < BREAKOUT_SIZE_MIN_PCT:
        return None

    avg_vol_lookback = sum(c["volume"] for c in lookback) / len(lookback)
    if avg_vol_lookback <= 0:
        return None
    relative_volume = current["volume"] / avg_vol_lookback
    if relative_volume < RELATIVE_VOLUME_MIN:
        return None

    daily_candles = get_daily_candles(instrument_key)
    if len(daily_candles) < 50:
        return None

    last_20_vol = [c["volume"] for c in daily_candles[-20:]]
    avg_vol_20 = sum(last_20_vol) / len(last_20_vol)
    if avg_vol_20 < LIQUIDITY_MIN_AVG_VOL:
        return None

    closes = [c["close"] for c in daily_candles]
    high_20d = max(closes[-20:])
    high_50d = max(closes[-50:])
    price_level_floor = 1 - PRICE_LEVEL_WITHIN_PCT / 100
    within_high_20 = current["close"] >= high_20d * price_level_floor
    within_high_50 = current["close"] >= high_50d * price_level_floor
    if not within_high_20 and not within_high_50:
        return None
    pct_from_high_20 = (current["close"] - high_20d) / high_20d * 100
    pct_from_high_50 = (current["close"] - high_50d) / high_50d * 100
    pct_from_high = (
        pct_from_high_20 if abs(pct_from_high_20) <= abs(pct_from_high_50) else pct_from_high_50
    )

    sma_20 = sum(closes[-20:]) / 20
    sma_50 = sum(closes[-50:]) / 50
    if not (current["close"] > sma_20 and current["close"] > sma_50):
        return None

    return BreakoutResult(
        ticker=symbol,
        price=current["close"],
        breakout_pct=breakout_pct,
        breakout_size_pct=breakout_size_pct,
        relative_volume=relative_volume,
        pct_from_high=pct_from_high,
        consolidation_high=consolidation_high,
        consolidation_low=consolidation_low,
        signal_time=current["date"],
    )
