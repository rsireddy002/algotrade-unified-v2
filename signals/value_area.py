"""
signals/value_area.py — Value Area High/Low (VAH/VAL).

IMPORTANT — validated finding (lipi-levels-replication): VAH/VAL edges are
UNRELIABLE. Median overlap with the reference implementation was 62-71%,
worse (38%) on BankNifty specifically. This is not a "might be slightly
off" caveat — it's a concluded result from your own backtesting. Every
result from this module carries a `confidence="low"` field for that
reason; don't silently upgrade it to a hard signal in downstream strategy
code without re-validating first.

For a more trustworthy level, prefer vwap.vwap_poc_proxy() (validated
<0.1% median gap) over this module's POC output, and treat VAH/VAL here as
informational/visual only, not a decision input.
"""

from dataclasses import dataclass

from signals.volume_profile import build_volume_profile

VALUE_AREA_PCT = 0.70  # standard convention: 70% of volume defines the value area


@dataclass
class ValueArea:
    vah: float
    val: float
    poc: float
    confidence: str  # always "low" — see module docstring


def compute_value_area(candles, num_bins=20, value_area_pct=VALUE_AREA_PCT):
    """Compute VAH/VAL by expanding outward from the POC bin, adding
    whichever adjacent bin (above or below) has more volume, until the
    accumulated volume reaches `value_area_pct` of the total.

    Returns a ValueArea with confidence="low" — see module docstring for
    why this isn't a typo.
    """
    profile = build_volume_profile(candles, num_bins=num_bins)
    bins = profile.bins
    total_volume = sum(b.volume for b in bins)
    if total_volume <= 0:
        raise ValueError("no volume in the given candles — cannot compute a value area")

    poc_index = bins.index(profile.poc_bin)
    included = {poc_index}
    accumulated = profile.poc_bin.volume
    low_idx, high_idx = poc_index, poc_index

    while accumulated < total_volume * value_area_pct:
        next_low = low_idx - 1
        next_high = high_idx + 1
        vol_low = bins[next_low].volume if next_low >= 0 else -1
        vol_high = bins[next_high].volume if next_high < len(bins) else -1

        if vol_low < 0 and vol_high < 0:
            break  # ran out of bins on both sides

        if vol_high >= vol_low:
            included.add(next_high)
            accumulated += bins[next_high].volume
            high_idx = next_high
        else:
            included.add(next_low)
            accumulated += bins[next_low].volume
            low_idx = next_low

    val = bins[low_idx].price_low
    vah = bins[high_idx].price_high

    return ValueArea(vah=vah, val=val, poc=profile.poc_bin.mid, confidence="low")
