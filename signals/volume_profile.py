"""
signals/volume_profile.py — Fixed-bin-count volume profile (HVN/LVN).

Uses fixed bin count, not ATR-based bin sizing — this is the approach that
replaced the noisier ATR-based version in hvn-lvn-scanner and produced
clean results, per your own validated fix. Kept unchanged here rather than
reintroducing the ATR approach.
"""

from dataclasses import dataclass


@dataclass
class VolumeProfileBin:
    price_low: float
    price_high: float
    volume: float

    @property
    def mid(self):
        return (self.price_low + self.price_high) / 2


@dataclass
class VolumeProfile:
    bins: list
    poc_bin: VolumeProfileBin  # Point of Control — highest-volume bin
    hvn_bins: list  # High Volume Nodes — top N bins by volume
    lvn_bins: list  # Low Volume Nodes — bottom N bins by volume (excluding zero-volume gaps)


def build_volume_profile(candles, num_bins=20, hvn_count=3, lvn_count=3):
    """Build a fixed-bin volume profile from a list of candles (each with
    high/low/volume). Volume is distributed evenly across each candle's
    high-low range into whichever bins it overlaps — a standard
    approximation when only OHLCV is available (no tick-by-tick prints)."""
    if not candles:
        raise ValueError("build_volume_profile requires at least one candle")

    price_min = min(c["low"] for c in candles)
    price_max = max(c["high"] for c in candles)
    if price_max <= price_min:
        raise ValueError("candle price range is degenerate (high <= low across all candles)")

    bin_width = (price_max - price_min) / num_bins
    bin_volumes = [0.0] * num_bins

    for c in candles:
        low, high, vol = c["low"], c["high"], c["volume"]
        if vol <= 0:
            continue
        candle_range = high - low
        if candle_range <= 0:
            # Single-price candle (low == high) — dump all volume in one bin.
            idx = min(int((low - price_min) / bin_width), num_bins - 1)
            bin_volumes[idx] += vol
            continue

        # Distribute this candle's volume proportionally across every bin
        # its high-low range overlaps.
        first_bin = max(0, int((low - price_min) / bin_width))
        last_bin = min(num_bins - 1, int((high - price_min) / bin_width))
        overlapped = last_bin - first_bin + 1
        vol_per_bin = vol / overlapped
        for idx in range(first_bin, last_bin + 1):
            bin_volumes[idx] += vol_per_bin

    bins = [
        VolumeProfileBin(
            price_low=price_min + i * bin_width,
            price_high=price_min + (i + 1) * bin_width,
            volume=bin_volumes[i],
        )
        for i in range(num_bins)
    ]

    sorted_by_volume = sorted(bins, key=lambda b: b.volume, reverse=True)
    poc_bin = sorted_by_volume[0]
    hvn_bins = sorted_by_volume[:hvn_count]
    # LVN candidates exclude zero-volume bins (those are gaps, not "low volume nodes").
    nonzero_sorted = sorted((b for b in bins if b.volume > 0), key=lambda b: b.volume)
    lvn_bins = nonzero_sorted[:lvn_count]

    return VolumeProfile(bins=bins, poc_bin=poc_bin, hvn_bins=hvn_bins, lvn_bins=lvn_bins)
