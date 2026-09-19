"""
No network calls in this file, by design — everything here runs
against synthetic fixtures so it can run in CI or offline. The
Etherscan/RPC clients are exercised separately (manually, by
pointing at real endpoints) since mocking free-tier quirks
convincingly isn't worth it for a hackathon build.
"""


from app.features import math_utils, wallet
from app.features.bytecode import (
    classify_privileged_from_names,
    extract_push4_selectors,
)
from app.models import TxKind, TxRecord

NOW = 1_700_000_000  # fixed reference point so tests are deterministic
DAY = 86400


def tx(hash_, ts, frm, to, value=0, kind=TxKind.EXTERNAL, method_id=None,
       function_name=None, input_data="0x"):
    return TxRecord(
        hash=hash_, block_number=1, timestamp=ts,
        from_address=frm.lower(), to_address=to.lower(), value_wei=value,
        kind=kind, method_id=method_id, function_name=function_name,
        input_data=input_data,
    )


WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
FUNDER = "0xAb5801a7D398351b8bE11C439e05C5B3259aec9B"
COUNTERPARTY = "0xC02aaA39b223FE8D0A0e5C4F27ead9083C756Cc2"


# --- math_utils --------------------------------------------------------

def test_burst_zscore_needs_history():
    assert math_utils.burst_zscore(50, [1, 2]) is None  # <3 days of history


def test_burst_zscore_flags_spike():
    quiet_history = [2, 3, 1, 2, 2, 3, 1]
    z = math_utils.burst_zscore(40, quiet_history)
    assert z > 5  # a 40-tx day after a ~2/day baseline should score as a clear spike


def test_burst_zscore_zero_variance_history():
    assert math_utils.burst_zscore(5, [5, 5, 5]) == 0.0
    assert math_utils.burst_zscore(9, [5, 5, 5]) == 10.0


def test_decode_uint256_param_approve_amount():
    # approve(address,uint256) calldata: selector + address + amount
    selector = "095ea7b3"
    address_word = "0" * 24 + "c" * 40
    amount_word = "f" * 64  # near-max uint256
    calldata = "0x" + selector + address_word + amount_word
    amount = math_utils.decode_uint256_param(calldata, param_index=1)
    assert amount == int("f" * 64, 16)
    assert math_utils.is_unlimited_amount(amount)


def test_decode_uint256_small_amount_not_unlimited():
    selector = "095ea7b3"
    address_word = "0" * 24 + "c" * 40
    amount_word = format(1000, "x").zfill(64)  # 1000 wei-equivalent
    calldata = "0x" + selector + address_word + amount_word
    amount = math_utils.decode_uint256_param(calldata, param_index=1)
    assert amount == 1000
    assert not math_utils.is_unlimited_amount(amount)


# --- bytecode selector extraction --------------------------------------

def test_extract_push4_selectors_finds_dispatcher_pattern():
    # PUSH4 0xa9059cbb, EQ, PUSH2 0x0100, JUMPI  — the standard
    # Solidity `if (selector == X) jump` dispatch pattern.
    bytecode = "0x63a9059cbb14610100"
    selectors = extract_push4_selectors(bytecode)
    assert "0xa9059cbb" in selectors


def test_extract_push4_selectors_ignores_push_without_eq():
    # PUSH4 followed by unrelated opcodes (no EQ nearby) shouldn't count.
    bytecode = "0x63a9059cbb01020304"
    selectors = extract_push4_selectors(bytecode)
    assert selectors == set()


def test_extract_push4_selectors_skips_over_push_data():
    # A PUSH32 loaded with data that happens to contain byte 0x63
    # should not be misread as a real PUSH4 opcode.
    push32_with_fake_push4_inside = "7f" + "63aabbccdd" + "00" * 27
    bytecode = "0x" + push32_with_fake_push4_inside
    selectors = extract_push4_selectors(bytecode)
    assert selectors == set()


def test_classify_privileged_from_names():
    names = ["transfer", "mint", "balanceOf", "addBlackList", "totalSupply"]
    privileged = classify_privileged_from_names(names)
    assert "mint" in privileged
    assert "addBlackList" in privileged
    assert "transfer" not in privileged
    assert "balanceOf" not in privileged


# --- wallet feature functions -------------------------------------------

