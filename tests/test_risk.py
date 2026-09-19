"""The feature-layer -> model bridge: name mapping, the two label-derived
parameters, and that a Verdict actually comes out the other end."""
import time

from app.features.wallet import flagged_counterparty_share
from app.models import TxKind, TxRecord, Verdict, WalletFeatures
from app.risk import load_flagged, to_model_features
from risk_model import FEATS

SUBJECT = "0x" + "a" * 40
SCAMMER = "0x" + "b" * 40
NORMAL = "0x" + "c" * 40


def _tx(from_address, to_address, ts, value_wei=10**18):
    return TxRecord(
        hash=f"0x{ts}", block_number=1, timestamp=int(ts),
        from_address=from_address, to_address=to_address,
        value_wei=value_wei, kind=TxKind.EXTERNAL,
    )


def _wallet_features(**overrides):
    base = dict(
        address=SUBJECT, address_age_days=400.0, tx_count_30d=4,
        unique_counterparties_30d=2, new_counterparty_ratio=0.25,
        pass_through_ratio=0.1, median_hold_minutes=900.0,
        first_funder_address=NORMAL, recent_activity_burst_zscore=0.3,
        total_tx_count=4,
    )
    return WalletFeatures(**{**base, **overrides})


def test_every_model_parameter_is_mapped():
    """A rename on either side must fail here, not silently feed the model a NaN."""
    out = to_model_features(_wallet_features(), [], flagged=set())
    assert set(out) == set(FEATS)


def test_burst_zscore_is_renamed_for_the_model():
    out = to_model_features(_wallet_features(recent_activity_burst_zscore=2.5), [], {SCAMMER})
    assert out["recent_activity_burst"] == 2.5


def test_label_parameters_stay_unknown_without_a_flagged_list():
    """Reporting 0.0 would tell the model "verified clean" on an empty list."""
    out = to_model_features(_wallet_features(), [], flagged=set())
    assert out["flagged_counterparty_share"] is None
    assert out["first_funder_flagged"] is None


def test_first_funder_flagged_is_a_float_flag():
    assert to_model_features(_wallet_features(), [], {SCAMMER})["first_funder_flagged"] == 0.0
    assert to_model_features(
        _wallet_features(first_funder_address=SCAMMER), [], {SCAMMER}
    )["first_funder_flagged"] == 1.0
    # no inbound history at all: train.py scores this 0, not unknown
    assert to_model_features(
        _wallet_features(first_funder_address=None), [], {SCAMMER}
    )["first_funder_flagged"] == 0.0


def test_flagged_share_counts_transfers_in_the_30d_window():
    now = time.time()
    records = [
        _tx(SUBJECT, SCAMMER, now - 86400),
        _tx(SUBJECT, NORMAL, now - 86400 * 2),
        _tx(NORMAL, SUBJECT, now - 86400 * 3),
        _tx(SUBJECT, SCAMMER, now - 86400 * 60),  # outside the window, ignored
    ]
    assert flagged_counterparty_share(records, SUBJECT, {SCAMMER}, now=now) == 1 / 3
    assert flagged_counterparty_share(records, SUBJECT, set(), now=now) == 0.0
    assert flagged_counterparty_share([], SUBJECT, {SCAMMER}, now=now) is None


def test_load_flagged_skips_comments_and_blanks(tmp_path):
    listing = tmp_path / "flagged.txt"
    listing.write_text(f"# sanctions export\n{SCAMMER.upper()}\n\n  {NORMAL}  # note\n")
    assert load_flagged(str(listing)) == {SCAMMER, NORMAL}
    assert load_flagged(str(tmp_path / "absent.txt")) == set()


def test_scores_through_to_a_verdict():
    from risk_model import RiskModel

    inputs = to_model_features(_wallet_features(), [], flagged=set())
    verdict = Verdict(**RiskModel().score(inputs), inputs=inputs)

    assert 0 <= verdict.risk_score <= 100
    assert verdict.band in {"ALLOW", "REVIEW", "BLOCK"}
    assert {f.parameter for f in verdict.factors} == set(FEATS)
    assert verdict.inputs["address_age_days"] == 400.0
