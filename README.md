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

## Live path: MetaMask Snap → API → model

A MetaMask Snap reads the transaction on the confirmation screen and shows a verdict
before the user signs. Nothing is intercepted or proxied: `onTransaction` is MetaMask's
own hook, so the panel renders inside the wallet's native confirm UI.

```
MetaMask confirm screen
   └─ snap/src/index.jsx   onTransaction -> transaction.to
        ├─ GET /analyze/contract/{to}   (400 "no code" => it's an EOA, fall through)
        └─ GET /analyze/wallet/{to}
             ├─ app/clients/etherscan.py   txlist + txlistinternal + tokentx
             ├─ app/features/wallet.py     the Etherscan-derived parameters
             ├─ app/risk.py                -> the 9 parameters the model was trained on
             └─ risk_model.py              LightGBM -> {risk_score, band, factors}
        └─ renders band + score, top risk drivers, then the raw Etherscan features
```

`/analyze/wallet/{address}` now returns a `verdict` alongside `features` and
`recent_transactions`:

```jsonc
{
  "features": { "address_age_days": 412.3, "tx_count_30d": 18 },   // from Etherscan
  "recent_transactions": [],
  "verdict": {
    "risk_score": 94,           // riskier than 94% of legitimate wallets
    "band": "REVIEW",           // ALLOW | REVIEW (>=90) | BLOCK (>=99)
    "probability": 0.31,
    "factors": [                // every parameter, most influential first
      { "parameter": "new_counterparty_ratio", "value": 0.8, "impact": 0.42,
        "direction": "raises risk",
        "text": "80% of the addresses it dealt with are themselves brand-new",
        "pct_of_legit_at_or_below": 97, "pct_of_scam_at_or_below": 61 }
    ],
    "inputs": {}                // exactly what the model saw, for debugging
  }
}
```

The snap sets `SeverityLevel.Critical` (MetaMask's red warning banner) for any band
other than `ALLOW`. `/analyze/contract/` has no verdict — contracts get their own model.

### Parameter mapping

`app/risk.py` is the only place that knows both vocabularies. Six parameters map
straight across; these three do not:

| Model parameter | Comes from | Note |
|---|---|---|
| `recent_activity_burst` | `WalletFeatures.recent_activity_burst_zscore` | rename only |
| `first_funder_flagged` | `WalletFeatures.first_funder_address` | address → `1.0` if on the flagged list |
| `flagged_counterparty_share` | `app/features/wallet.py` | share of the last 30 days' transfers whose counterparty is flagged |

`tests/test_risk.py` asserts the mapped keys equal `risk_model.FEATS`, so a rename on
either side fails a test instead of silently feeding the model a NaN.

### Flagged-address list

Parameters #7 and #8 are built in training from the dataset's own scam labels, so serving
them needs a list of known scam/sanctioned addresses: `data/flagged_addresses.txt`, one
lowercase address per line (override with `FLAGGED_ADDRESSES_PATH`).

**While the file has no entries both parameters are sent as unknown, not `0.0`.** They
carry a monotone `↑` constraint, so `0.0` reads to the model as "checked and clean" —
claiming that with no list to check against would understate risk on exactly the wallets
this is meant to catch. Populate it from an OFAC SDN crypto export, Chainalysis sanctions
data, or Etherscan's phish/hack labels.

### Not yet wired

The API scores with `risk_model.py` directly — Layers 1 and 3 (per-user lists, held
payments, review prompts) still need Postgres and a `user_id` the snap doesn't send yet.
`app.risk.to_model_features` already returns the exact shape `DecisionEngine`'s `extract`
callable expects, so wiring it up is an endpoint and a sync wrapper, not a rewrite.

## Setup

```bash
uv venv && uv pip install -r requirements.txt     # or: pip install -r requirements.txt
pytest tests/                                     # API, feature layer, model bridge
python test_decision_engine.py                    # engine end-to-end on an embedded Postgres
python demo.py                                    # real dataset wallets through all 3 layers + Postgres rows
```

Run the live path, three terminals:

```bash
uvicorn app.main:app --reload                     # :8000 — loads the model at startup
cd snap && npm install && npm start               # :8080 — snap dev server, rebuilds on save
cd frontend && python -m http.server 3000         # :3000 — must be 3000, that's what CORS allows
```

Open `http://localhost:3000/base.html` in a browser with **MetaMask Flask** (regular
MetaMask will not install a `local:` snap), click **Install Snap**, then send a test
transaction. A plain Send from MetaMask's own UI is enough — no dapp needed.

> `model/risk_lgbm.txt` must keep LF line endings; LightGBM's parser rejects CRLF, and a
> Windows checkout will convert it. `.gitattributes` pins it — don't remove that entry.

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
