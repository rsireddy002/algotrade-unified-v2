"""
dashboard/app.py — Unified trading dashboard.

Ties together every phase built so far:
  - core/          : instrument resolution, candle fetching
  - signals/       : RVOL, VWAP/POC, volume profile, value area, breakout
  - strategies/    : morning fade
  - execution/     : signal dedup, paper trading, LLM gatekeeper

Styled after the original "AlgoTrade Pro" mockup (dark slate/emerald/rose
theme) — same visual language as breakout-scanner-streamlit, now backed by
the unified platform instead of a standalone screener.

Scanner Grid can optionally read from a live-aggregated candle feed
(scripts/run_live_scanner_feed.py -> data_cache/live_candles.json) instead
of REST-polling, via core/live_candle_reader.py, with automatic fallback
to REST when that file is missing/stale — e.g. when deployed somewhere
(like Streamlit Community Cloud) that can't run a separate persistent
background process. Everything else always runs on polled
historical/intraday candles, same as before.

Run locally:
    streamlit run dashboard/app.py

Deployed on Streamlit Community Cloud: secrets (UPSTOX_ACCESS_TOKEN etc.)
come from Cloud's Secrets manager, not a .env file (that's gitignored on
purpose) — the block right after the imports below bridges st.secrets
into os.environ so core/auth.py's existing os.getenv() calls keep working
unchanged, locally or deployed.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Bridge Streamlit Cloud's Secrets manager into os.environ, so every
# existing os.getenv("UPSTOX_ACCESS_TOKEN")-style call (core/auth.py, etc.)
# keeps working unchanged whether running locally (.env file) or deployed
# (Cloud secrets). Harmless no-op locally: st.secrets is empty when no
# secrets.toml exists, and .setdefault() never overwrites a real .env value.
try:
    for _key, _value in st.secrets.items():
        os.environ.setdefault(_key, str(_value))
except Exception:
    pass  # no secrets configured at all (e.g. fresh local clone, no .env yet) — fine, core.auth will raise its own clear error if a token is actually needed and missing

from core.candle_store import get_daily_candles, get_intraday_candles
from core.config import CORE_INDEX_SYMBOLS, IST
from core.instruments import get_fno_tickers, resolve_equity_key, resolve_futures_key
from core.live_candle_reader import LiveFeedUnavailable, get_live_candles, live_feed_status
from execution.paper_trader import PaperTradeLog, atr_based_target
from execution.signal_store import SignalStore
from execution.telegram_alert import send_telegram_alert
from signals.atr import compute_atr
from signals.breakout import screen_ticker as breakout_screen
from signals.cvd import compute_candle_cvd
from signals.poc_cross import detect_poc_cross
from signals.rvol import compute_rvol, compute_rvol_series
from signals.value_area import compute_value_area
from signals.volume_profile import build_volume_profile
from signals.vwap import session_vwap, vwap_poc_proxy
from strategies.morning_fade import evaluate_fade_candidate

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data_cache")
os.makedirs(_DATA_DIR, exist_ok=True)
SIGNAL_STORE_PATH = os.path.join(_DATA_DIR, "dashboard_signals.json")
TRADE_LOG_PATH = os.path.join(_DATA_DIR, "dashboard_trades.json")
POC_ALERT_STORE_PATH = os.path.join(_DATA_DIR, "poc_cross_alerts.json")

st.set_page_config(page_title="AlgoTrade Unified", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background-color: #0f172a; color: #e2e8f0; }
    .hero-card { background: #1e293b; border-radius: 12px; padding: 1.2rem;
                 border: 1px solid #334155; }
    .hero-price { font-size: 1.8rem; font-weight: 700; color: #f1f5f9; }
    .hero-change-up { color: #22c55e; font-weight: 600; }
    .hero-change-down { color: #ef4444; font-weight: 600; }
    .low-confidence { color: #f59e0b; font-size: 0.85rem; font-weight: 500; }
    div.stButton > button {
        background: linear-gradient(to right, #10b981, #14b8a6);
        color: white; border: none; border-radius: 999px; padding: 0.5rem 1.5rem; font-weight: 600;
    }
    div.stButton > button:hover { opacity: 0.9; color: white; }

    /* Streamlit's own components default to a low-contrast gray that
       nearly disappears on this dark background — override via their
       documented data-testid hooks (stable across versions, unlike the
       auto-generated emotion-cache class names). */
    [data-testid="stCaptionContainer"] { color: #cbd5e1 !important; }
    [data-testid="stMetricLabel"] { color: #94a3b8 !important; font-weight: 500; }
    [data-testid="stMetricValue"] { color: #f1f5f9 !important; }
    [data-testid="stMarkdownContainer"] p { color: #e2e8f0; }
    [data-testid="stTabs"] button { color: #94a3b8 !important; font-weight: 500; }
    [data-testid="stTabs"] button[aria-selected="true"] { color: #10b981 !important; }
    label, .stTextInput label { color: #cbd5e1 !important; }
    .stTextInput input { color: #f1f5f9 !important; background-color: #1e293b !important;
                          border: 1px solid #334155 !important; }
    [data-testid="stDataFrame"] { background-color: #1e293b; }

    /* Flash-on-change: briefly tints a hero card when its price changes
       from the last value seen this session, then fades back out. */
    .flash-up { animation: flashUp 0.4s ease-out; }
    .flash-down { animation: flashDown 0.4s ease-out; }
    @keyframes flashUp { 0% { background-color: rgba(34,197,94,0.35); } 100% { background-color: transparent; } }
    @keyframes flashDown { 0% { background-color: rgba(239,68,68,0.35); } 100% { background-color: transparent; } }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_signal_store():
    return SignalStore(SIGNAL_STORE_PATH)


@st.cache_resource
def get_trade_log():
    return PaperTradeLog(TRADE_LOG_PATH)


@st.cache_resource
def get_poc_alert_store():
    # Separate store from get_signal_store() (fade-scanner dedup) — keyed
    # by (symbol, candle_date) so the same VWAP/POC cross doesn't
    # re-trigger a Telegram message on every scan/auto-refresh tick, only
    # once per genuinely new crossing candle.
    return SignalStore(POC_ALERT_STORE_PATH)


def _sparkline_svg(values, width=100, height=26, color="#94a3b8"):
    """Minimal sparkline: single desaturated line, no gridlines/axes —
    used inside hero cards instead of a full mini chart."""
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - ((v - lo) / rng) * height:.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="margin-top:4px;display:block;">'
        f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def render_hero_card(col, symbol):
    with col:
        try:
            key = resolve_futures_key(symbol)
            daily = get_daily_candles(key, lookback_days=10)
            if len(daily) < 2:
                st.markdown(f'<div class="hero-card">{symbol}: not enough data</div>', unsafe_allow_html=True)
                return
            last, prev = daily[-1], daily[-2]
            change_pct = (last["close"] - prev["close"]) / prev["close"] * 100
            css_class = "hero-change-up" if change_pct >= 0 else "hero-change-down"
            arrow = "▲" if change_pct >= 0 else "▼"

            # Flash the card briefly when the price changes from the last
            # value seen in this session (persists across Streamlit reruns
            # via session_state; a fresh session simply shows no flash).
            prev_seen = st.session_state.get(f"hero_last_price_{symbol}")
            flash_class = ""
            if prev_seen is not None and prev_seen != last["close"]:
                flash_class = "flash-up" if last["close"] >= prev_seen else "flash-down"
            st.session_state[f"hero_last_price_{symbol}"] = last["close"]

            sparkline = _sparkline_svg([c["close"] for c in daily])

            st.markdown(
                f"""<div class="hero-card {flash_class}">
                    <div style="color:#94a3b8;font-size:0.9rem;">{symbol}</div>
                    <div class="hero-price">{last['close']:.2f}</div>
                    <div class="{css_class}">{arrow} {abs(change_pct):.2f}%</div>
                    {sparkline}
                </div>""",
                unsafe_allow_html=True,
            )
        except Exception as exc:
            st.markdown(f'<div class="hero-card">{symbol}: error — {exc}</div>', unsafe_allow_html=True)


# --- Header / hero cards --------------------------------------------------------
st.markdown('<div style="font-size:1.6rem;font-weight:700;color:#f1f5f9;">AlgoTrade Unified</div>', unsafe_allow_html=True)
st.caption(f"Last refreshed: {datetime.now(tz=IST).strftime('%I:%M:%S %p')} IST")

hero_cols = st.columns(len(CORE_INDEX_SYMBOLS))
for col, symbol in zip(hero_cols, CORE_INDEX_SYMBOLS):
    render_hero_card(col, symbol)

st.divider()

tab_breakout, tab_fade, tab_trades, tab_levels, tab_scanner = st.tabs(
    ["Breakout Scanner", "Morning Fade Scanner", "Paper Trades", "Levels & Volume Profile", "Scanner Grid"]
)

# --- Breakout Scanner tab -------------------------------------------------------
with tab_breakout:
    st.subheader("Breakout Scanner")
    if st.button("Scan for breakouts", key="scan_breakout"):
        tickers = get_fno_tickers()
        results, errors = [], []
        with st.spinner(f"Scanning {len(tickers)} tickers..."):
            with ThreadPoolExecutor(max_workers=3) as executor:  # Cloudflare-safe concurrency
                futures = {executor.submit(breakout_screen, t): t for t in tickers}
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as exc:
                        errors.append(f"{symbol}: {exc}")

        if results:
            df = pd.DataFrame([r.__dict__ for r in results]).sort_values("relative_volume", ascending=False)
            st.dataframe(df, width='stretch', hide_index=True)
        else:
            st.info("No breakout signals found.")
        if errors:
            with st.expander(f"{len(errors)} ticker(s) had errors"):
                for e in errors:
                    st.text(e)

# --- Morning Fade Scanner tab ----------------------------------------------------
with tab_fade:
    st.subheader("Morning Fade Scanner")
    st.caption(
        "Short candidates: extended move from open + high RVOL, within the 09:30-10:30 IST window. "
        "RVOL_MIN threshold is an unvalidated default — see strategies/morning_fade.py."
    )
    if st.button("Scan for fade candidates", key="scan_fade"):
        tickers = get_fno_tickers()
        signal_store = get_signal_store()
        found = []
        with st.spinner(f"Scanning {len(tickers)} tickers..."):
            for symbol in tickers:
                try:
                    key = resolve_equity_key(symbol)
                    candles = get_intraday_candles(key, unit="minutes", interval=5)
                    if not candles:
                        continue
                    open_price = candles[0]["open"]
                    signal = evaluate_fade_candidate(symbol, candles, open_price=open_price)
                    if signal and not signal_store.is_duplicate(signal.ticker, signal.signal_time):
                        found.append(signal)
                except Exception:
                    continue  # a single ticker's failure shouldn't block the scan

        if found:
            df = pd.DataFrame([s.__dict__ for s in found])
            st.dataframe(df, width='stretch', hide_index=True)
            for signal in found:
                col1, col2 = st.columns([3, 1])
                col1.write(f"{signal.ticker} @ {signal.entry_price:.2f} (stop {signal.stop_price:.2f})")
                if col2.button("Paper trade this", key=f"trade_{signal.ticker}_{signal.signal_time}"):
                    signal_store.record(signal.ticker, signal.signal_time)
                    log = get_trade_log()
                    log.open_trade(
                        signal.ticker, "short", signal.entry_price, signal.stop_price,
                        target_price=None, entry_time=signal.signal_time,
                    )
                    st.success(f"Opened paper trade for {signal.ticker}")
        else:
            st.info("No new fade candidates found (or all already recorded).")

# --- Paper Trades tab -------------------------------------------------------------
with tab_trades:
    st.subheader("Paper Trades")
    log = get_trade_log()

    if st.button("Refresh / check exits", key="check_exits"):
        open_trades = log.open_trades()
        if open_trades:
            prices = {}
            for t in open_trades:
                try:
                    key = resolve_equity_key(t.ticker)
                    candles = get_intraday_candles(key, unit="minutes", interval=5)
                    if candles:
                        prices[t.ticker] = candles[-1]["close"]
                except Exception:
                    continue
            log.check_exits(prices, datetime.now(tz=IST))
            st.success("Checked exits for all open trades.")

    if log.trades:
        df = pd.DataFrame([t.__dict__ for t in log.trades])
        st.dataframe(df, width='stretch', hide_index=True)
        summary = log.summary()
        if summary["total_trades"] > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric("Closed trades", summary["total_trades"])
            c2.metric("Win rate", f"{summary['win_rate']*100:.1f}%")
            c3.metric("Avg P&L", f"{summary['avg_pnl_pct']:.2f}%")
    else:
        st.info("No paper trades yet — open one from the Morning Fade Scanner tab.")

# --- Levels & Volume Profile tab ---------------------------------------------------
RVOL_LOOKBACK = 10
MIN_REPLAY_BARS = 6  # floor to avoid degenerate (too-flat/too-few) volume profile inputs

with tab_levels:
    st.subheader("Levels & Volume Profile")
    ticker_input = st.text_input("Ticker (e.g. RELIANCE, or NIFTY / BANKNIFTY for index futures)", value="RELIANCE")

    if st.button("Load levels", key="load_levels"):
        try:
            ticker_upper = ticker_input.upper()
            if ticker_upper in CORE_INDEX_SYMBOLS:
                key = resolve_futures_key(ticker_upper)
            else:
                key = resolve_equity_key(ticker_upper)
            full_candles = get_intraday_candles(key, unit="minutes", interval=5)

            st.session_state.levels_ticker = ticker_upper
            st.session_state.levels_full_candles = full_candles
            # New ticker => forget any previous day selection / replay position
            # so we start fresh at the latest trading day, fully revealed.
            st.session_state.pop("levels_selected_day", None)
            st.session_state.pop("levels_replay_idx", None)
        except Exception as exc:
            st.error(f"Could not load levels for {ticker_input}: {exc}")
            st.session_state.levels_full_candles = None

    if st.session_state.get("levels_full_candles"):
        full_candles = st.session_state.levels_full_candles

        # Group candles into trading days so replay can step through any
        # fetched day, not just today's session.
        day_bounds = {}
        day_order = []
        for i, c in enumerate(full_candles):
            d = c["date"][:10]
            if d not in day_bounds:
                day_bounds[d] = [i, i + 1]
                day_order.append(d)
            else:
                day_bounds[d][1] = i + 1
        day_options = list(reversed(day_order))  # most recent first

        dcol1, dcol2 = st.columns([1, 4])
        with dcol1:
            selected_day = st.selectbox("Session day", day_options, key="levels_day_select")

        if st.session_state.get("levels_selected_day") != selected_day:
            st.session_state.levels_selected_day = selected_day
            day_start, day_end = day_bounds[selected_day]
            # Default replay position for a freshly selected day: fully revealed.
            st.session_state.levels_replay_idx = day_end - day_start

        day_start, day_end = day_bounds[selected_day]
        session_all = full_candles[day_start:day_end]
        max_idx = len(session_all)
        min_idx = min(MIN_REPLAY_BARS, max_idx)
        st.session_state.setdefault("levels_replay_idx", max_idx)
        st.session_state.levels_replay_idx = max(min_idx, min(st.session_state.levels_replay_idx, max_idx))

        st.markdown("**Replay**")
        rc1, rc2, rc3, rc4 = st.columns([1, 1, 1, 5])
        if rc1.button("⏮ Start", key="replay_start"):
            st.session_state.levels_replay_idx = min_idx
        if rc2.button("◀ Back", key="replay_back"):
            st.session_state.levels_replay_idx = max(min_idx, st.session_state.levels_replay_idx - 1)
        if rc3.button("Fwd ▶", key="replay_fwd"):
            st.session_state.levels_replay_idx = min(max_idx, st.session_state.levels_replay_idx + 1)
        with rc4:
            st.session_state.levels_replay_idx = st.slider(
                "Bar", min_value=min_idx, max_value=max_idx,
                value=st.session_state.levels_replay_idx, key="levels_replay_slider",
                label_visibility="collapsed",
            )
        replay_idx = st.session_state.levels_replay_idx
        is_replaying = replay_idx < max_idx
        if is_replaying:
            st.caption(f"Replaying bar {replay_idx} of {max_idx} — {session_all[replay_idx - 1]['date']}")

        session_candles = session_all[:replay_idx]
        # RVOL's baseline lookback can reach back before the selected day's
        # start (into earlier fetched days), so it uses the full fetch up to
        # the current replay point, not just this day's session_candles.
        candles_for_rvol = full_candles[: day_start + replay_idx]

        try:
            ticker_upper = st.session_state.levels_ticker
            vwap_points = session_vwap(session_candles)
            poc_proxy = vwap_poc_proxy(session_candles)
            profile = build_volume_profile(session_candles, num_bins=20)
            va = compute_value_area(session_candles, num_bins=20)
            rvol = compute_rvol(candles_for_rvol, lookback=RVOL_LOOKBACK)
            rvol_series = compute_rvol_series(candles_for_rvol, lookback=RVOL_LOOKBACK)
            # Align the RVOL series to this day's candles only (drop any
            # lookback-warmup entries that fall before this day's start).
            rvol_series = rvol_series[-len(session_candles):] if rvol_series else []
            cvd_series = compute_candle_cvd(session_candles)  # resets each day, by design

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("VWAP / POC proxy", f"{poc_proxy:.2f}")
            c2.metric("RVOL", f"{rvol.rvol:.2f}x")
            c3.metric("Volume-profile POC", f"{profile.poc_bin.mid:.2f}")
            c4.metric("VAH / VAL", f"{va.vah:.2f} / {va.val:.2f}")
            st.markdown(
                f'<span class="low-confidence">⚠ Value area confidence: {va.confidence} — '
                f'validated finding: VAH/VAL edges unreliable (62-71% overlap). '
                f'VWAP/POC proxy is the more trustworthy level.</span>',
    
