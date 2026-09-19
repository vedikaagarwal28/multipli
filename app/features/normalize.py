"""
One normalization step, three sources in. Every wallet feature reads
from a single list[TxRecord] so nothing downstream needs to know
whether a row came from txlist, txlistinternal, or tokentx.
"""
from app.models import TxKind, TxRecord


def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_normal_tx(row: dict) -> TxRecord:
    return TxRecord(
        hash=row["hash"],
        block_number=_safe_int(row.get("blockNumber")),
        timestamp=_safe_int(row.get("timeStamp")),
        from_address=row.get("from", "").lower(),
        to_address=(row.get("to") or "").lower(),
        value_wei=_safe_int(row.get("value")),
        kind=TxKind.EXTERNAL,
        is_error=row.get("isError") == "1",
        method_id=row.get("methodId") or None,
        function_name=row.get("functionName") or None,
        input_data=row.get("input", "0x") or "0x",
    )


def normalize_internal_tx(row: dict) -> TxRecord:
    return TxRecord(
        hash=row["hash"],
        block_number=_safe_int(row.get("blockNumber")),
        timestamp=_safe_int(row.get("timeStamp")),
        from_address=row.get("from", "").lower(),
        to_address=(row.get("to") or "").lower(),
        value_wei=_safe_int(row.get("value")),
        kind=TxKind.INTERNAL,
        is_error=row.get("isError") == "1",
        input_data=row.get("input", "0x") or "0x",
    )


def normalize_token_tx(row: dict) -> TxRecord:
    return TxRecord(
        hash=row["hash"],
        block_number=_safe_int(row.get("blockNumber")),
        timestamp=_safe_int(row.get("timeStamp")),
        from_address=row.get("from", "").lower(),
        to_address=(row.get("to") or "").lower(),
        value_wei=_safe_int(row.get("value")),
        kind=TxKind.TOKEN,
        method_id=row.get("methodId") or None,
        function_name=row.get("functionName") or None,
        # tokentx's own `input` field is always the literal string
        # "deprecated" per Etherscan's docs, not real calldata — pull
        # decoding data from the matching txlist row instead when a
        # feature needs it (see wallet.py's approval detection).
        input_data="0x",
        token_symbol=row.get("tokenSymbol"),
        token_decimals=_safe_int(row.get("tokenDecimal"), default=None),
    )


def build_tx_frame(
    normal_rows: list[dict],
    internal_rows: list[dict],
    token_rows: list[dict],
) -> list[TxRecord]:
    records = (
        [normalize_normal_tx(r) for r in normal_rows]
        + [normalize_internal_tx(r) for r in internal_rows]
        + [normalize_token_tx(r) for r in token_rows]
    )
    records.sort(key=lambda r: r.timestamp)
    return records
