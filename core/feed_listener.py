"""
core/feed_listener.py — Upstox V3 Market Data Feed WebSocket client.

Handles REST authorization, WebSocket connect, subscribe/unsubscribe/
change_mode, protobuf decoding, and a staleness watchdog.

The int64-as-string casting below isn't a guess — it's confirmed by
actually compiling market_data_feed_v3.proto and round-tripping a synthetic
message through it: ltt/ltq/bidQ/askQ/vtt/currentTs all come back as JSON
strings from MessageToDict(), exactly matching the gotcha already recorded
from upstox-feed-listener. This module casts them back to int centrally so
every consumer downstream gets real integers, not strings to remember to
convert themselves.

The watchdog exists because of a real failure mode already hit in
production: the WebSocket handshake can succeed while zero ticks ever
arrive (reported independently by other Upstox users too, not just here).
Rather than fail silently, this logs a clear warning once the feed goes
quiet for too long, so it's caught in minutes, not discovered hours later
via a stale dashboard.

NOT tested against a live Upstox connection — this sandbox has no network
access to api.upstox.com or the wss feeder endpoint. The protobuf
schema/decoding IS tested (see the round-trip test that produced this
module). The actual websockets.connect() + auth + subscribe flow needs
your first real run to confirm.
"""

import asyncio
import json
import logging
import ssl
import time
import uuid

import certifi
import requests
import websockets
from google.protobuf.json_format import MessageToDict

from core.auth import UpstoxError, get_token
from core.proto import market_data_feed_v3_pb2 as pb

logger = logging.getLogger("feed_listener")

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

# Upstox serializes these int64 fields as JSON strings via MessageToDict —
# cast back to int here, once, rather than at every call site downstream.
_INT_FIELDS = {"ltt", "ltq", "bidQ", "askQ", "vtt", "currentTs"}

VALID_MODES = {"ltpc", "full_d5", "option_greeks", "full_d30"}


def get_authorized_ws_uri():
    """REST call to obtain the one-time-use authorized wss:// URI."""
    token = get_token()
    resp = requests.get(
        AUTHORIZE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
        timeout=15,
    )
    if not resp.ok:
        raise UpstoxError(f"Feed authorize failed {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    if payload.get("status") != "success":
        raise UpstoxError(f"Feed authorize returned an error: {str(payload)[:200]}")
    return payload["data"]["authorized_redirect_uri"]


def _cast_int_fields(obj):
    """Recursively cast known int64-as-string fields back to Python ints."""
    if isinstance(obj, dict):
        return {
            k: (int(v) if k in _INT_FIELDS and isinstance(v, str) else _cast_int_fields(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_cast_int_fields(item) for item in obj]
    return obj


def decode_feed_message(raw_bytes):
    """Decode one binary WebSocket message into a plain dict with correctly
    typed int fields (not left as strings)."""
    feed_response = pb.FeedResponse()
    feed_response.ParseFromString(raw_bytes)
    as_dict = MessageToDict(feed_response, preserving_proto_field_name=True)
    return _cast_int_fields(as_dict)


class FeedListener:
    """Manages one WebSocket connection, its subscriptions, and dispatches
    decoded messages to a callback.

    Two-tier subscription support matches the existing architecture: call
    subscribe(keys, mode="full_d30") for up to 50 priority symbols (Tier 1),
    and subscribe(keys, mode="full_d5") for the broader universe (Tier 2).
    Per Upstox V3 limits, full_d30 subscriptions are capped at 50 keys per
    connection — batch accordingly if you have more than that in Tier 1.
    """

    def __init__(self, on_message=None, on_status=None):
        self.on_message = on_message or (lambda msg: None)
        self.on_status = on_status or (lambda msg: logger.info(msg))
        self.ws = None
        self._pending_subscriptions = []
        self._last_message_at = None
        self._running = False

    async def connect(self):
        uri = get_authorized_ws_uri()
        # Use certifi's bundled CA certificates explicitly rather than
        # relying on the OS trust store lookup — on Windows this commonly
        # fails with "unable to get local issuer certificate" even though
        # the connection itself is otherwise fine.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.ws = await websockets.connect(uri, ssl=ssl_context, max_size=None)
        self.on_status(f"Connected to feed ({uri[:50]}...)")

        # Replay any subscribe() calls made before connect() finished.
        for instrument_keys, mode in self._pending_subscriptions:
            await self._send("sub", mode, instrument_keys)
        self._pending_subscriptions.clear()

    async def _send(self, method, mode, instrument_keys):
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode '{mode}' — must be one of {VALID_MODES}")
        message = {
            "guid": str(uuid.uuid4()),
            "method": method,
            "data": {"mode": mode, "instrumentKeys": instrument_keys},
        }
        # Must be sent as a binary frame, not a text message.
        await self.ws.send(json.dumps(message).encode("utf-8"))

    async def subscribe(self, instrument_keys, mode="full_d5"):
        if len(instrument_keys) > 50 and mode == "full_d30":
            raise ValueError(
                f"full_d30 supports at most 50 instrument keys per connection, got {len(instrument_keys)}"
            )
        if self.ws is None:
            self._pending_subscriptions.append((instrument_keys, mode))
            return
        await self._send("sub", mode, instrument_keys)

    async def unsubscribe(self, instrument_keys):
        await self._send("unsub", "ltpc", instrument_keys)

    async def change_mode(self, instrument_keys, mode):
        await self._send("change_mode", mode, instrument_keys)

    async def run_forever(self, staleness_warning_seconds=30):
        """Receive loop with a staleness watchdog running alongside it."""
        self._last_message_at = time.time()
        self._running = True
        watchdog_task = asyncio.create_task(self._watchdog(staleness_warning_seconds))
        try:
            async for raw in self.ws:
                self._last_message_at = time.time()
                try:
                    decoded = decode_feed_message(raw)
                except Exception as exc:  # noqa: BLE001 — one bad message shouldn't kill the loop
                    logger.error(f"Failed to decode feed message: {exc}")
                    continue
                self.on_message(decoded)
        finally:
            self._running = False
            watchdog_task.cancel()

    async def _watchdog(self, threshold_seconds):
        while self._running:
            await asyncio.sleep(5)
            if self._last_message_at and (time.time() - self._last_message_at) > threshold_seconds:
                self.on_status(
                    f"WARNING: no feed message received in over {threshold_seconds}s — "
                    "the connection may be silently stalled (handshake succeeded but "
                    "no ticks arriving — a known failure mode, not necessarily a bug "
                    "in this code)."
                )

    async def close(self):
        self._running = False
        if self.ws:
            await self.ws.close()
