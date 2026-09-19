"""Train + battle-test the LightGBM wallet risk model on features from features.py.

Parameters (blockchain_risk_parameters_v2.md): #1-9. #10 address_label_status is the label itself
-> never a model input (user lists live in decision_engine.py). Contracts get their own model later.
Run: python train.py (fails loudly if a battle test fails).
"""
import json
import os

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (average_precision_score, confusion_matrix, f1_score,
                             precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import StratifiedKFold, train_test_split

from risk_model import FEATS, RiskModel

D = "data/"
M = "model/"  # trained artifacts, tracked in git
CAND = M + "candidate/"  # written here, promoted into M only after every gate passes
# domain direction: +1 = more is riskier, -1 = less risky, 0 = learned
# (flow/counterparty directions are locked too: in this sampled data they add ~no AUC, and unlocked
# they learn backwards directions that would show users "forwards 95% within an hour" as reassuring)
MONO = {"address_age_days": -1, "flagged_counterparty_share": 1, "first_funder_flagged": 1,
        "pass_through_ratio": 1, "median_hold_minutes": -1, "new_counterparty_ratio": 1}
PARAMS = dict(objective="binary", learning_rate=0.03, num_leaves=31, min_child_samples=50,
              feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1, lambda_l2=5.0,
              verbose=-1, seed=7, num_threads=8, monotone_constraints_method="advanced")

wf = pd.read_parquet(D + "wallet_features.parquet")
pairs = pd.read_parquet(D + "pairs_30d.parquet")
tg = pd.read_parquet(D + "targets_global.parquet")
pair_tot = pairs.groupby("addr").n.sum()
# labelled scams (incl. contracts) that are never scored here can always sit in the labels table
outside_scams = set(tg.node_id[tg.is_scam == 1]) - set(wf.addr)


def with_labels(df, known):
    """#7 and #8 against a labels table holding only `known` scam addresses."""
    known = np.fromiter(known, dtype=np.uint64)
    hit = pairs[np.isin(pairs.cp.values, known)].groupby("addr").n.sum()
    df = df.copy()
    df["flagged_counterparty_share"] = (hit.reindex(df.addr).fillna(0) / pair_tot.reindex(df.addr)).values
    df["first_funder_flagged"] = np.isin(df.first_funder.values, known).astype(float)
    return df


def known_from(train_df):
    return outside_scams | set(train_df.addr[train_df.is_scam == 1])


def fit(tr, feats=FEATS, seed=7):
    a, b = train_test_split(tr, test_size=0.15, stratify=tr.is_scam, random_state=seed)
    p = dict(PARAMS, seed=seed, monotone_constraints=[MONO.get(f, 0) for f in feats])
    return lgb.train(p, lgb.Dataset(a[feats], a.is_scam, weight=a.w), 3000,
                     valid_sets=[lgb.Dataset(b[feats], b.is_scam, weight=b.w)],
                     callbacks=[lgb.early_stopping(100, verbose=False)])


def year_auc(df, p):
    """AUC inside each snapshot year, size-weighted: can't be won by telling eras apart."""
    g = pd.DataFrame({"y": df.is_scam.values, "p": p, "c": df.year.values})
    g = g.groupby("c").filter(lambda x: x.y.nunique() == 2)
    a = g.groupby("c").apply(lambda x: roc_auc_score(x.y, x.p), include_groups=False)
    return float(np.average(a, weights=g.c.value_counts()[a.index]))


def metrics(df, p, t):
    y, yh = df.is_scam.values, (p >= t).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, yh, labels=[0, 1]).ravel()
    return dict(n=len(y), roc_auc=roc_auc_score(y, p), year_auc=year_auc(df, p),
                pr_auc=average_precision_score(y, p), precision=precision_score(y, yh, zero_division=0),
                recall=recall_score(y, yh), f1=f1_score(y, yh), fpr=fp / max(fp + tn, 1))


