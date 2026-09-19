"""
Orchestrates parameters #1-6, #9 (wallet) and #16-18 (transaction,
computed retrospectively over pulled history rather than live at
signing time — interception is out of scope for this build).

Every function here takes an already-fetched list[TxRecord] plus
whatever clients it needs for the small number of extra lookups
(counterparty age checks) — it does NOT fetch the subject wallet's
own history itself. That fetch-once, compute-many split keeps this
module testable with synthetic TxRecord fixtures and keeps the
orchestration (main.py) in charge of caching.
"""
import asyncio
import time
from typing import Optional

from app.clients.etherscan import EtherscanClient
from app.features import math_utils
from app.models import TransactionFlags, TxKind, TxRecord, WalletFeatures

SECONDS_PER_DAY = 86400
APPROVE_SELECTOR = "0x095ea7b3"
SET_APPROVAL_FOR_ALL_SELECTOR = "0xa22cb465"
PERMIT_SELECTOR = "0xd505accf"  # EIP-2612 permit(owner,spender,value,deadline,v,r,s)


# ---------------------------------------------------------------------------
# #1-3, #9 — pure derivations from the tx frame, no extra calls needed
# ---------------------------------------------------------------------------

def address_age_days(records: list[TxRecord], now: Optional[float] = None) -> Optional[float]:
    if not records:
        return None
    now = now or time.time()
    return (now - records[0].timestamp) / SECONDS_PER_DAY


def _distinct_hashes_in_window(records: list[TxRecord], window_start: float) -> set[str]:
    return {r.hash for r in records if r.timestamp >= window_start}


def tx_count_30d(records: list[TxRecord], now: Optional[float] = None) -> int:
    now = now or time.time()
    window_start = now - 30 * SECONDS_PER_DAY
    return len(_distinct_hashes_in_window(records, window_start))


def unique_counterparties_30d(records: list[TxRecord], address: str, now: Optional[float] = None) -> int:
    now = now or time.time()
    window_start = now - 30 * SECONDS_PER_DAY
    address = address.lower()
    parties = set()
    for r in records:
        if r.timestamp < window_start:
            continue
        other = r.to_address if r.from_address == address else r.from_address
        if other and other != address:
            parties.add(other)
    return len(parties)


def recent_activity_burst_zscore(records: list[TxRecord], now: Optional[float] = None) -> Optional[float]:
    now = now or time.time()
    last_24h_start = now - SECONDS_PER_DAY
    hashes_by_ts = [r.timestamp for r in records]
    recent_count = len({r.hash for r in records if r.timestamp >= last_24h_start})

    historical_ts = [ts for ts in hashes_by_ts if ts < last_24h_start]
    if not historical_ts:
        return None
    day_counts = math_utils.daily_counts(historical_ts)
    return math_utils.burst_zscore(recent_count, list(day_counts.values()))


# ---------------------------------------------------------------------------
# #5, #6 — fund flow. See math_utils / module docstring for the
# simplification: "forwarded" is indicator-based (did ANY outbound
# follow within the window), not a full FIFO value allocation.
# ---------------------------------------------------------------------------

def pass_through_ratio(records: list[TxRecord], address: str, window_minutes: int = 60) -> Optional[float]:
    address = address.lower()
    inbound = [r for r in records if r.to_address == address and r.value_wei > 0]
    outbound_ts = sorted(r.timestamp for r in records if r.from_address == address and r.value_wei > 0)
    if not inbound:
        return None

    total_in = sum(r.value_wei for r in inbound)
    if total_in == 0:
        return None

    window_seconds = window_minutes * 60
    forwarded_value = 0
    for r in inbound:
        # any outbound strictly after this inflow, within the window?
        idx_start = r.timestamp
        idx_end = r.timestamp + window_seconds
        if any(idx_start < t <= idx_end for t in outbound_ts):
            forwarded_value += r.value_wei

    return min(forwarded_value / total_in, 2.0)


def median_hold_minutes(records: list[TxRecord], address: str) -> Optional[float]:
    address = address.lower()
    inbound = sorted(
        (r.timestamp for r in records if r.to_address == address and r.value_wei > 0)
    )
    outbound = sorted(
        (r.timestamp for r in records if r.from_address == address and r.value_wei > 0)
    )
    if not inbound or not outbound:
        return None

    gaps = []
    out_idx = 0
    for in_ts in inbound:
        while out_idx < len(outbound) and outbound[out_idx] <= in_ts:
            out_idx += 1
        if out_idx < len(outbound):
            gaps.append((outbound[out_idx] - in_ts) / 60.0)
    return math_utils.median_or_none(gaps)


# ---------------------------------------------------------------------------
# #4 — needs one Etherscan call per sampled counterparty
# ---------------------------------------------------------------------------

async def new_counterparty_ratio(
    records: list[TxRecord],
    address: str,
    etherscan: EtherscanClient,
    now: Optional[float] = None,
    sample_size: int = 20,
) -> Optional[float]:
    now = now or time.time()
    window_start = now - 30 * SECONDS_PER_DAY
    address = address.lower()

    parties = list({
        (r.to_address if r.from_address == address else r.from_address)
        for r in records
        if r.timestamp >= window_start and (r.from_address or r.to_address) != address
    })
    if not parties:
        return None

    sample = parties[:sample_size]
    first_seen = await asyncio.gather(*(etherscan.get_first_seen_timestamp(p) for p in sample))

    new_count = sum(1 for ts in first_seen if ts is not None and ts >= window_start)
    known = [ts for ts in first_seen if ts is not None]
    if not known:
        return None
    return new_count / len(known)


