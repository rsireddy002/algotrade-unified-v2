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
from signals.atr import compute_atr
from signals.breakout import screen_ticker as breakout_screen
from signals.cvd import compute_candle_cvd
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
            st.dataframe(df, use_container_width=True, hide_index=True)
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
            st.dataframe(df, use_container_width=True, hide_index=True)
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
        st.dataframe(df, use_container_width=True, hide_index=True)
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
                unsafe_allow_html=True,
            )
            st.caption(
                "CVD shown here is a candle-level approximation (each candle's close vs. "
                "the prior close, whole-candle volume assigned to that side) — not true "
                "tick-rule CVD, which needs the live feed. See signals/cvd.py."
            )

            # Candlestick + volume profile (top), Volume/RVOL (middle), CVD (bottom) —
            # all sharing the same x-axis (time) so replay steps stay aligned across rows.
            from plotly.subplots import make_subplots

            # Category-type x-axis (below) needs plain labels, not raw ISO
            # timestamps — HH:MM is enough since a session is always one day.
            labels = [c["date"][11:16] for c in session_candles]
            opens = [c["open"] for c in session_candles]
            highs = [c["high"] for c in session_candles]
            lows = [c["low"] for c in session_candles]
            closes = [c["close"] for c in session_candles]
            volumes = [c["volume"] for c in session_candles]

            fig = make_subplots(
                rows=3, cols=2, shared_xaxes=True, shared_yaxes=True,
                row_heights=[0.55, 0.2, 0.25],
                column_widths=[0.78, 0.22], horizontal_spacing=0.01, vertical_spacing=0.03,
                specs=[
                    [{"type": "candlestick"}, {"type": "bar"}],
                    [{"type": "bar"}, None],
                    [{"type": "bar"}, None],
                ],
            )

            fig.add_trace(
                go.Candlestick(
                    x=labels, open=opens, high=highs, low=lows, close=closes,
                    increasing_line_color="#22c55e", increasing_fillcolor="#22c55e",
                    decreasing_line_color="#ef4444", decreasing_fillcolor="#ef4444",
                    name=ticker_upper, showlegend=False,
                ),
                row=1, col=1,
            )

            for level_price, label, color in [
                (poc_proxy, "VWAP/POC proxy", "#10b981"),
                (va.vah, "VAH", "#f59e0b"),
                (va.val, "VAL", "#f59e0b"),
            ]:
                fig.add_hline(
                    y=level_price, line_color=color, line_dash="dot" if label != "VWAP/POC proxy" else "solid",
                    annotation_text=label, annotation_position="right",
                    row=1, col=1,
                )

            fig.add_trace(
                go.Bar(
                    x=[b.volume for b in profile.bins],
                    y=[b.mid for b in profile.bins],
                    orientation="h",
                    marker_color="#475569",
                    showlegend=False,
                ),
                row=1, col=2,
            )

            # Volume bars colored by RVOL — bars above 1x baseline (busier than
            # average) stand out in amber, the rest in slate. Early bars in a
            # short session may lack a full RVOL lookback; those fall back to slate.
            colored = ["#f59e0b" if r.rvol >= 1.0 else "#475569" for r in rvol_series]
            n_missing = len(volumes) - len(colored)
            rvol_colors = (["#475569"] * n_missing + colored) if n_missing > 0 else colored
            fig.add_trace(
                go.Bar(x=labels, y=volumes, marker_color=rvol_colors, showlegend=False, name="Volume (RVOL-colored)"),
                row=2, col=1,
            )

            # CVD as bars (height = cumulative running total, not per-candle
            # delta) — colored green where the running total rose from the
            # previous bar, red where it fell.
            cvd_colors = [
                "#22c55e" if (i == 0 or cvd_series[i] >= cvd_series[i - 1]) else "#ef4444"
                for i in range(len(cvd_series))
            ]
            fig.add_trace(
                go.Bar(x=labels, y=cvd_series, marker_color=cvd_colors, showlegend=False, name="CVD (candle approx.)"),
                row=3, col=1,
            )
            fig.add_hline(y=0, line_color="#64748b", line_width=1, row=3, col=1)

            fig.update_layout(
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b", font_color="#cbd5e1",
                height=750, margin=dict(l=20, r=20, t=20, b=20),
                bargap=0.05,  # bars fill nearly all their allotted width
            )
            # type="category" (not the default date/time axis) removes the
            # off-hours gaps that were squeezing candles into the left portion
            # of the plot and stretches every bar to fill its slot evenly.
            fig.update_xaxes(type="category", rangeslider_visible=False, gridcolor="#334155", row=1, col=1)
            fig.update_xaxes(title_text="Volume", gridcolor="#334155", row=1, col=2)
            fig.update_xaxes(type="category", gridcolor="#334155", row=2, col=1)
            fig.update_xaxes(type="category", gridcolor="#334155", row=3, col=1)
            fig.update_yaxes(title_text="Price", gridcolor="#334155", row=1, col=1)
            fig.update_yaxes(showticklabels=False, row=1, col=2)
            fig.update_yaxes(title_text="Vol", gridcolor="#334155", row=2, col=1)
            fig.update_yaxes(title_text="CVD", gridcolor="#334155", row=3, col=1)

            st.plotly_chart(fig, use_container_width=True)

        except Exception as exc:
            st.error(f"Could not compute levels at this replay position: {exc}")

