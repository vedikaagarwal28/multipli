"""
Pure functions only — no I/O, no async, nothing that touches a
client. This is deliberate: these are the pieces most worth unit
testing with synthetic fixtures (see tests/test_features.py), and
keeping them free of side effects makes that trivial.
"""
import statistics
from collections import defaultdict
from typing import Optional

SECONDS_PER_DAY = 86400
MAX_UINT256 = 2**256 - 1
# Treated as "practically unlimited" even when not exactly MAX_UINT256 —
# wallets sometimes approve a slightly-below-max sentinel value that is
# still, in effect, everything the holder will ever have.
UNLIMITED_APPROVAL_THRESHOLD = 2**250


def median_or_none(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def daily_counts(timestamps: list[int]) -> dict[str, int]:
    """timestamps -> {'YYYY-MM-DD': count}, UTC calendar days."""
    from datetime import datetime, timezone
    counts: dict[str, int] = defaultdict(int)
    for ts in timestamps:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        counts[day] += 1
    return dict(counts)


def burst_zscore(recent_count: int, historical_daily_counts: list[int]) -> Optional[float]:
    """z-score of `recent_count` (e.g. last 24h) against the address's
    own historical daily activity — relative to itself, not a global
    threshold, per the design note in the original plan: a bot doing
    40 tx/day every day is normal for that bot; a wallet jumping from
    2/month to 40 in a day is the signal.

    Returns None when there isn't enough history to judge (fewer than
    3 historical days) rather than a misleadingly confident number.
    """
    if len(historical_daily_counts) < 3:
        return None
    mean = statistics.mean(historical_daily_counts)
    try:
        stdev = statistics.stdev(historical_daily_counts)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        # Every historical day identical — any deviation at all is
        # meaningful, but a raw z-score would be undefined/infinite.
        # Report a large finite number instead of inf so callers
        # (JSON, thresholds) don't choke on it.
        return 10.0 if recent_count > mean else 0.0
    return (recent_count - mean) / stdev


def decode_uint256_param(calldata_hex: str, param_index: int) -> Optional[int]:
    """Extract the param_index-th 32-byte word after the 4-byte
    selector from ABI-encoded calldata. param_index is 0-based.
    Returns None if the calldata is too short."""
    data = calldata_hex[2:] if calldata_hex.startswith("0x") else calldata_hex
    start = 8 + param_index * 64  # 8 hex chars = 4-byte selector
    end = start + 64
    if len(data) < end:
        return None
    try:
        return int(data[start:end], 16)
    except ValueError:
        return None


def decode_address_param(calldata_hex: str, param_index: int) -> Optional[str]:
    word = decode_uint256_param(calldata_hex, param_index)
    if word is None:
        return None
    return "0x" + format(word, "x")[-40:].zfill(40)


def decode_bool_param(calldata_hex: str, param_index: int) -> Optional[bool]:
    word = decode_uint256_param(calldata_hex, param_index)
    if word is None:
        return None
    return word != 0


def is_unlimited_amount(amount: int) -> bool:
    return amount >= UNLIMITED_APPROVAL_THRESHOLD
