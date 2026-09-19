# multipli: wallet risk decision engine

Decides whether an outgoing payment to a wallet should go through. Every list is per user; nothing is shared between users.

| Layer | What it does |
|---|---|
| 1. Deterministic | The user's **blacklist** blocks. Their **whitelist** allows, but only if the wallet's behaviour was re-verified within its cadence: **6h** for wallets under 30 days old or already flagged when whitelisted, **24h** for everything else. A stale entry is re-checked at payment time. |
| 2. LightGBM | Scores unknown wallets on the 9 wallet parameters in `blockchain_risk_parameters_v2.md`. |
| 3. Policy | Anything flagged is **held**. The user gets a prompt with the risk score, plain-English reasons benchmarked against legitimate and scam wallets, and options: `allow_once` / `whitelist` / `reject_once` / `blacklist`. Their choice updates Layer 1. |

**Whitelist re-verification:** a background job re-scores whitelisted wallets on their cadence. A wallet is **paused** (not deleted) when any of these happen:
- its risk band worsens
- its score rises by 15 or more points
- it starts transacting with known scam or sanctioned addresses

The user is then prompted with what changed and can `keep`, `remove` or `blacklist` it. If the extractor fails, payments are held, never allowed.

## Setup

```bash
uv venv && uv pip install -r requirements.txt     # or: pip install -r requirements.txt
python test_decision_engine.py                    # engine end-to-end on an embedded Postgres
python demo.py                                    # real dataset wallets through all 3 layers + Postgres rows
```

## Integrating the feature-extraction tool

The engine takes a single function:

```python
def extract(address: str) -> dict:   # lowercase 0x address
    return {"address_age_days": ..., "tx_count_30d": ..., "unique_counterparties_30d": ...,
            "new_counterparty_ratio": ..., "pass_through_ratio": ..., "median_hold_minutes": ...,
            "flagged_counterparty_share": ..., "first_funder_flagged": 0 or 1, "recent_activity_burst": ...}
```

- **Units:** as in `blockchain_risk_parameters_v2.md`. Ratios are 0–1, `median_hold_minutes` is in minutes, and windows end at the moment of scoring.
- **Missing values:** `None`, NaN or an absent key means unknown, which the model handles. Extra keys are ignored.
- **Failures:** if the function raises an error or returns junk, the payment is held.

```python
from decision_engine import DecisionEngine
engine = DecisionEngine("postgresql://user:pass@host/db", extract)   # creates tables if missing

engine.check(user_id, to_address, tx={...})   # -> {"outcome": "ALLOW" | "BLOCK" | "HOLD", "layer", "risk_score", ...}
                                              #    HOLD also returns "review_id" and "prompt" ({"text": ..., "factors": ...})
engine.resolve(user_id, review_id, choice)    # user's answer -> {"outcome": "ALLOW" | "BLOCK", ...} for the held tx
engine.open_reviews(user_id)                  # pending prompts, incl. paused-whitelist prompts
engine.entries(user_id, "whitelist")          # the user's list, with last check / next check
engine.remove(user_id, address)               # user deletes a wallet from their list
engine.whitelist(user_id, address) / engine.blacklist(user_id, address)
```

Background re-verification (cron):

```
*/15 * * * *  DATABASE_URL=postgresql://... FEATURE_EXTRACTOR=their_module:extract python decision_engine.py recheck
```

To run the demo against your Postgres and the real extractor:

```bash
DATABASE_URL=postgresql://... FEATURE_EXTRACTOR=their_module:extract python demo.py 0x<risky> 0x<safe>
```

## Postgres tables

| Table | Holds |
|---|---|
| `wallet_list` | Per-user whitelist and blacklist, keyed `(user_id, address)`. Whitelist rows also store the score and features at the time of whitelisting (the drift baseline) plus the recheck schedule. |
| `review` | Held-payment and paused-whitelist prompts, and the user's choice on each. |
| `decision_log` | Every decision: layer, outcome, score. |

## Model

- **Data:** `fesevu/ethereum_fraud_dataset_by_activity` (Hugging Face): 27.8k labelled wallets (8.4k scam) and 180M sampled native-ETH transactions.
- **Scoring moment:** each wallet is scored one second before a real incoming payment to it, which matches production.
- **Label features:** #7 and #8 are rebuilt per cross-validation fold from training labels only.
- **Era confound:** scam and legitimate wallets are weighted 50/50 within each year, so the model can't learn the era.

| Check | Result |
|---|---|
| 5-fold CV, AUC within year | 0.845 ± 0.020 (train/validation gap 0.009) |
| Time holdout (train before 2023, test 2023–24) | ROC-AUC 0.971 [0.959, 0.981], stable across seeds (±0.001) |
| At REVIEW (score ≥ 90) | precision 0.993, recall 0.874, false-positive rate 5.3% |
| At BLOCK (score ≥ 99) | precision 0.998, recall 0.584, false-positive rate 1.1% |
| Shuffled labels | 0.505 (no leakage) |

- **Risk score:** "riskier than X% of legitimate wallets".
- **Locked directions:** these parameters can only push risk one way: `address_age_days` (↓), `flagged_counterparty_share` (↑), `first_funder_flagged` (↑), `pass_through_ratio` (↑), `median_hold_minutes` (↓), `new_counterparty_ratio` (↑).
- **Retraining:** `python fetch_edges.py && python features.py && python train.py`. It downloads about 4.6 GB into `data/`, which is not tracked. `train.py` refuses to save a model that fails its gates.

**Known limits:**
- Training features are ETH-only and come from a 47% transaction sample. Retrain on the extractor's output once labelled wallets are available.
- Contracts will get their own model.
