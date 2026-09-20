"""
scripts/run_live_scanner_feed.py — Persistent live-feed service for the
Scanner Grid.

Subscribes to the full F&O equity universe (~200 stocks) over Upstox's
WebSocket feed, aggregates ticks into 5-min candles via
core/candle_aggregator.py, tracks true tick-rule CVD per symbol via
signals/cvd.py's CvdTracker (now finally fed real ticks, not the
candle-level approximation the Levels tab and Scanner Grid use today),
and periodically dumps a snapshot to a shared JSON file that
dashboard/app.py can read instead of REST-polling ~200 symbols per
refresh.

This REPLACES scripts/run_feed_listener.py in production (that script
was only ever a 2-symbol smoke test to confirm the WebSocket pipeline
works at all — see conversation history / that script's own docstring).
Keep run_feed_listener.py around for future debugging, but point the
systemd service at this one instead (see deploy/algotrade-livefeed.service).

UNVERIFIED AT THIS SCALE: the smoke test confirmed 2 symbols in
full_d30 mode work end-to-end. Subscribing ~200 keys in full_d5 mode
(required — full_d30 is capped at 50 keys/connection per
core/feed_listener.py) has NOT been run against live Upstox yet. Watch
the first real run closely: confirm ticks arrive for a broad sample of
symbols, not just a few, and watch for any rate-limit/rejection message
from Upstox at subscribe time.

OUTPUT:
    data_cache/live_candles.json — updated every DUMP_INTERVAL_SECONDS:
    {
      "generated_at": <epoch seconds>,
      "symbols": {
        "<SYMBOL>": {
          "candles": [{"date": iso, "open", "high", "low", "close", "volume"}, ...],
          "cvd": <float, true tick-rule cumulative delta since this process started>
        },
        ...
      }
    }

Run:
    python -m scripts.run_live_scanner_feed
Stop with Ctrl+C.
"""

import asyncio
import json
import logging
import os
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

from core.candle_aggregator import CandleAggregator
from core.config import IST
from core.feed_listener import FeedListener
from core.instruments import get_fno_tickers, resolve_equity_key
from signals.cvd import CvdTracker
from core.tick_broadcaster import TickBroadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_live_scanner_feed")

OUTPUT_PATH = os.environ.get(
    "LIVE_CANDLES_OUTPUT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_cache", "live_candles.json"),
)
DUMP_INTERVAL_SECONDS = 5

aggregator = CandleAggregator()
broadcaster = TickBroadcaster()
cvd_trackers = {}  # symbol -> CvdTracker
symbol_by_key = {}  # instrument_key -> symbol


def resolve_universe():
    """Build the symbol -> instrument_key map for the full F&O universe.
    Same Cloudflare-safe threaded pattern already used for the REST scans
    in dashboard/app.py (Breakout Scanner, Scanner Grid)."""
    tickers = get_fno_tickers()
    logger.info(f"Resolving instrument keys for {len(tickers)} F&O tickers...")
    resolved = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(resolve_equity_key, t): t for t in tickers}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                resolved[symbol] = future.result()
            except Exception as exc:
                logger.warning(f"Could not resolve {symbol}: {exc}")
    logger.info(f"Resolved {len(resolved)}/{len(tickers)} tickers.")
    return resolved


def on_message(decoded):
    for instrument_key, feed in decoded.get("feeds", {}).items():
        symbol = symbol_by_key.get(instrument_key)
        if not symbol:
            continue
        full_feed = feed.get("fullFeed", {})
        market_ff = full_feed.get("marketFF") or full_feed.get("indexFF")
        if not market_ff:
            continue
        ltpc = market_ff.get("ltpc", {})
        ltp = ltpc.get("ltp")
        ltq = ltpc.get("ltq")
        vtt = market_ff.get("vtt")  # cumulative day volume
        if ltp is None:
            continue

        aggregator.on_tick(symbol, ltp, vtt)

        if ltq is not None:
            tracker = cvd_trackers.setdefault(symbol, CvdTracker())
            tracker.record_tick(ltp, ltq)

        broadcaster.push_tick(symbol, ltp, ltq or 0)


def on_status(message):
    logger.info(f"[status] {message}")


def dump_snapshot():
    """Writes the aggregator + CVD state to OUTPUT_PATH. Atomic write
    (temp file + rename), same crash-safety pattern as
    execution/paper_trader.py and execution/signal_store.py, so the
    Streamlit app never reads a half-written file."""
    candle_snapshot = aggregator.snapshot()
    payload = {
        "generated_at": time.time(),
        "symbols": {
            symbol: {
                "candles": candles,
                "cvd": cvd_trackers[symbol].state.cumulative_delta if symbol in cvd_trackers else 0.0,
            }
            for symbol, candles in candle_snapshot.items()
        },
    }
    directory = os.path.dirname(OUTPUT_PATH)
    os.makedirs(directory, exist_ok=True)
    tmp_path = OUTPUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, OUTPUT_PATH)


async def dump_loop():
    while True:
        try:
            dump_snapshot()
        except Exception as exc:
            logger.error(f"Snapshot dump failed: {exc}")
        await asyncio.sleep(DUMP_INTERVAL_SECONDS)


async def main():
    universe = resolve_universe()
    if not universe:
        logger.error("No tickers resolved — nothing to subscribe to. Exiting.")
        return

    global symbol_by_key
    symbol_by_key = {key: symbol for symbol, key in universe.items()}
    all_keys = list(universe.values())

    listener = FeedListener(on_message=on_message, on_status=on_status)

    logger.info("Connecting to Upstox WebSocket feed...")
    await listener.connect()

    # full_d5, not full_d30 — full_d30 is capped at 50 keys/connection
    # (see core/feed_listener.py), and the F&O universe is ~200 stocks.
    # UNVERIFIED: whether Upstox accepts ~200 keys in one full_d5
    # subscribe call, or whether it needs batching too — watch the first
    # real run's [status] messages closely for any rejection.
    logger.info(f"Subscribing to {len(all_keys)} symbols in full_d5 mode...")
    await listener.subscribe(all_keys, mode="full_d5")

    dump_task = asyncio.create_task(dump_loop())
    broadcast_task = asyncio.create_task(broadcaster.start(host="0.0.0.0", port=8765))

    logger.info(f"Listening for ticks — writing snapshots to {OUTPUT_PATH} every {DUMP_INTERVAL_SECONDS}s...")
    try:
        await listener.run_forever(staleness_warning_seconds=30)
    finally:
        dump_task.cancel()
        await listener.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
