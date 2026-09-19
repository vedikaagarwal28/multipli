"""Bridges the Etherscan feature layer to the LightGBM scorer in risk_model.py.

The model was trained on nine parameters (risk_model.FEATS). Seven of them come
straight off WalletFeatures, two under a different name, so the mapping lives
here rather than leaking model vocabulary into the feature layer.

flagged_counterparty_share and first_funder_flagged are the exception: train.py
builds them from the training set's own scam labels, so serving them needs a
flagged-address list. With no list loaded they stay None (unknown) instead of
0.0 — the model's monotone constraint reads 0.0 as "verified clean", which is
the wrong direction to guess in when we have nothing to check against.
"""
from pathlib import Path
from typing import Optional

from app.features.wallet import flagged_counterparty_share
from app.models import TxRecord, WalletFeatures


def load_flagged(path: str) -> set[str]:
    """One lowercase 0x address per line. Blank lines and # comments ignored.
    A missing file is normal — it just leaves the two label-derived
    parameters unknown."""
    file = Path(path)
    if not file.is_file():
        return set()

    out = set()
    for line in file.read_text().splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            out.add(entry)
    return out


def to_model_features(
    features: WalletFeatures,
    records: list[TxRecord],
    flagged: set[str],
) -> dict[str, Optional[float]]:
    """WalletFeatures -> the dict risk_model.RiskModel.score() expects.
    This is also the shape decision_engine.py's `extract` callable must return."""
    funder = features.first_funder_address
    if not flagged:
        funder_flagged = None
    elif funder is None:
        funder_flagged = 0.0  # never received funds; train.py scores that 0, not unknown
    else:
        funder_flagged = float(funder in flagged)

    return {
        "address_age_days": features.address_age_days,
        "tx_count_30d": features.tx_count_30d,
        "unique_counterparties_30d": features.unique_counterparties_30d,
        "new_counterparty_ratio": features.new_counterparty_ratio,
        "pass_through_ratio": features.pass_through_ratio,
        "median_hold_minutes": features.median_hold_minutes,
        "flagged_counterparty_share": (
            flagged_counterparty_share(records, features.address, flagged) if flagged else None
        ),
        "first_funder_flagged": funder_flagged,
        "recent_activity_burst": features.recent_activity_burst_zscore,
    }
