"""
signals/vwap.py — Session VWAP, and VWAP as a POC proxy.

Your own validated finding (lipi-levels-replication): VWAP-band POC is a
reliable proxy for the true volume-profile POC, with a median gap under
0.1%. This module treats the running session VWAP itself as that proxy —
it does NOT independently derive POC from a volume profile (see
volume_profile.py for that, if you want the real thing to cross-check
against).
"""

from dataclasses import dataclass


@dataclass
class VwapPoint:
    date: str
    vwap: float
    cumulative_volume: float


def session_vwap(candles):
    """Compute the running (developing) session VWAP across a list of
    candles, using typical price ((H+L+C)/3) weighted by volume. Returns
    one VwapPoint per candle. The last point's vwap value is the session's
    final VWAP — treat this as the POC proxy per the validated finding
    above."""
    if not candles:
        raise ValueError("session_vwap requires at least one candle")

    points = []
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    for c in candles:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        cumulative_pv += typical_price * c["volume"]
        cumulative_vol += c["volume"]
        vwap = cumulative_pv / cumulative_vol if cumulative_vol > 0 else typical_price
        points.append(VwapPoint(date=c["date"], vwap=vwap, cumulative_volume=cumulative_vol))
    return points


def vwap_poc_proxy(candles):
    """The final session VWAP, used as a POC proxy per the validated
    <0.1% median gap finding. Cheaper than building a full volume profile
    when you just need a "good enough" POC estimate."""
    return session_vwap(candles)[-1].vwap
