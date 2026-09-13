"""
strategies/morning_fade.py — Morning fade (mean-reversion short) strategy.

NOT a port of your original backtest_fade.py — that file's exact logic
wasn't available to reuse here. This is a new implementation built from
the validated parameters you'd already backtested (vp-paper-trader):

    min_move=0.3%, stop=1.0%, window=09:30-10:30 IST
    -> +0.21R/trade, 65% win rate across ~78 trades
    -> 24/27 parameter combinations positive in the sweep
    -> RVOL predicts magnitude (1.17-1.25x), NOT direction — it's used
       here as a ranking/filter signal, not a directional one
    -> exit/risk rules were flagged as the weakest part of the original
       system — this module's exit logic (fixed stop, EOD square-off) is
       intentionally simple rather than guessing at a more sophisticated
       scheme you hadn't validated

Thesis: stocks that have already moved up sharply from the open, on high
relative volume, within the first hour of trading, tend to mean-revert
(fade) rather than continue — this is a SHORT-only strategy on the
qualifying names.

RVOL_MIN below is a NEW default I'm introducing (not in the validated
parameter set, which only specified min_move/stop/window) — the original
finding was about RVOL's top-decile effect, not a specific cutoff. Treat
this threshold as unvalidated until you backtest it.
"""

from dataclasses import dataclass
from datetime import datetime, time as dt_time

from core.config import IST
from signals.rvol import compute_rvol

WINDOW_START = dt_time(9, 30)
WINDOW_END = dt_time(10, 30)
MIN_MOVE_PCT = 0.3   # validated
STOP_PCT = 1.0       # validated
RVOL_MIN = 1.5        # NEW default, not validated — see module docstring


@dataclass
class FadeSignal:
    ticker: str
    direction: str  # always "short" for this strategy
    entry_price: float
    stop_price: float
    move_from_open_pct: float
    rvol: float
    signal_time: str


def _in_entry_window(candle_datetime):
    t = candle_datetime.astimezone(IST).time()
    return WINDOW_START <= t <= WINDOW_END


def evaluate_fade_candidate(
    ticker,
    candles,
    open_price,
    rvol_lookback=10,
    min_move_pct=MIN_MOVE_PCT,
    stop_pct=STOP_PCT,
    rvol_min=RVOL_MIN,
):
    """Check whether the most recent candle qualifies as a morning-fade
    short candidate. Returns a FadeSignal, or None if it doesn't qualify.

    `open_price` is the day's opening price (first candle's open) — passed
    in explicitly rather than re-derived, since callers typically already
    have the day's candles loaded and shouldn't need to re-fetch or
    re-slice them here.
    """
    if len(candles) < rvol_lookback + 1:
        return None

    current = candles[-1]
    candle_dt = datetime.fromisoformat(current["date"])
    if not _in_entry_window(candle_dt):
        return None

    move_from_open_pct = (current["close"] - open_price) / open_price * 100
    if move_from_open_pct < min_move_pct:
        return None  # hasn't moved up enough to qualify as an extended/overbought candidate

    rvol_result = compute_rvol(candles, lookback=rvol_lookback)
    if rvol_result.rvol < rvol_min:
        return None

    entry_price = current["close"]
    stop_price = entry_price * (1 + stop_pct / 100)  # stop ABOVE entry — this is a short

    return FadeSignal(
        ticker=ticker,
        direction="short",
        entry_price=entry_price,
        stop_price=stop_price,
        move_from_open_pct=move_from_open_pct,
        rvol=rvol_result.rvol,
        signal_time=current["date"],
    )
