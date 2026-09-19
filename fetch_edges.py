"""Download the 4 edge columns features.py needs, in 10M-row chunks; rerun resumes."""
import os

import duckdb

U = "https://huggingface.co/datasets/fesevu/ethereum_fraud_dataset_by_activity/resolve/main/gnn_dataset/edges_all/edges.parquet"
N, STEP = 380_566_661, 10_000_000
os.makedirs("data/edges", exist_ok=True)
c = duckdb.connect(config={"threads": 16, "memory_limit": "4GB"})
c.sql("INSTALL httpfs; LOAD httpfs;")
for i, lo in enumerate(range(0, N, STEP)):
    out = f"data/edges/part-{i:03d}.parquet"
    if os.path.exists(out):
        continue
    c.sql(f"""COPY (SELECT src_id, dst_id, ts, CAST(value_wei AS DOUBLE) / 1e18 AS value_eth
                  FROM read_parquet('{U}', file_row_number = true)
                  WHERE file_row_number >= {lo} AND file_row_number < {lo + STEP})
           TO '{out}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    os.rename(out + ".tmp", out)
    print(out, flush=True)
print("done")