def run_cv(df, note="", feats=FEATS):
    rows, oof = [], np.zeros(len(df))
    for i, j in StratifiedKFold(5, shuffle=True, random_state=1).split(df, df.year.astype(str) + df.is_scam.astype(str)):
        known = known_from(df.iloc[i])
        tr, te = with_labels(df.iloc[i], known), with_labels(df.iloc[j], known)
        m = fit(tr, feats)
        oof[j] = m.predict(te[feats])
        rows.append((year_auc(tr, m.predict(tr[feats])), year_auc(te, oof[j])))
    r = np.array(rows)
    print(f"  CV{note}: train {r[:,0].mean():.4f} | val {r[:,1].mean():.4f} ± {r[:,1].std():.4f} | gap {r[:,0].mean()-r[:,1].mean():.4f}")
    return oof, r[:, 1].mean()


def thresholds(df, p, since):
    """REVIEW at 10% / BLOCK at 1% false-positive rate on benign wallets from `since` on.
    Recent rows only: benign score levels drift across years, older cut-offs don't transfer."""
    neg = p[(df.is_scam.values == 0) & (df.year.values >= since)]
    return {"review": float(np.quantile(neg, 0.90)), "block": float(np.quantile(neg, 0.99))}


def fmt(m):
    return " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in m.items())


