# algotrade-unified

Consolidating the F&O trading toolset (breakout scanner, RVOL, VWAP/POC,
value area, volume profile, CVD/footprint, morning fade, ML zone
prediction, LLM gatekeeper) into one platform, built in verified stages.

## Phase 1 — core/ (auth, instruments, candles)
**Verified live** against your real Upstox account.

## Phase 2 — core/feed_listener.py (WebSocket)
**Verified live** — real decoded ticks received for NIFTY/BANKNIFTY
futures, correct int64 casting confirmed against live data.

## Phase 3 — signals/ (RVOL, VWAP/POC, volume profile, value area, breakout, CVD)
**Verified live** for the historical-data signals (RVOL, VWAP/POC, volume
profile, value area, breakout) — including VWAP-POC and volume-profile-POC
landing within ~0.08% of each other on real RELIANCE data, independently
reproducing your own <0.1% validated finding. CVD is unit-tested but not
yet run against a live tick stream.

## Phase 4 — strategies/ + execution/ (morning fade, paper trader, LLM gatekeeper)

- `strategies/morning_fade.py` — **NEW implementation, not a port** of your
  original `backtest_fade.py` (that file's exact logic wasn't available to
  reuse). Built from the validated parameters: `min_move=0.3%`,
  `stop=1.0%`, `window=09:30-10:30 IST` → your backtest found +0.21R/trade,
  65% win rate. `RVOL_MIN=1.5` is a new default I introduced, NOT part of
  the validated parameter set — treat it as unvalidated until backtested.
  **Unit-tested**: correctly qualifies/rejects on window, move threshold,
  and RVOL.
- `signals/atr.py` — Wilder's ATR on DAILY bars (validated fix — 5-min ATR
  understated real volatility). **Unit-tested** against hand-calculated
  values, including catching and fixing a smoothing-window bug during
  testing before it shipped.
- `execution/signal_store.py` — persistent JSON signal log with dedup by
  (ticker, signal_time), atomic writes. Directly addresses the signal
  deduplication issues noted in fno-scanner-strategy-update. **Unit-tested**:
  dedup, distinct-signal handling, and persistence across process restarts
  all confirmed.
- `execution/paper_trader.py` — stop/target/EOD-square-off exit logic (EOD
  square-off at 15:30 IST is a validated fix, kept unchanged). Exit/risk
  rules are intentionally simple — your own notes flagged this as "the
  weakest system component" of the original, so this doesn't invent a more
  sophisticated unvalidated scheme. **Unit-tested**: stop-hit, target-hit,
  EOD square-off (including the exact 15:29 vs 15:30 boundary), and P&L
  math all confirmed.
- `execution/llm_gatekeeper.py` — Claude API sanity check (structural
  consistency only — not a trading-decision AI) before a signal reaches
  the paper trader. **Verified live** against a real Claude API call —
  and that live test caught a real bug the mocked tests had missed:
  Claude can return a ThinkingBlock as the first content item, not a text
  block, and the original code blindly assumed `content[0]` was text.
  Fixed to search for the actual text block explicitly instead, with a
  regression test added that reproduces the exact failure. Corrected
  model string too — an initial draft used the stale `claude-sonnet-4-5`;
  fixed to the current `claude-sonnet-5` before this ever reached you.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# paste your Analytics Token, Telegram credentials, and (optionally)
# ANTHROPIC_API_KEY into .env
```

## Phase 5 — dashboard/app.py (unified UI)

Ties together every phase into one Streamlit dashboard: breakout scanner,
morning fade scanner, paper trades, and a levels/volume-profile viewer for
any ticker — same AlgoTrade Pro dark theme as breakout-scanner-streamlit.

**NOT wired to the live WebSocket feed** — that needs a background thread
feeding shared state Streamlit can poll safely, which is its own
reliability problem; I didn't want to ship that half-tested. Runs on
polled candles throughout, same proven pattern as breakout-scanner-streamlit.
Live-tick integration (CVD, real-time hero cards) is a clearly separate
future step, not attempted here.

**Verified**: every function it imports from core/signals/strategies/
execution actually exists with the right name (16 imports checked
directly, not assumed) — this is exactly the class of bug (wrong
attribute name, renamed function) that's caused real failures earlier in
this build. Also **actually started the server** in this sandbox
(`streamlit run`, headless, real HTTP requests against it) rather than
just syntax-checking: it returned HTTP 200 and rendered ~7.5KB of real
page content with zero unhandled exceptions — including with a
deliberately invalid Upstox token, confirming the try/except error
handling in the hero cards works rather than crashing the whole page.
**Not tested**: the actual scan/trade button click-through flows (those
need a real browser interacting with a running session) — the structural
integrity and error handling are verified, the interactive behavior isn't.

## Phase 6 — deploy/ + DEPLOY.md (deployment)

Systemd services for the dashboard and feed listener, plus a full
deployment runbook — mirrors the exact pattern already proven in
production for breakout-scanner-streamlit (same key pair reuse, same
service structure).

**What's verified**: both `.service` files pass `systemd-analyze verify`
with zero syntax/structural errors — genuine validation by systemd's own
parser, not just visual inspection. The only complaint is that binary
paths don't exist yet in this sandbox, which is expected (they're created
by the runbook's own setup steps on the real server).

**Not tested**: an actual EC2 deployment — no AWS access from this
sandbox. The commands in DEPLOY.md mirror your own successful
breakout-scanner-streamlit deployment steps, but this is the first phase
where "verified" means "matches a working pattern" rather than "actually
run and confirmed."

**Honest scope note**: the feed listener service logs ticks to the
systemd journal only — it does NOT yet feed the dashboard's hero cards or
CVD. That integration was explicitly deferred in Phase 5 (needs a
background thread + shared state Streamlit can poll safely) and isn't
attempted here either. Running both services on one box is a step toward
that, not the integration itself.

## All phases complete

Six phases, each phase's README section above documents exactly what's
verified live vs. unit-tested vs. structurally-checked-only — worth
reading before treating any piece as more solid than it's actually shown
to be.

## Notes

- Educational and research purposes only — not financial advice.