def test_address_age_days():
    records = [tx("0x1", NOW - 10 * DAY, FUNDER, WALLET, value=1)]
    age = wallet.address_age_days(records, now=NOW)
    assert 9.9 < age < 10.1


def test_tx_count_30d_dedupes_by_hash_across_kinds():
    # Same hash appearing as both an external and a token transfer
    # (typical for an ERC-20 transfer call) should count once.
    records = [
        tx("0xsame", NOW - DAY, FUNDER, WALLET, value=1, kind=TxKind.EXTERNAL),
        tx("0xsame", NOW - DAY, FUNDER, WALLET, value=1, kind=TxKind.TOKEN),
        tx("0xother", NOW - 40 * DAY, FUNDER, WALLET, value=1),  # outside window
    ]
    assert wallet.tx_count_30d(records, now=NOW) == 1


def test_unique_counterparties_30d():
    records = [
        tx("0x1", NOW - DAY, FUNDER, WALLET, value=1),
        tx("0x2", NOW - 2 * DAY, WALLET, COUNTERPARTY, value=1),
        tx("0x3", NOW - 2 * DAY, WALLET, FUNDER, value=1),  # repeat counterparty
    ]
    assert wallet.unique_counterparties_30d(records, WALLET, now=NOW) == 2


def test_pass_through_ratio_flags_rapid_forwarding():
    records = [
        tx("0xin", NOW - 3600, FUNDER, WALLET, value=1000),
        tx("0xout", NOW - 1800, WALLET, COUNTERPARTY, value=1000),  # 30 min later
    ]
    ratio = wallet.pass_through_ratio(records, WALLET, window_minutes=60)
    assert ratio == 1.0


def test_pass_through_ratio_no_forwarding():
    records = [
        tx("0xin", NOW - 7 * DAY, FUNDER, WALLET, value=1000),
        # no outbound at all
    ]
    ratio = wallet.pass_through_ratio(records, WALLET, window_minutes=60)
    assert ratio == 0.0


def test_median_hold_minutes():
    records = [
        tx("0xin1", NOW - 3600, FUNDER, WALLET, value=1000),
        tx("0xout1", NOW - 1800, WALLET, COUNTERPARTY, value=500),  # 30 min hold
    ]
    minutes = wallet.median_hold_minutes(records, WALLET)
    assert 29.9 < minutes < 30.1


def test_first_funder_picks_earliest_inbound():
    records = [
        tx("0x2", NOW - 5 * DAY, FUNDER, WALLET, value=1),
        tx("0x1", NOW - 10 * DAY, COUNTERPARTY, WALLET, value=1),  # earlier
    ]
    assert wallet.first_funder(records, WALLET) == COUNTERPARTY.lower()


def test_classify_transaction_type_approval():
    r = tx("0x1", NOW, WALLET, COUNTERPARTY, method_id="0x095ea7b3", function_name="approve(address,uint256)")
    assert wallet.classify_transaction_type(r) == "approval"


def test_classify_transaction_type_native_transfer():
    r = tx("0x1", NOW, WALLET, COUNTERPARTY, value=100, input_data="0x")
    assert wallet.classify_transaction_type(r) == "native_transfer"


def test_approval_is_unlimited_true_for_max_uint():
    address_word = "0" * 24 + "c" * 40
    amount_word = "f" * 64
    r = tx("0x1", NOW, WALLET, COUNTERPARTY, method_id="0x095ea7b3",
           input_data="0x095ea7b3" + address_word + amount_word)
    assert wallet.approval_is_unlimited(r) is True


def test_approval_is_unlimited_false_for_small_amount():
    address_word = "0" * 24 + "c" * 40
    amount_word = format(500, "x").zfill(64)
    r = tx("0x1", NOW, WALLET, COUNTERPARTY, method_id="0x095ea7b3",
           input_data="0x095ea7b3" + address_word + amount_word)
    assert wallet.approval_is_unlimited(r) is False


def test_set_approval_for_all_true_counts_as_unlimited():
    operator_word = "0" * 24 + "c" * 40
    bool_word = "0" * 63 + "1"
    r = tx("0x1", NOW, WALLET, COUNTERPARTY, method_id="0xa22cb465",
           input_data="0xa22cb465" + operator_word + bool_word)
    assert wallet.approval_is_unlimited(r) is True