def main():
    df = wf.copy()
    df["year"] = pd.to_datetime(df.as_of, unit="s").dt.year
    # scams and benigns come from different eras: keep years with >= 30 of each class and
    # weight each year's classes 50/50 so the snapshot era carries no signal
    cnt = df.groupby(["year", "is_scam"]).size().unstack(fill_value=0)
    df = df[df.year.isin(cnt.index[cnt.min(axis=1) >= 30])].sort_values("as_of").reset_index(drop=True)
    df["w"] = df.groupby(["year", "is_scam"]).addr.transform(lambda s: 1.0 / len(s))
    df["w"] *= len(df) / df.w.sum()
    print(f"wallets {len(df)}: scam {df.is_scam.sum()} benign {(df.is_scam == 0).sum()} years {sorted(df.year.unique())}")

    print("\n[1] leak scan: single-feature year AUC (≈1.0 alone = leak)")
    full = with_labels(df, known_from(df))
    for f in FEATS:
        a = year_auc(full, full[f].fillna(full[f].median()).values)
        print(f"  {f:28s} {max(a, 1 - a):.3f}")

    cut = int((df.year < 2023).sum())
    pre = df.iloc[:cut].reset_index(drop=True)
    print("\n[2] tuning grid, CV on pre-2023 only (holdout never seen)")
    best = (0, None)
    for nl, mcs, l2 in [(15, 100, 5), (31, 50, 5), (31, 100, 20), (63, 50, 5), (63, 100, 20)]:
        PARAMS.update(num_leaves=nl, min_child_samples=mcs, lambda_l2=l2)
        _, auc = run_cv(pre, f" leaves={nl} min_child={mcs} l2={l2}")
        best = max(best, (auc, (nl, mcs, l2)), key=lambda b: b[0])
    PARAMS.update(zip(["num_leaves", "min_child_samples", "lambda_l2"], best[1]))
    print(f"  -> picked leaves={best[1][0]} min_child={best[1][1]} l2={best[1][2]}")

    print("\n[3] 5-fold CV, all years, labels table rebuilt per fold (train fold only)")
    oof, cv_auc = run_cv(df)

    print("\n[4] temporal holdout: train before 2023, test 2023+ (thresholds from 2022 OOF)")
    oof_pre, _ = run_cv(pre, " pre-2023")
    th = thresholds(pre, oof_pre, 2022)
    known = known_from(pre)
    tr, te = with_labels(pre, known), with_labels(df.iloc[cut:], known)
    runs = [fit(tr, seed=s).predict(te[FEATS]) for s in (7, 11, 23)]
    p_te = runs[0]
    y = te.is_scam.values
    rng = np.random.default_rng(0)
    boot = [roc_auc_score(y[b], p_te[b]) for b in (rng.integers(0, len(y), len(y)) for _ in range(500))]
    holdout = {lvl: metrics(te, p_te, th[lvl]) for lvl in ["review", "block"]}
    for lvl, m in holdout.items():
        print(f"  @{lvl:6s} (t={th[lvl]:.2f}): {fmt(m)}")
    all_auc = roc_auc_score(y, p_te)
    seed_aucs = [roc_auc_score(y, r) for r in runs]
    print(f"  holdout roc_auc={all_auc:.3f} 95% CI [{np.percentile(boot, 2.5):.3f}, {np.percentile(boot, 97.5):.3f}]"
          f" | across seeds {np.mean(seed_aucs):.3f} ± {np.std(seed_aucs):.4f}")

    print("\n[5] label permutation within years (must be ~0.5)")
    sh = df.copy()
    sh["is_scam"] = sh.groupby("year").is_scam.transform(lambda s: np.random.default_rng(0).permutation(s.values))
    _, sh_auc = run_cv(sh, " shuffled")

    print("\n[6] ablation (val year AUC when removed)")
    for drop in [["address_age_days"], ["recent_activity_burst"], ["pass_through_ratio", "median_hold_minutes"],
                 ["flagged_counterparty_share", "first_funder_flagged"]]:
        run_cv(df, f" -{'/'.join(drop)}", [f for f in FEATS if f not in drop])

    print("\n[7] final model on all wallets + thresholds from 2024+ OOF")
    final = fit(full)
    w = np.abs(final.predict(full[FEATS], pred_contrib=True)[:, :-1]).mean(0)
    weights = {f: round(float(v), 4) for f, v in sorted(zip(FEATS, w / w.sum()), key=lambda t: -t[1])}
    print("  weights:", ", ".join(f"{f} {v:.1%}" for f, v in weights.items()))
    os.makedirs(CAND, exist_ok=True)
    final.save_model(CAND + "risk_lgbm.txt")
    # risk_score = percentile of p among recent benign wallets (OOF, so not in-sample-flattered):
    # "riskier than X% of legitimate wallets"; REVIEW = 90 / BLOCK = 99 match the 10% / 1% FPR cut-offs
    q = np.linspace(0, 1, 1001)
    benign_p = np.quantile(oof[(df.is_scam.values == 0) & (df.year.values >= 2024)], q)
    ref = {f: {c: np.nanquantile(full.loc[full.is_scam == k, f], np.linspace(0, 1, 101)).round(6).tolist()
               for c, k in [("benign", 0), ("scam", 1)]} for f in FEATS}
    json.dump({"features": FEATS, "params": {k: PARAMS[k] for k in ("num_leaves", "min_child_samples", "lambda_l2")},
               "thresholds": {"review": 90, "block": 99}, "benign_p_quantiles": benign_p.tolist(),
               "feature_reference": ref, "weights": weights, "cv_year_auc": cv_auc,
               "holdout_roc_auc": all_auc, "holdout": holdout},
              open(CAND + "risk_model.json", "w"), indent=2)

    print("\n[8] battle-test gates + RiskModel smoke test")
    assert abs(sh_auc - 0.5) < 0.03, f"shuffled labels still learnable: {sh_auc}"
    assert cv_auc > 0.8 and all_auc > 0.8, "model too weak"
    assert np.std(seed_aucs) < 0.01, "unstable across seeds"
    rm = RiskModel(CAND)
    s_old = rm.score({"address_age_days": 2000, "tx_count_30d": 3})["risk_score"]
    s_new = rm.score({"address_age_days": 0.5, "tx_count_30d": 3})["risk_score"]
    assert s_new >= s_old, "monotone age constraint broken"
    fast = rm.score({"address_age_days": 5, "pass_through_ratio": 0.95, "median_hold_minutes": 4})
    assert all(f["impact"] >= 0 for f in fast["factors"] if f["parameter"] in ("pass_through_ratio", "median_hold_minutes")), \
        "fast forwarding shown as lowering risk"
    for i in [int(np.argmax(p_te)), int(np.argmin(p_te))]:
        r = rm.score(te.iloc[i][FEATS].to_dict())
        print(f"  risk_score={r['risk_score']} band={r['band']}:", "; ".join(f["text"] for f in r["factors"][:3]))
    for f in ("risk_lgbm.txt", "risk_model.json"):
        os.replace(CAND + f, M + f)
    os.rmdir(CAND)
    print(f"  all gates passed -> promoted to {M}")


if __name__ == "__main__":
    main()