# --- Scanner Grid tab -------------------------------------------------------------
# Multi-symbol card grid, modeled on the dryarapureddy-tick-ML scanner-app's
# layout (compact chart + level lines + a small delta panel + SL/Target +
# Buy/Sell, several cards per row) — but built entirely from signals already
# in this platform (RVOL, VWAP/POC proxy, value area, candle-approx CVD, ATR).
# Deliberately NOT the ML-zone-break-risk version from that app (composite
# 18-day zones, cross-timeframe validation, zone_break_model.pkl) — that's
# a separate, larger port; see conversation history if picking that up later.
GRID_ATR_PERIOD = 14
GRID_COLS_PER_ROW = 2


def _latest_day_candles(candles):
    """Slice a multi-day intraday candle list down to just the most recent
    trading day — get_intraday_candles fetches ~10 calendar days by
    default, but a grid card should chart one session, not all of them
    overlaid (see conversation: HH:MM-only labels on a category x-axis
    collapse every day's candles onto the same ~75 slots otherwise)."""
    if not candles:
        return candles
    latest_day = candles[-1]["date"][:10]
    start = next(i for i, c in enumerate(candles) if c["date"][:10] == latest_day)
    return candles[start:]


def _scan_one_for_rvol(symbol):
    """Used by the universe scan below: resolve + fetch once, return
    everything the grid needs so the per-card render step doesn't have to
    re-fetch the same candles a second time.

    Tries the live feed first — if scripts/run_live_scanner_feed.py has
    accumulated enough of today's bars, this needs zero REST calls. But
    the live aggregator only ever holds TODAY's candles (it starts empty
    each process launch), while RVOL's lookback baseline needs bars from
    BEFORE now — so live-only RVOL only becomes usable roughly
    RVOL_LOOKBACK * 5 minutes into the session. Before that (or if the
    live feed isn't running/stale/hasn't reached this symbol yet), this
    falls back to the REST multi-day fetch, unchanged from before.
    Returns (symbol, key, candles, rvol, source) — 'source' is "live" or
    "rest", surfaced in the UI so it's never a silent guess which one ran."""
    key = resolve_equity_key(symbol)

    try:
        live_candles = get_live_candles(symbol)
        if len(live_candles) >= RVOL_LOOKBACK + 1:
            rvol = compute_rvol(live_candles, lookback=RVOL_LOOKBACK)
            return symbol, key, live_candles, rvol.rvol, "live"
    except LiveFeedUnavailable:
        pass  # fall through to REST below

    candles = get_intraday_candles(key, unit="minutes", interval=5)
    if len(candles) < RVOL_LOOKBACK + 1:
        return None
    rvol = compute_rvol(candles, lookback=RVOL_LOOKBACK)
    return symbol, key, candles, rvol.rvol, "rest"


