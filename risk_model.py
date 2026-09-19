"""Wallet scoring for the decision engine (Layer 2): load once, call score(features)."""
import json
import math
import os

import lightgbm as lgb
import numpy as np
import pandas as pd

FEATS = ["address_age_days", "tx_count_30d", "unique_counterparties_30d", "new_counterparty_ratio",
         "pass_through_ratio", "median_hold_minutes", "flagged_counterparty_share",
         "first_funder_flagged", "recent_activity_burst"]

# plain-English description of each parameter's value
DESCRIBE = {
    "address_age_days": lambda v: f"Wallet is {_days(v)} old",
    "tx_count_30d": lambda v: f"{v:.0f} transactions in the last 30 days",
    "unique_counterparties_30d": lambda v: f"Transacted with {v:.0f} different addresses in the last 30 days",
    "new_counterparty_ratio": lambda v: f"{v:.0%} of the addresses it dealt with are themselves brand-new",
    "pass_through_ratio": lambda v: f"Forwards {v:.0%} of the ETH it receives within 60 minutes",
    "median_hold_minutes": lambda v: f"Typically holds received funds for {_minutes(v)} before moving them",
    "flagged_counterparty_share": lambda v: f"{v:.1%} of its transfers involve known scam/sanctioned addresses",
    "first_funder_flagged": lambda v: ("Its very first funding came from a known scam/sanctioned address" if v
                                       else "Its first funding came from an address with no scam/sanction flag"),
    "recent_activity_burst": lambda v: ("Last-24h activity is in line with its own normal" if abs(v) < 0.5 else
                                        f"Last-24h activity is {abs(v):.1f} standard deviations "
                                        f"{'above' if v > 0 else 'below'} its own normal"),
}
MISSING = {
    "new_counterparty_ratio": "No transactions in the last 30 days, so there are no counterparties to judge",
    "median_hold_minutes": "Never moved funds on after receiving them in the last 30 days",
    "first_funder_flagged": "Has never received funds, so there is no funder to check",
    "pass_through_ratio": "Received no ETH in the last 30 days, so fund-forwarding can't be measured",
    "recent_activity_burst": "Too little history to judge whether current activity is unusual",
}


class RiskModel:
    def __init__(self, d=os.path.join(os.path.dirname(os.path.abspath(__file__)), "model", "")):
        self.model = lgb.Booster(model_file=d + "risk_lgbm.txt")
        cfg = json.load(open(d + "risk_model.json"))
        self.thresholds = cfg["thresholds"]
        self.benign_p = np.array(cfg["benign_p_quantiles"])
        self.ref = {f: {c: np.array(q) for c, q in r.items()} for f, r in cfg["feature_reference"].items()}
        self.legit_median = {f: float(self.ref[f]["benign"][50]) for f in FEATS}

    def score(self, feats):
        """feats: dict of FEATS (missing/None = unknown). Returns
        risk_score 0-100 = riskier than X% of legitimate wallets; band ALLOW/REVIEW/BLOCK;
        factors = every parameter, most influential first, with plain-English text + benchmarks."""
        x = pd.DataFrame([{f: feats.get(f) for f in FEATS}], dtype=float)
        # impact of each parameter = how much the risk drops if just that value were the typical
        # legitimate wallet's value; intuitive for users, and follows the model's locked directions
        cf = pd.concat([x] * (len(FEATS) + 1), ignore_index=True)
        for i, f in enumerate(FEATS, 1):
            cf.loc[i, f] = self.legit_median[f]
        out = self.model.predict(cf, raw_score=True)  # log-odds: no saturation near 0 / 100
        p = float(1 / (1 + np.exp(-out[0])))
        contrib = out[0] - out[1:]
        risk = int(np.searchsorted(self.benign_p, p, side="right") / len(self.benign_p) * 100)
        band = "BLOCK" if risk >= self.thresholds["block"] else "REVIEW" if risk >= self.thresholds["review"] else "ALLOW"
        factors = sorted((self._factor(f, x[f].iloc[0], c) for f, c in zip(FEATS, contrib)),
                         key=lambda d: -abs(d["impact"]))
        return {"risk_score": risk, "band": band, "probability": round(p, 4), "factors": factors}

    def _factor(self, f, v, impact):
        missing = math.isnan(v)
        d = {"parameter": f, "value": None if missing else float(v), "impact": round(float(impact), 4),
             "direction": "raises risk" if impact > 0 else "lowers risk" if impact < 0 else "neutral"}
        if missing:
            d["text"] = MISSING.get(f, f"{f} unavailable")
            return d
        d["text"] = DESCRIBE[f](v)
        if f != "first_funder_flagged":
            # share of legitimate / scam wallets (training data) with a value at or below this one
            d["pct_of_legit_at_or_below"] = _pct(self.ref[f]["benign"], v)
            d["pct_of_scam_at_or_below"] = _pct(self.ref[f]["scam"], v)
        return d


def _pct(q, v):
    return int(round(np.searchsorted(q, v, side="right") / len(q) * 100))


def _days(v):
    return f"{v * 24:.0f} hours" if v < 1 else f"{v:.0f} days" if v < 730 else f"{v / 365:.1f} years"


def _minutes(v):
    return f"{v:.0f} minutes" if v < 120 else f"{v / 60:.1f} hours" if v < 2880 else f"{v / 1440:.1f} days"