def flagged_counterparty_share(
    records: list[TxRecord],
    address: str,
    flagged: set[str],
    now: Optional[float] = None,
) -> Optional[float]:
    """#7 — share of the last 30 days' transfers whose counterparty is a known
    scam/sanctioned address. Denominator is every transfer in the same window,
    matching how train.py builds this from the labelled pair counts."""
    now = now or time.time()
    window_start = now - 30 * SECONDS_PER_DAY
    window = [r for r in records if r.timestamp >= window_start]
    if not window:
        return None

    hits = sum(1 for r in window if (_counterparty_of(r, address) or "") in flagged)
    return hits / len(window)


def first_funder(records: list[TxRecord], address: str) -> Optional[str]:
    address = address.lower()
    inbound = [r for r in records if r.to_address == address and r.value_wei > 0]
    if not inbound:
        return None
    return sorted(inbound, key=lambda r: r.timestamp)[0].from_address


# ---------------------------------------------------------------------------
# #16-18 — per-transaction, computed over already-pulled history
# ---------------------------------------------------------------------------

def classify_transaction_type(record: TxRecord) -> str:
    fn = (record.function_name or "").lower()
    method = (record.method_id or "").lower()

    if record.kind == TxKind.TOKEN:
        if "transferfrom" in fn:
            return "token_transferFrom"
        return "token_transfer"
    if method == APPROVE_SELECTOR or "approve" in fn:
        return "approval"
    if method == SET_APPROVAL_FOR_ALL_SELECTOR or "setapprovalforall" in fn:
        return "approval_for_all"
    if method == PERMIT_SELECTOR or fn.startswith("permit"):
        return "permit"
    if "swap" in fn:
        return "swap"
    if record.input_data in ("0x", "") and record.value_wei > 0:
        return "native_transfer"
    if record.input_data in ("0x", ""):
        return "native_transfer_zero_value"
    return "contract_call"


def approval_is_unlimited(record: TxRecord) -> Optional[bool]:
    method = (record.method_id or "").lower()
    if method == APPROVE_SELECTOR:
        amount = math_utils.decode_uint256_param(record.input_data, param_index=1)
        return math_utils.is_unlimited_amount(amount) if amount is not None else None
    if method == SET_APPROVAL_FOR_ALL_SELECTOR:
        approved = math_utils.decode_bool_param(record.input_data, param_index=1)
        return approved  # approving an entire collection IS the unlimited case
    if method == PERMIT_SELECTOR:
        amount = math_utils.decode_uint256_param(record.input_data, param_index=2)
        return math_utils.is_unlimited_amount(amount) if amount is not None else None
    return None


def _counterparty_of(record: TxRecord, address: str) -> Optional[str]:
    address = address.lower()
    if record.from_address == address:
        return record.to_address or None
    return record.from_address or None


async def build_transaction_flags(
    records: list[TxRecord],
    address: str,
    etherscan: EtherscanClient,
    sample_size: int = 10,
) -> list[TransactionFlags]:
    """#16-18 for the most recent `sample_size` transactions. Capped
    because #18 costs one extra Etherscan call per transaction."""
    recent = sorted(records, key=lambda r: r.timestamp, reverse=True)[:sample_size]

    async def _flag_one(r: TxRecord) -> TransactionFlags:
        counterparty = _counterparty_of(r, address)
        counterparty_age_days = None
        counterparty_is_new = None
        if counterparty:
            first_seen = await etherscan.get_first_seen_timestamp(counterparty)
            if first_seen is not None:
                counterparty_age_days = (r.timestamp - first_seen) / SECONDS_PER_DAY
                counterparty_is_new = counterparty_age_days < 30

        return TransactionFlags(
            hash=r.hash,
            timestamp=r.timestamp,
            transaction_type=classify_transaction_type(r),
            approval_is_unlimited=approval_is_unlimited(r),
            counterparty=counterparty,
            counterparty_age_days=counterparty_age_days,
            counterparty_is_new=counterparty_is_new,
        )

    return list(await asyncio.gather(*(_flag_one(r) for r in recent)))


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

async def compute_wallet_features(
    address: str,
    records: list[TxRecord],
    etherscan: EtherscanClient,
    truncated: bool,
    new_counterparty_sample_size: int = 20,
) -> WalletFeatures:
    now = time.time()
    funder = first_funder(records, address)

    ncr = await new_counterparty_ratio(
        records, address, etherscan, now=now, sample_size=new_counterparty_sample_size
    )

    return WalletFeatures(
        address=address.lower(),
        address_age_days=address_age_days(records, now),
        tx_count_30d=tx_count_30d(records, now),
        unique_counterparties_30d=unique_counterparties_30d(records, address, now),
        new_counterparty_ratio=ncr,
        pass_through_ratio=pass_through_ratio(records, address),
        median_hold_minutes=median_hold_minutes(records, address),
        first_funder_address=funder,
        recent_activity_burst_zscore=recent_activity_burst_zscore(records, now),
        total_tx_count=len({r.hash for r in records}),
        truncated=truncated,
    )