def render_grid_card(col, symbol, key, full_candles, rvol_pct, log, source="rest"):
    with col:
        try:
            candles = _latest_day_candles(full_candles)  # today only — see _latest_day_candles
            profile = build_volume_profile(candles, num_bins=20)
            va = compute_value_area(candles, num_bins=20)
            poc_proxy = vwap_poc_proxy(candles)
            cvd_series = compute_candle_cvd(candles)
            last_close = candles[-1]["close"]

            labels = [c["date"][11:16] for c in candles]
            opens = [c["open"] for c in candles]
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            closes = [c["close"] for c in candles]

            fig = go.Figure(
                data=[
                    go.Candlestick(
                        x=labels, open=opens, high=highs, low=lows, close=closes,
                        increasing_line_color="#22c55e", increasing_fillcolor="#22c55e",
                        decreasing_line_color="#ef4444", decreasing_fillcolor="#ef4444",
                        showlegend=False,
                    )
                ]
            )
            for level_price, dash in [(poc_proxy, "solid"), (va.vah, "dot"), (va.val, "dot")]:
                fig.add_hline(y=level_price, line_color="#f59e0b" if dash == "dot" else "#10b981", line_dash=dash)
            fig.update_layout(
                title=dict(
                    text=f"{symbol} (RVOL {rvol_pct:.0f}%) · {'🟢 live' if source == 'live' else 'REST'}",
                    font=dict(color="#f1f5f9", size=14),  # explicit — title text doesn't inherit font_color
                ),
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b", font_color="#cbd5e1",
                height=260, margin=dict(l=10, r=10, t=30, b=10),
                xaxis=dict(type="category", rangeslider_visible=False, gridcolor="#334155"),
                yaxis=dict(gridcolor="#334155"),
                bargap=0.05,
            )
            st.plotly_chart(fig, use_container_width=True, key=f"grid_chart_{symbol}")

            cvd_colors = [
                "#22c55e" if (i == 0 or cvd_series[i] >= cvd_series[i - 1]) else "#ef4444"
                for i in range(len(cvd_series))
            ]
            cvd_fig = go.Figure(data=[go.Bar(x=labels, y=cvd_series, marker_color=cvd_colors, showlegend=False)])
            cvd_fig.update_layout(
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b", font_color="#cbd5e1",
                height=80, margin=dict(l=10, r=10, t=5, b=5),
                xaxis=dict(type="category", showticklabels=False, gridcolor="#334155"),
                yaxis=dict(gridcolor="#334155"),
                bargap=0.05,
            )
            st.plotly_chart(cvd_fig, use_container_width=True, key=f"grid_cvd_{symbol}")

            already_open = any(t.ticker == symbol and t.is_open for t in log.trades)
            daily = get_daily_candles(key, lookback_days=60)
            atr_val = compute_atr(daily, period=GRID_ATR_PERIOD).atr if len(daily) >= GRID_ATR_PERIOD + 1 else None

            bcol, scol = st.columns(2)
            with bcol:
                # Long candidate: VAL as the natural stop (mean-reversion up
                # off the low of the value area), ATR-multiple target.
                if last_close > va.val and atr_val:
                    target = atr_based_target(last_close, atr_val, "long")
                    st.caption(f"SL {va.val:.1f} / T {target:.1f}")
                    if st.button("Buy", key=f"grid_buy_{symbol}", disabled=already_open):
                        log.open_trade(symbol, "long", last_close, va.val, target, candles[-1]["date"])
                        st.success(f"LONG opened: {symbol}")
                else:
                    st.caption("No valid long setup")
            with scol:
                # Short candidate: VAH as the natural stop, ATR-multiple target.
                if last_close < va.vah and atr_val:
                    target = atr_based_target(last_close, atr_val, "short")
                    st.caption(f"SL {va.vah:.1f} / T {target:.1f}")
                    if st.button("Sell", key=f"grid_sell_{symbol}", disabled=already_open):
                        log.open_trade(symbol, "short", last_close, va.vah, target, candles[-1]["date"])
                        st.success(f"SHORT opened: {symbol}")
                else:
                    st.caption("No valid short setup")
            if already_open:
                st.caption("⚠ Already have an open paper trade in this symbol.")

        except Exception as exc:
            st.write(f"{symbol}: could not render — {exc}")


def _run_universe_scan():
    tickers = get_fno_tickers()
    scanned = []
    with st.spinner(f"Scanning {len(tickers)} tickers for RVOL..."):
        with ThreadPoolExecutor(max_workers=3) as executor:  # Cloudflare-safe concurrency
            futures = {executor.submit(_scan_one_for_rvol, t): t for t in tickers}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        scanned.append(result)
                except Exception:
                    continue  # a single ticker's failure shouldn't block the scan
    scanned.sort(key=lambda r: r[3], reverse=True)  # rank by rvol, descending
    st.session_state.scanner_grid_results = scanned
    st.session_state.scanner_grid_last_scan = datetime.now(tz=IST)


