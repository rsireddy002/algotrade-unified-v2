"""
scripts/run_feed_listener.py — Live smoke test for the WebSocket feed
listener. Subscribes to NIFTY + BANKNIFTY futures in full_d30 mode and
prints every tick as it arrives.

This is the actual test of the one thing that couldn't be verified in a
sandboxed environment: does the real Upstox WebSocket connection, auth
flow, and subscribe handshake work end-to-end. The protobuf decoding
itself is already tested (see core/feed_listener.py's docstring) — this
script is about the network/auth/subscribe plumbing around it.

Run:
    python scripts/run_feed_listener.py

Stop with Ctrl+C. Watch for:
- "Connected to feed" — auth + WebSocket handshake succeeded
- Tick output starting within a few seconds of subscribing (during market
  hours) — if this doesn't happen, that's the known "connected but zero
  ticks" failure mode already seen before; the watchdog will log a warning
  after 30s of silence either way.
"""

import asyncio
import logging
import sys

from core.feed_listener import FeedListener
from core.instruments import resolve_futures_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def on_message(decoded):
    for instrument_key, feed in decoded.get("feeds", {}).items():
        full_feed = feed.get("fullFeed", {})
        market_ff = full_feed.get("marketFF") or full_feed.get("indexFF")
        if not market_ff:
            continue
        ltpc = market_ff.get("ltpc", {})
        print(f"{instrument_key}: LTP={ltpc.get('ltp')} vol_today={market_ff.get('vtt')}")


def on_status(message):
    print(f"[status] {message}")


async def main():
    print("Resolving NIFTY and BANKNIFTY front-month futures keys...")
    nifty_key = resolve_futures_key("NIFTY")
    banknifty_key = resolve_futures_key("BANKNIFTY")
    print(f"NIFTY: {nifty_key}")
    print(f"BANKNIFTY: {banknifty_key}")

    listener = FeedListener(on_message=on_message, on_status=on_status)

    print("\nConnecting to Upstox WebSocket feed...")
    await listener.connect()

    print("Subscribing in full_d30 mode...")
    await listener.subscribe([nifty_key, banknifty_key], mode="full_d30")

    print("\nListening for ticks (Ctrl+C to stop)...\n")
    try:
        await listener.run_forever(staleness_warning_seconds=30)
    except KeyboardInterrupt:
        pass
    finally:
        await listener.close()
        print("\nConnection closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
