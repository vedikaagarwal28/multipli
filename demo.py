"""Walk real wallets through the whole decision flow and show what lands in Postgres.

  python demo.py                       # embedded Postgres + features from the training dataset
  DATABASE_URL=postgresql://... FEATURE_EXTRACTOR=pkg.module:function python demo.py 0xabc... 0xdef...
      # your Postgres + the real feature-extraction tool, on any addresses (first = "risky" role)
"""
import os
import sys
import tempfile

from decision_engine import DecisionEngine


def dataset_extractor():
    """Stand-in for the extraction tool: the 9 parameters of labelled wallets, from the training data."""
    import numpy as np
    import pandas as pd
    import train
    from risk_model import FEATS, RiskModel

    df = train.with_labels(train.wf, train.known_from(train.wf))
    df = df.merge(train.tg[["node_id", "address"]], left_on="addr", right_on="node_id")
    table = df.set_index("address")[FEATS].replace({np.nan: None}).to_dict("index")
    # demo picks from 2023+ snapshots: the newest, least in-sample part of the data
    recent = df[pd.to_datetime(df.as_of, unit="s").dt.year >= 2023]
    p = RiskModel().model.predict(recent[FEATS])
    scam = recent.address.values[np.argmax(np.where(recent.is_scam == 1, p, -1))]
    legit = recent.address.values[np.argmin(np.where(recent.is_scam == 0, p, 2))]
    return (lambda a: table[a]), scam, legit


def show(db, title):
    print(f"\n--- postgres: {title}")
    for r in db.execute("SELECT user_id, address, list, status, baseline_score, last_score, last_checked_at, "
                        "next_check_at, check_every_hours, suspended_reason FROM wallet_list ORDER BY 1, 2"):
        print("   ", {k: (v.strftime("%m-%d %H:%M") if hasattr(v, "strftime") else v) for k, v in r.items()})


def main():
    if os.environ.get("FEATURE_EXTRACTOR"):
        mod, fn = os.environ["FEATURE_EXTRACTOR"].split(":")
        extract = getattr(__import__(mod, fromlist=[fn]), fn)
        risky, safe = sys.argv[1], sys.argv[2]
    else:
        extract, risky, safe = dataset_extractor()
    dsn, srv = os.environ.get("DATABASE_URL"), None
    if not dsn:
        import pgserver
        srv = pgserver.get_server(tempfile.mkdtemp())
        dsn = srv.get_uri()
    e = DecisionEngine(dsn, extract)
    db = e.db
    print(f"risky wallet: {risky}\nsafe wallet:  {safe}")

    print("\n[1] alice pays the safe wallet")
    print("   ", e.check("alice", safe, {"value_eth": 0.5}))

    print("\n[2] alice pays the risky wallet")
    r = e.check("alice", risky, {"value_eth": 2})
    print("   ", {k: v for k, v in r.items() if k != "prompt"})
    if r["outcome"] == "HOLD":
        print("\n" + "\n".join("    | " + line for line in r["prompt"]["text"].splitlines()))
        print("\n[3] alice decides: whitelist it anyway (she knows this wallet)")
        print("   ", e.resolve("alice", r["review_id"], "whitelist", note="demo: trusted on purpose"))
        show(db, "alice's whitelist entry written")
        print("\n[4] alice pays it again -> Layer 1, no re-analysis (checked within its cadence)")
        print("   ", e.check("alice", risky))

    print("\n[5] bob pays the same risky wallet -> alice's whitelist does not apply to bob")
    r = e.check("bob", risky)
    print("   ", {k: v for k, v in r.items() if k != "prompt"})
    if r["outcome"] == "HOLD":
        print("   ", e.resolve("bob", r["review_id"], "blacklist", note="demo: never pay"))
    show(db, "bob's blacklist entry written")
    print("\n[6] bob pays it again -> Layer 1 block")
    print("   ", e.check("bob", risky))

    print("\n[7] periodic re-verification: make alice's entry due and run the recheck job")
    db.execute("UPDATE wallet_list SET next_check_at = now() - interval '1 minute' WHERE list = 'whitelist'")
    for res in e.recheck_due():
        print("   ", res)
    show(db, "after recheck (next_check_at moved out by its cadence)")

    print("\n[8] alice deletes the wallet from her whitelist")
    print("    deleted:", e.remove("alice", risky))
    show(db, "alice's row is gone, bob's blacklist untouched")

    print("\n--- postgres: decision_log")
    for row in db.execute("SELECT user_id, layer, outcome, risk_score, detail FROM decision_log ORDER BY id"):
        print("   ", row)
    if srv:
        srv.cleanup()


if __name__ == "__main__":
    main()
