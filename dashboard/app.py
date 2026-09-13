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

NOT wired to the live WebSocket feed (core/feed_listener.py) — that needs
a background thread feeding a shared state Streamlit can poll, which is
its own reliability problem to get right and wasn't something I wanted to
ship half-tested. Everything here runs on polled historical/intraday
candles, the same proven pattern as breakout-scanner-streamlit. Live-tick
integration (CVD, real-time hero cards) is a clearly separate future step.

Run:
    streamlit run dashboard/app.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candle_store import get_daily_candles, get_intraday_candles
from core.config import CORE_INDEX_SYMBOLS, IST
from core.instruments import get_fno_tickers, resolve_equity_key, resolve_futures_key
from execution.paper_trader import PaperTradeLog, atr_based_target
from execution.signal_store import SignalStore
from signals.atr import compute_atr
from signals.breakout import screen_ticker as breakout_screen
from signals.rvol import compute_rvol
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
            st.markdown(
                f"""<div class="hero-card">
                    <div style="color:#94a3b8;font-size:0.9rem;">{symbol}</div>
                    <div class="hero-price">{last['close']:.2f}</div>
                    <div class="{css_class}">{arrow} {abs(change_pct):.2f}%</div>
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

tab_breakout, tab_fade, tab_trades, tab_levels = st.tabs(
    ["Breakout Scanner", "Morning Fade Scanner", "Paper Trades", "Levels & Volume Profile"]
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
            candles = get_intraday_candles(key, unit="minutes", interval=5)
            session_candles = candles[-75:] if len(candles) >= 75 else candles

            vwap_points = session_vwap(session_candles)
            poc_proxy = vwap_poc_proxy(session_candles)
            profile = build_volume_profile(session_candles, num_bins=20)
            va = compute_value_area(session_candles, num_bins=20)
            rvol = compute_rvol(candles, lookback=10)

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

            # Candlestick (left, wide) + volume profile (right, narrow),
            # sharing the same price (y) axis — the standard "levels" view.
            from plotly.subplots import make_subplots

            dates = [c["date"] for c in session_candles]
            opens = [c["open"] for c in session_candles]
            highs = [c["high"] for c in session_candles]
            lows = [c["low"] for c in session_candles]
            closes = [c["close"] for c in session_candles]

            fig = make_subplots(
                rows=1, cols=2, shared_yaxes=True,
                column_widths=[0.78, 0.22], horizontal_spacing=0.01,
            )

            fig.add_trace(
                go.Candlestick(
                    x=dates, open=opens, high=highs, low=lows, close=closes,
                    increasing_line_color="#22c55e", increasing_fillcolor="#22c55e",
                    decreasing_line_color="#ef4444", decreasing_fillcolor="#ef4444",
                    name=ticker_input.upper(), showlegend=False,
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

            fig.update_layout(
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b", font_color="#cbd5e1",
                height=550, margin=dict(l=20, r=20, t=20, b=20),
            )
            fig.update_xaxes(rangeslider_visible=False, gridcolor="#334155", row=1, col=1)
            fig.update_xaxes(title_text="Volume", gridcolor="#334155", row=1, col=2)
            fig.update_yaxes(title_text="Price", gridcolor="#334155", row=1, col=1)
            fig.update_yaxes(showticklabels=False, row=1, col=2)

            st.plotly_chart(fig, use_container_width=True)

        except Exception as exc:
            st.error(f"Could not load levels for {ticker_input}: {exc}")

st.markdown(
    '<div style="text-align:center;color:#64748b;font-size:0.8rem;margin-top:2rem;'
    'padding-top:1rem;border-top:1px solid #334155;">'
    'For educational and research purposes only. Not financial advice.</div>',
    unsafe_allow_html=True,
)
