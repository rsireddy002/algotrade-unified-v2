"""
core/tick_broadcaster.py

Embeds a WebSocket server directly inside run_live_scanner_feed.py's
own asyncio event loop, so each tick reaches connected browsers the
moment on_message() processes it -- no polling, no 5-second snapshot
delay, and no dependency on the existing 5-min `aggregator` (which
is built for the dashboard's historical charts, not per-tick pushes).

Builds its own lightweight 1-minute live candle per symbol internally
-- O(1) per tick, safe to call on every single tick across the full
~200-symbol F&O universe.

INTEGRATION -- two small additions to run_live_scanner_feed.py:

1. Near the top, alongside your other `from core...` imports:

    from core.tick_broadcaster import TickBroadcaster
    broadcaster = TickBroadcaster()

2. Inside main(), right where dump_task is created, add a sibling line:

    dump_task = asyncio.create_task(dump_loop())
    broadcast_task = asyncio.create_task(broadcaster.start(host="0.0.0.0", port=8765))

3. Inside on_message(), right after the existing cvd tracker block
   (after `tracker.record_tick(ltp, ltq)`), add:

    broadcaster.push_tick(symbol, ltp, ltq or 0)

That's it -- one process, no file polling. Run run_live_scanner_feed.py
exactly as you do today; it now also serves ws://<this-machine>:8765
for the browser chart.
"""

import asyncio
import json
import queue
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set

import websockets

TIMEFRAME_SECONDS = 60  # 1-minute candles


@dataclass
class _LiveBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return {
            "time": self.ts, "open": self.open, "high": self.high,
            "low": self.low, "close": self.close, "volume": self.volume,
        }


class TickBroadcaster:
    def __init__(self, timeframe_seconds: int = TIMEFRAME_SECONDS):
        self._clients: Set = set()
        self._queue: "queue.Queue" = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._timeframe = timeframe_seconds
        self._live_bars: Dict[str, _LiveBar] = {}

    def push_tick(self, symbol: str, price: float, volume: float = 0.0, ts: Optional[float] = None):
        """
        Call this from on_message right after each tick. Builds/updates
        this symbol's 1-min live bar in O(1), then queues it for
        broadcast. Safe to call from any thread.
        """
        if ts is None:
            ts = time.time()
        bucket_start = int(ts // self._timeframe * self._timeframe)

        current = self._live_bars.get(symbol)
        closed_bar = None

        if current is None or bucket_start != current.ts:
            if current is not None:
                closed_bar = current
            current = _LiveBar(ts=bucket_start, open=price, high=price, low=price, close=price, volume=volume)
            self._live_bars[symbol] = current
        else:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            current.volume += volume

        message = {"type": "bar_update", "symbol": symbol, "bar": current.to_dict()}
        self._enqueue(message)
        if closed_bar is not None:
            self._enqueue({"type": "bar_closed", "symbol": symbol, "bar": closed_bar.to_dict()})

    def _enqueue(self, message: dict):
        if self._loop is None:
            return  # broadcaster not started yet -- drop rather than block on_message
        self._loop.call_soon_threadsafe(self._queue.put_nowait, message)

    async def _drain_loop(self):
        while True:
            try:
                while True:
                    message = self._queue.get_nowait()
                    await self._broadcast(json.dumps(message))
            except queue.Empty:
                pass
            await asyncio.sleep(0.02)  # 20ms -- effectively instant to a human eye

    async def _broadcast(self, payload: str):
        dead = []
        for ws in self._clients:
            try:
                await ws.send(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _handler(self, ws):
        self._clients.add(ws)
        try:
            # send whatever live bars already exist so a newly-connected
            # browser isn't blank until the next tick
            for symbol, bar in self._live_bars.items():
                await ws.send(json.dumps({"type": "bar_update", "symbol": symbol, "bar": bar.to_dict()}))
            async for _ in ws:
                pass  # browser doesn't need to send anything back
        finally:
            self._clients.discard(ws)

    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        """Run as a background task inside the SAME event loop as the
        rest of run_live_scanner_feed.py -- see integration notes above."""
        self._loop = asyncio.get_running_loop()
        asyncio.create_task(self._drain_loop())
        async with websockets.serve(self._handler, host, port):
            await asyncio.Future()  # run forever, alongside your other tasks
