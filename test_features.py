"""Hand-computed check of features.py on a 5-tx synthetic wallet: python test_features.py"""
import pathlib, subprocess, sys, tempfile
import pandas as pd

d = pathlib.Path(tempfile.mkdtemp())
(d / "edges").mkdir()
t0, day = 1_600_000_000, 86400
PAY = t0 + 40 * day + 7302  # the inbound tx hash(addr, ts) picks as the scoring moment
pd.DataFrame({"node_id": [1], "is_scam": [1], "is_contract": [0], "address": ["0x1"]}).to_parquet(d / "targets_global.parquet")
# label file says the wallet was first active 10 days before the sampled edges show
(d / "addr_labels_balanced.csv.zst").write_bytes(__import__("zstandard").compress(
    b"address,is_scam,description,activity_start_ts,activity_end_ts,is_contract\n0x1,1,x,2020-09-03 12:26:40,2020-10-23 16:26:40,0\n"))
pd.DataFrame(
    [(9, 1, t0, 1.0),                         # first funder 9
     (1, 10, t0 + 600, 1.0),                  # forwarded 10 min later (outside 30d window)
     (11, 1, t0 + 40 * day, 2.0),             # inside window
     (1, 12, t0 + 40 * day + 1800, 1.5),      # forwards 1.5 of the 2.0 within 60 min
     (1, 13, t0 + 40 * day + 7200, 0.5),
     (14, 1, PAY, 3.0)],                      # the payment being scored: as_of = PAY - 1
    columns=["src_id", "dst_id", "ts", "value_eth"]).to_parquet(d / "edges" / "p.parquet")
subprocess.run([sys.executable, "features.py", f"{d}/"], check=True)
f = pd.read_parquet(d / "wallet_features.parquet").iloc[0]
print(f)
assert f.as_of == PAY - 1, 'snapshot must sit just before the payment'
assert f.first_funder == 9
assert abs(f.address_age_days - (50 + (PAY - 1 - t0 - 40 * day) / day)) < 1e-9  # true start = t0 - 10d
assert f.tx_count_30d == 3 and f.unique_counterparties_30d == 3
assert f.new_counterparty_ratio == 1.0          # 11/12/13 all first seen inside the window
assert abs(f.pass_through_ratio - 0.75) < 1e-9  # 1.5 of 2.0 forwarded within 60 min
assert f.median_hold_minutes == 30.0
assert f.recent_activity_burst > 3              # 3 txs today vs ~0.12/day history
print("ok")
