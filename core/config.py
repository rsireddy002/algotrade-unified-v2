"""
core/config.py — Shared constants used across every module in the platform.
"""

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

UPSTOX_BASE = "https://api.upstox.com"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

# Index futures never have a corresponding NSE_EQ instrument.
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

# Per your stated intent: core strategies (morning fade, CVD/footprint, value
# area) run on these two only. The F&O equity universe (get_fno_tickers in
# instruments.py) remains separate and is for screening/scanning purposes,
# not for running the index-only strategies against every stock.
CORE_INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY"]

MARKET_OPEN = (9, 15)   # (hour, minute) IST
MARKET_CLOSE = (15, 30)  # (hour, minute) IST
