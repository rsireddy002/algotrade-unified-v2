"""
core/instruments.py — Instrument key resolution, for both:

1. The equity F&O universe (~200 stocks) — ported directly from
   breakout-scanner-streamlit, unchanged logic. Used for screening/scanning
   only, per the "full-universe for scanning, index-only for core
   strategies" scope decision.

2. Nifty/BankNifty futures front-month resolution — NEW code, not a port
   of your existing contract_resolver.py (that file's exact 4-layer
   fallback logic wasn't available to reuse here). This is a simpler
   2-tier version: primary lookup via the instrument master, with a
   fallback that just re-downloads a fresh master if the cached one looks
   stale (e.g. the resolved contract already expired). Swap in your
   original contract_resolver.py if you'd rather keep using exactly what's
   already validated in upstox-feed-listener.
"""

import gzip
import json
import os
import time
from datetime import datetime, date

from core.auth import UpstoxError, upstox_get
from core.config import INDEX_NAMES, INSTRUMENT_MASTER_URL, IST

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, "data_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

FNO_UNIVERSE_CACHE_FILE = os.path.join(_CACHE_DIR, "fno_universe_cache.json")
FNO_UNIVERSE_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60

_instrument_master_cache = None  # in-memory, per-process
_equity_key_cache = {}
_futures_key_cache = {}


# ---------------------------------------------------------------------------
# Instrument master (shared download, used by both equity and futures paths)
# ---------------------------------------------------------------------------

def _download_instrument_master():
    """Download and parse Upstox's public instrument master (gzip JSON, no auth needed)."""
    import requests

    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=120)
    resp.raise_for_status()
    return json.loads(gzip.decompress(resp.content))


def _get_instrument_master(force_refresh=False):
    """Cache the full instrument master in-process for the life of the run —
    both equity universe building and futures resolution need to scan it,
    and it's a ~30-50MB download best done once per process, not per call."""
    global _instrument_master_cache
    if _instrument_master_cache is None or force_refresh:
        _instrument_master_cache = _download_instrument_master()
    return _instrument_master_cache


# ---------------------------------------------------------------------------
# Equity F&O universe (ported from breakout-scanner-streamlit, unchanged)
# ---------------------------------------------------------------------------

def _build_fno_tickers(instruments):
    fo_underlying_names = set()
    for inst in instruments:
        if inst.get("segment") == "NSE_FO" and inst.get("instrument_type") == "FUT":
            name = (inst.get("name") or "").upper()
            if name and name not in INDEX_NAMES:
                fo_underlying_names.add(name)

    tickers = set()
    for inst in instruments:
        if inst.get("segment") == "NSE_EQ" and inst.get("instrument_type") == "EQ":
            name = (inst.get("name") or "").upper()
            if name in fo_underlying_names:
                trading_symbol = inst.get("trading_symbol")
                if trading_symbol:
                    tickers.add(trading_symbol)

    return sorted(tickers)


def get_fno_tickers(force_refresh=False):
    """Load the current F&O equity universe, using the local 24h cache when fresh."""
    if not force_refresh and os.path.exists(FNO_UNIVERSE_CACHE_FILE):
        with open(FNO_UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        age = time.time() - cached.get("generated_at", 0)
        if age < FNO_UNIVERSE_CACHE_MAX_AGE_SECONDS and cached.get("tickers"):
            return cached["tickers"]

    instruments = _get_instrument_master(force_refresh=force_refresh)
    tickers = _build_fno_tickers(instruments)

    with open(FNO_UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.time(), "tickers": tickers}, f)

    return tickers


def resolve_equity_key(symbol):
    """NSE_EQ instrument key for a cash equity symbol, via Upstox's
    lightweight search endpoint (not the full master — faster for one-off
    lookups)."""
    if symbol in _equity_key_cache:
        return _equity_key_cache[symbol]

    url = "https://api.upstox.com/v2/instruments/search"
    payload = upstox_get(url, params={"query": symbol, "exchanges": "NSE", "segments": "EQ"})
    matches = payload.get("data", [])

    exact = next(
        (
            m for m in matches
            if m.get("segment") == "NSE_EQ" and (m.get("trading_symbol") or "").upper() == symbol.upper()
        ),
        None,
    )
    if not exact:
        raise UpstoxError(f'Could not resolve an NSE_EQ instrument key for "{symbol}"')

    _equity_key_cache[symbol] = exact["instrument_key"]
    return exact["instrument_key"]


# ---------------------------------------------------------------------------
# Futures front-month resolution (NEW — see module docstring caveat above)
# ---------------------------------------------------------------------------

def _parse_expiry(inst):
    """Normalize an instrument's expiry to epoch-ms, regardless of whether
    Upstox returns it as an int or an ISO date string."""
    expiry = inst.get("expiry")
    if expiry is None:
        return None
    if isinstance(expiry, (int, float)):
        return int(expiry)
    try:
        return int(
            datetime.combine(date.fromisoformat(str(expiry)[:10]), datetime.min.time())
            .replace(tzinfo=IST)
            .timestamp()
            * 1000
        )
    except Exception:
        return None


def resolve_futures_key(underlying, force_refresh=False):
    """Front-month (soonest non-expired) futures instrument_key for an
    index/stock underlying, e.g. "NIFTY" or "BANKNIFTY".

    Tier 1: scan the instrument master for all live NSE_FO FUT contracts
    matching this underlying, pick the soonest expiry that hasn't passed.
    Tier 2 (fallback): if Tier 1 finds nothing (e.g. the cached master is
    stale right after a monthly rollover), force a fresh download and
    retry once.
    """
    cache_key = underlying.upper()
    if not force_refresh and cache_key in _futures_key_cache:
        cached_key, cached_expiry_ms = _futures_key_cache[cache_key]
        if cached_expiry_ms is None or cached_expiry_ms > time.time() * 1000:
            return cached_key
        # cached contract has expired — fall through and re-resolve

    instruments = _get_instrument_master(force_refresh=force_refresh)
    now_ms = time.time() * 1000

    candidates = []
    for inst in instruments:
        if inst.get("segment") != "NSE_FO" or inst.get("instrument_type") != "FUT":
            continue
        if (inst.get("name") or "").upper() != cache_key:
            continue
        expiry_ms = _parse_expiry(inst)
        instrument_key = inst.get("instrument_key")
        if instrument_key:
            candidates.append((expiry_ms, instrument_key))

    upcoming = [c for c in candidates if c[0] is not None and c[0] >= now_ms]

    if not upcoming and not force_refresh:
        # Tier 2 fallback: cached master may be stale right after rollover.
        return resolve_futures_key(underlying, force_refresh=True)

    if not upcoming:
        raise UpstoxError(
            f'Could not resolve a live front-month futures contract for "{underlying}" '
            "even after a fresh instrument master download"
        )

    expiry_ms, instrument_key = min(upcoming, key=lambda c: c[0])
    _futures_key_cache[cache_key] = (instrument_key, expiry_ms)
    return instrument_key
