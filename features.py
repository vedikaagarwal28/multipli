"""Extract wallet risk parameters (blockchain_risk_parameters_v2.md #1-6, #9 + inputs for #7/#8)
from the fesevu/ethereum_fraud_dataset_by_activity edge list.

Snapshot time (as_of) = one second before a real incoming payment to the wallet (picked
pseudo-randomly per wallet): exactly when the decision engine scores it in production, so only
history strictly before the payment is used. 30d window = (as_of - 30d, as_of].
Outputs: data/wallet_features.parquet, data/pairs_30d.parquet
"""
import sys

import duckdb

D = sys.argv[1] if len(sys.argv) > 1 else "data/"
c = duckdb.connect(D + "work.duckdb", config={"memory_limit": "5GB", "temp_directory": D + "tmp"})

c.sql(f"""
-- wallets only; true first activity (full chain history) from the label file, since the edge
-- list is a sample and its earliest row can be months late (#1 address_age_days)
CREATE OR REPLACE TABLE eoa AS
  SELECT t.node_id AS addr, t.is_scam, epoch(b.activity_start_ts)::BIGINT AS true_first_ts
    FROM '{D}targets_global.parquet' t JOIN read_csv('{D}addr_labels_balanced.csv.zst') b USING (address)
   WHERE t.is_contract = 0;

-- every native transfer touching a labelled wallet, from that wallet's point of view
CREATE OR REPLACE TABLE ev AS
  SELECT src_id AS addr, dst_id AS cp, ts, value_eth AS v, -1 AS dir
    FROM '{D}edges/*.parquet' WHERE src_id IN (SELECT addr FROM eoa) AND src_id <> dst_id
  UNION ALL
  SELECT dst_id, src_id, ts, value_eth, 1
    FROM '{D}edges/*.parquet' WHERE dst_id IN (SELECT addr FROM eoa) AND src_id <> dst_id;

-- scoring moment: just before a deterministic pseudo-random inbound payment
CREATE OR REPLACE TABLE snap AS
  SELECT addr, arg_min(ts, hash(addr, ts)) - 1 AS as_of FROM ev WHERE dir = 1 GROUP BY addr;

CREATE OR REPLACE TABLE past AS
  SELECT ev.*, as_of FROM ev JOIN snap USING (addr) WHERE ts <= as_of;

CREATE OR REPLACE TABLE life AS
  SELECT addr, min(ts) AS first_ts, count(*) AS n_total,
         arg_min(cp, ts) FILTER (WHERE dir = 1 AND v > 0) AS first_funder
    FROM past GROUP BY addr;

CREATE OR REPLACE TABLE w AS
  SELECT * FROM past WHERE ts > as_of - 30 * 86400;

-- ponytail: first-seen of a counterparty = its earliest tx *in this dataset*, which only holds
-- txs touching labelled addresses; swap for Etherscan txlist when extracting live.
CREATE OR REPLACE TABLE first_seen AS
  WITH cps AS (SELECT DISTINCT cp FROM w),
       t AS (SELECT src_id AS a, ts FROM '{D}edges/*.parquet' UNION ALL SELECT dst_id, ts FROM '{D}edges/*.parquet')
  SELECT a AS cp, min(ts) AS fs FROM t WHERE a IN (SELECT cp FROM cps) GROUP BY a;

CREATE OR REPLACE TABLE flow AS
  SELECT addr, dir, v, ts,
         sum(CASE WHEN dir = -1 THEN v ELSE 0 END) OVER win_fwd AS out_next_60m,
         min(CASE WHEN dir = -1 THEN ts END) OVER win_rest AS next_out_ts
    FROM w
  WINDOW win_fwd  AS (PARTITION BY addr ORDER BY ts RANGE BETWEEN CURRENT ROW AND 3600 FOLLOWING),
         win_rest AS (PARTITION BY addr ORDER BY ts ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING);

CREATE OR REPLACE TABLE daily AS
  SELECT addr, floor((as_of - ts) / 86400) AS d, count(*) AS n FROM past GROUP BY ALL;
""")

c.sql(f"""
COPY (
  WITH win AS (
    SELECT w.addr, count(*) AS tx_count_30d,
           count(DISTINCT w.cp) AS unique_counterparties_30d,
           count(DISTINCT w.cp) FILTER (WHERE fs > w.as_of - 30 * 86400) / count(DISTINCT w.cp) AS new_counterparty_ratio,
           count(*) FILTER (WHERE ts > w.as_of - 86400) AS c24
      FROM w LEFT JOIN first_seen USING (cp) GROUP BY w.addr),
  fl AS (
    SELECT addr,
           sum(least(v, out_next_60m)) FILTER (WHERE dir = 1) / nullif(sum(v) FILTER (WHERE dir = 1), 0) AS pass_through_ratio,
           median((next_out_ts - ts) / 60.0) FILTER (WHERE dir = 1 AND v > 0) AS median_hold_minutes
      FROM flow GROUP BY addr),
  dz AS (
    SELECT d.addr, sum(n * n) AS s2 FROM daily d GROUP BY d.addr)
  -- wallets with no history before the payment still count (paying a brand-new wallet is real)
  SELECT eoa.addr, eoa.is_scam, snap.as_of, life.first_funder,
         greatest(snap.as_of - least(coalesce(life.first_ts, snap.as_of), coalesce(eoa.true_first_ts, snap.as_of)), 0)
           / 86400.0 AS address_age_days,
         coalesce(win.tx_count_30d, 0) AS tx_count_30d, coalesce(win.unique_counterparties_30d, 0) AS unique_counterparties_30d,
         win.new_counterparty_ratio, fl.pass_through_ratio, fl.median_hold_minutes,
         -- z-score of last-24h count vs the wallet's own daily history (zero days included)
         (coalesce(win.c24, 0) - n_total / days) / nullif(sqrt(greatest(dz.s2 / days - (n_total / days) ** 2, 0)), 0)
           AS recent_activity_burst
    FROM eoa JOIN snap USING (addr)
         LEFT JOIN (SELECT life.*, floor((as_of - first_ts) / 86400) + 1 AS days FROM life JOIN snap USING (addr)) life USING (addr)
         LEFT JOIN win USING (addr) LEFT JOIN fl USING (addr) LEFT JOIN dz USING (addr)
) TO '{D}wallet_features.parquet';

COPY (SELECT addr, cp, count(*) AS n FROM w GROUP BY ALL) TO '{D}pairs_30d.parquet';
""")
print(c.sql(f"SELECT is_scam, count(*), median(address_age_days), median(tx_count_30d) FROM '{D}wallet_features.parquet' GROUP BY 1"))
