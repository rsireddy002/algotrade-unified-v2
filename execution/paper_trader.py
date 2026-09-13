"""
execution/paper_trader.py — Paper trading engine.

Two validated fixes carried forward from hvn-lvn-scanner, both kept
unchanged on purpose:
  1. EOD square-off at 15:30 IST for any still-open trade.
  2. ATR-based stop/target sizing uses DAILY bars, not 5-minute — 5-min
     ATR badly understates real volatility for sizing purposes.

Exit/risk rules were explicitly flagged in your own notes as "the weakest
system component" of the original morning-fade work — this engine keeps
exits intentionally simple (fixed stop from the strategy signal, ATR-based
target, EOD square-off) rather than inventing a more sophisticated scheme
that hasn't been backtested.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dt_time

from core.config import IST

EOD_SQUARE_OFF_TIME = dt_time(15, 30)  # validated


@dataclass
class PaperTrade:
    ticker: str
    direction: str  # "long" or "short"
    entry_price: float
    stop_price: float
    target_price: float
    entry_time: str
    exit_price: float = None
    exit_time: str = None
    exit_reason: str = None  # "stop", "target", "eod_square_off"
    pnl_pct: float = None

    @property
    def is_open(self):
        return self.exit_price is None


class PaperTradeLog:
    """Persistent JSON log of paper trades. Atomic writes (temp file +
    rename), same pattern as execution/signal_store.py, for the same
    crash-safety reason."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.trades = []
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trades = [PaperTrade(**t) for t in data.get("trades", [])]
        else:
            self.trades = []

    def _save(self):
        directory = os.path.dirname(os.path.abspath(self.filepath)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"trades": [asdict(t) for t in self.trades]}, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def open_trade(self, ticker, direction, entry_price, stop_price, target_price, entry_time):
        trade = PaperTrade(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            entry_time=entry_time,
        )
        self.trades.append(trade)
        self._save()
        return trade

    def open_trades(self):
        return [t for t in self.trades if t.is_open]

    def _close(self, trade, exit_price, exit_time, exit_reason):
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason
        if trade.direction == "short":
            trade.pnl_pct = (trade.entry_price - exit_price) / trade.entry_price * 100
        else:
            trade.pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
        self._save()

    def check_exits(self, current_prices, current_datetime):
        """Check every open trade against `current_prices` (a dict of
        ticker -> current price) and the current time, closing any that
        hit their stop, hit their target, or need EOD square-off.
        `current_datetime` should be timezone-aware; naive datetimes are
        assumed to already be in IST."""
        if current_datetime.tzinfo is None:
            current_datetime = current_datetime.replace(tzinfo=IST)
        current_time = current_datetime.astimezone(IST).time()
        is_eod = current_time >= EOD_SQUARE_OFF_TIME

        for trade in self.open_trades():
            price = current_prices.get(trade.ticker)
            if price is None:
                continue

            if is_eod:
                self._close(trade, price, current_datetime.isoformat(), "eod_square_off")
                continue

            if trade.direction == "short":
                if price >= trade.stop_price:
                    self._close(trade, trade.stop_price, current_datetime.isoformat(), "stop")
                elif trade.target_price is not None and price <= trade.target_price:
                    self._close(trade, trade.target_price, current_datetime.isoformat(), "target")
            else:  # long
                if price <= trade.stop_price:
                    self._close(trade, trade.stop_price, current_datetime.isoformat(), "stop")
                elif trade.target_price is not None and price >= trade.target_price:
                    self._close(trade, trade.target_price, current_datetime.isoformat(), "target")

    def summary(self):
        closed = [t for t in self.trades if not t.is_open]
        if not closed:
            return {"total_trades": 0, "win_rate": None, "avg_pnl_pct": None}
        wins = [t for t in closed if t.pnl_pct > 0]
        return {
            "total_trades": len(closed),
            "win_rate": len(wins) / len(closed),
            "avg_pnl_pct": sum(t.pnl_pct for t in closed) / len(closed),
        }


def atr_based_target(entry_price, atr, direction, atr_multiple=2.0):
    """A simple ATR-multiple target — deliberately simple, not a validated
    scheme (see module docstring: exit/risk rules were flagged as the
    weakest part of the original system)."""
    if direction == "short":
        return entry_price - atr * atr_multiple
    return entry_price + atr * atr_multiple