with tab_scanner:
    st.subheader("Scanner Grid")
    st.caption(
        "Ranks the full F&O universe by RVOL, then shows the top N as chart cards — "
        "same layout idea as the dryarapureddy-tick-ML scanner-app, but using this "
        "platform's own signals (VWAP/POC proxy, value area, candle-approx CVD, ATR) "
        "rather than that app's ML zone-break-risk model."
    )

    is_live, live_age, live_symbol_count = live_feed_status()
    if is_live:
        st.success(
            f"🟢 Live feed active — {live_symbol_count} symbols, snapshot {live_age:.0f}s old. "
            "Cards for symbols with enough of today's bars use this (zero REST calls); "
            "others fall back to REST until enough live history accumulates."
        )
    else:
        st.info(
            "Live feed not detected (scripts/run_live_scanner_feed.py not running, or "
            "data_cache/live_candles.json is stale) — every card will use REST polling."
        )

    top_n = st.number_input("Show top N by RVOL", min_value=4, max_value=40, value=12, step=2)

    autocol1, autocol2 = st.columns([1, 2])
    with autocol1:
        auto_refresh_on = st.checkbox("Auto-refresh", key="scanner_grid_autorefresh_enabled")
    with autocol2:
        if auto_refresh_on:
            refresh_secs = st.number_input(
                "Every N seconds", min_value=15, max_value=600, value=60, step=15,
                key="scanner_grid_refresh_secs",
            )
            st.caption(
                "⚠ Reruns the WHOLE app on this timer, not just this tab — "
                "Streamlit re-executes top to bottom regardless of which tab is visually "
                "active, so in-progress state elsewhere (e.g. a Levels & Volume Profile "
                "replay position) will keep re-rendering at its current position on every "
                "tick rather than being disturbed, but any *unsaved* selection you're "
                "mid-change on could get reset. Turn it off while actively using another tab."
            )

    if auto_refresh_on:
        from streamlit_autorefresh import st_autorefresh
        tick_count = st_autorefresh(interval=refresh_secs * 1000, key="scanner_grid_autorefresh_timer")
    else:
        tick_count = None

    manual_click = st.button("Scan universe", key="scan_grid")

    # st_autorefresh's tick_count only increments on its OWN timer — but
    # Streamlit reruns this whole script on ANY widget interaction anywhere
    # in the app (e.g. moving the Levels tab's replay slider). Comparing
    # against the last-seen tick avoids re-scanning the full 200-stock
    # universe on every unrelated rerun — only a genuine timer tick (or the
    # manual button, or turning auto-refresh on for the first time with no
    # results yet) triggers a rescan.
    should_scan = manual_click
    if auto_refresh_on:
        prev_tick = st.session_state.get("scanner_grid_prev_tick")
        if prev_tick != tick_count:
            st.session_state.scanner_grid_prev_tick = tick_count
            should_scan = True
        elif "scanner_grid_results" not in st.session_state:
            should_scan = True

    if should_scan:
        _run_universe_scan()

    last_scan = st.session_state.get("scanner_grid_last_scan")
    if last_scan:
        st.caption(f"Last scanned: {last_scan.strftime('%I:%M:%S %p')} IST")

    results = st.session_state.get("scanner_grid_results")
    if results:
        top_results = results[:top_n]
        log = get_trade_log()
        for i in range(0, len(top_results), GRID_COLS_PER_ROW):
            row = top_results[i:i + GRID_COLS_PER_ROW]
            cols = st.columns(len(row))
            for col, (symbol, key, candles, rvol_pct, source) in zip(cols, row):
                render_grid_card(col, symbol, key, candles, rvol_pct, log, source=source)
    else:
        st.info("Click \"Scan universe\" to rank the F&O universe by RVOL and load the grid.")

st.markdown(
    '<div style="text-align:center;color:#64748b;font-size:0.8rem;margin-top:2rem;'
    'padding-top:1rem;border-top:1px solid #334155;">'
    'For educational and research purposes only. Not financial advice.</div>',
    unsafe_allow_html=True,
)
