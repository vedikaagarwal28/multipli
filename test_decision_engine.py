"""End-to-end check of decision_engine.py on a real (embedded) Postgres: python test_decision_engine.py"""
import tempfile

import pgserver

from decision_engine import DecisionEngine

SAFE = dict(address_age_days=1500.0, tx_count_30d=4.0, unique_counterparties_30d=2.0, new_counterparty_ratio=0.0,
            pass_through_ratio=0.0, median_hold_minutes=5000.0, flagged_counterparty_share=0.0,
            first_funder_flagged=0.0, recent_activity_burst=0.5)
RISKY = dict(address_age_days=2.0, tx_count_30d=40.0, unique_counterparties_30d=35.0, new_counterparty_ratio=0.9,
             pass_through_ratio=0.95, median_hold_minutes=4.0, flagged_counterparty_share=0.1,
             first_funder_flagged=1.0, recent_activity_burst=1.0)
A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
world = {A: SAFE, B: RISKY, C: SAFE}  # what the "extractor" currently sees on-chain
calls = []


def extract(addr):
    calls.append(addr)
    if world[addr] is None:
        raise TimeoutError("extractor timed out")
    return dict(world[addr]) if isinstance(world[addr], dict) else world[addr]


srv = pgserver.get_server(tempfile.mkdtemp())
e = DecisionEngine(srv.get_uri(), extract)
db = e.db

# input validation at the trust boundary
for bad in ["0x123", "hello", None, "0x" + "g" * 40]:
    try:
        e.check("alice", bad); raise AssertionError(f"accepted {bad!r}")
    except ValueError:
        pass

# Layer 2 passes a safe unknown wallet
r = e.check("alice", A.upper().replace("0X", "0x"), {"value_eth": 1})
assert r["outcome"] == "ALLOW" and r["layer"] == 2, r

# Layer 2 flags a risky one -> Layer 3 hold with a real explanation
r = e.check("alice", B, {"value_eth": 5})
assert r["outcome"] == "HOLD" and r["layer"] == 3 and r["prompt"]["band"] in ("REVIEW", "BLOCK"), r
print(r["prompt"]["text"], "\n")
assert "Why it was flagged" in r["prompt"]["text"] and "legitimate wallets" in r["prompt"]["text"]
assert "brand-new wallets" in r["prompt"]["text"]  # the new-account outlier is called out

# user decisions: bad choice rejected, allow_once doesn't remember, double-resolve rejected
try:
    e.resolve("alice", r["review_id"], "keep"); raise AssertionError("accepted recheck choice on tx review")
except ValueError:
    pass
try:
    e.resolve("bob", r["review_id"], "allow_once"); raise AssertionError("bob resolved alice's review")
except LookupError:
    pass
assert e.resolve("alice", r["review_id"], "allow_once")["outcome"] == "ALLOW"
try:
    e.resolve("alice", r["review_id"], "whitelist"); raise AssertionError("resolved twice")
except ValueError:
    pass
assert e.check("alice", B)["outcome"] == "HOLD"  # allow_once remembered nothing

# whitelist from a prompt -> Layer 1 allows without calling the extractor (fresh check)
r = e.check("alice", B)
assert e.resolve("alice", r["review_id"], "whitelist", note="my new cold wallet")["outcome"] == "ALLOW"
n = len(calls)
r = e.check("alice", B)
assert r == {**r, "outcome": "ALLOW", "layer": 1} and len(calls) == n, r

# lists are per user: bob still gets held for the same wallet
assert e.check("bob", B)["outcome"] == "HOLD"

# blacklist blocks at Layer 1 without analysis
e.blacklist("bob", C)
n = len(calls)
assert e.check("bob", C)["outcome"] == "BLOCK" and len(calls) == n
assert e.check("alice", C)["outcome"] == "ALLOW"  # alice unaffected

# periodic verification: safe whitelisted wallet stays trusted, next check pushed out
e.whitelist("alice", A, note="exchange deposit")
db.execute("UPDATE wallet_list SET next_check_at = now() - interval '1 minute' WHERE address = %s", (A,))
res = e.recheck_due()
assert [x["state"] for x in res] == ["ok"], res
row = db.execute("SELECT * FROM wallet_list WHERE user_id='alice' AND address=%s", (A,)).fetchone()
assert row["status"] == "active" and row["next_check_at"] > row["last_checked_at"]
assert e.recheck_due() == []  # nothing else due

# ...then its behaviour turns bad: next periodic check pauses it and prompts with what changed
world[A] = dict(RISKY, address_age_days=1501.0)
db.execute("UPDATE wallet_list SET next_check_at = now() - interval '1 minute' WHERE address = %s", (A,))
res = e.recheck_due()
assert res[0]["state"] == "suspended", res
rev = e.open_reviews("alice")[-1]
assert rev["kind"] == "whitelist_recheck"
print(rev["prompt"]["text"], "\n")
assert "What changed" in rev["prompt"]["text"] and "flagged" not in rev["prompt"]["headline"]
# while paused, payments go through the model again and are held with that context
r = e.check("alice", A)
assert r["outcome"] == "HOLD" and "paused" in r["prompt"]["text"], r
# user keeps it -> active again, new baseline, the recheck prompt is closed
assert e.resolve("alice", rev["id"], "keep")["outcome"] == "keep"
row = db.execute("SELECT * FROM wallet_list WHERE user_id='alice' AND address=%s", (A,)).fetchone()
assert row["status"] == "active" and row["baseline_score"] >= 90

# just-in-time: a stale whitelist entry is re-checked at payment time, not trusted blindly
world[C] = SAFE
e.whitelist("alice", C)
db.execute("UPDATE wallet_list SET last_checked_at = now() - interval '25 hours' WHERE address = %s", (C,))
world[C] = dict(SAFE, flagged_counterparty_share=0.2)  # started dealing with known-bad addresses
r = e.check("alice", C)
assert r["outcome"] == "HOLD" and "paused" in r["prompt"]["text"], r
assert db.execute("SELECT status FROM wallet_list WHERE user_id='alice' AND address=%s", (C,)).fetchone()["status"] == "suspended"

# fail closed: extractor down -> hold with explanation, never auto-allow
D = "0x" + "d" * 40
world[D] = None
r = e.check("alice", D)
assert r["outcome"] == "HOLD" and "couldn't analyse" in r["prompt"]["text"], r
# whitelisting while analysis is down: no baseline, so the first payment re-checks it
e.resolve("alice", r["review_id"], "whitelist")
world[D] = SAFE
n = len(calls)
assert e.check("alice", D)["outcome"] == "ALLOW" and len(calls) == n + 1
# stale whitelist + extractor down -> hold, retry scheduled
db.execute("UPDATE wallet_list SET last_checked_at = now() - interval '25 hours' WHERE address = %s", (D,))
world[D] = None
r = e.check("alice", D)
assert r["outcome"] == "HOLD" and "couldn't run" in r["prompt"]["text"]
assert db.execute("SELECT failed_checks FROM wallet_list WHERE address=%s", (D,)).fetchone()["failed_checks"] == 1

# cadence: young (<30d) whitelisted wallets must be re-verified every 6h, others every 24h
Y, O = "0x" + "1" * 40, "0x" + "2" * 40
world[Y], world[O] = dict(SAFE, address_age_days=5.0), dict(SAFE)
e.whitelist("alice", Y); e.whitelist("alice", O)
cad = {r["address"]: r["check_every_hours"] for r in e.entries("alice", "whitelist")}
assert cad[Y] == 6 and cad[O] == 24, cad
db.execute("UPDATE wallet_list SET last_checked_at = now() - interval '7 hours' WHERE address IN (%s, %s)", (Y, O))
n = len(calls)
assert e.check("alice", O)["outcome"] == "ALLOW" and len(calls) == n       # 7h < 24h: trusted as is
r = e.check("alice", Y)
assert len(calls) == n + 1 and r["detail"] == "whitelisted, re-checked now", r  # 7h > 6h: re-verified first
# background job only touches what's due
db.execute("UPDATE wallet_list SET next_check_at = now() + interval '1 hour' WHERE list = 'whitelist'")
db.execute("UPDATE wallet_list SET next_check_at = now() - interval '1 minute' WHERE address = %s", (Y,))
assert [x["address"] for x in e.recheck_due()] == [Y]

# user manages their own lists: view + delete (only their own row goes)
e.whitelist("bob", O)
assert O in {r["address"] for r in e.entries("alice")}
assert e.remove("alice", O) is True and e.remove("alice", O) is False
assert O not in {r["address"] for r in e.entries("alice")}
assert O in {r["address"] for r in e.entries("bob")}           # bob's entry untouched
n = len(calls)
assert e.check("alice", O)["layer"] == 2 and len(calls) == n + 1  # back to being analysed as unknown

# atomicity: if the list write fails mid-decision, the review stays open (no half-applied choice)
r = e.check("carol", B)
real_upsert = e._upsert
e._upsert = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup"))
try:
    e.resolve("carol", r["review_id"], "whitelist"); raise AssertionError("failure swallowed")
except RuntimeError:
    pass
e._upsert = real_upsert
assert db.execute("SELECT resolved_at FROM review WHERE id = %s", (r["review_id"],)).fetchone()["resolved_at"] is None
assert e.entries("carol") == []
assert e.resolve("carol", r["review_id"], "whitelist")["outcome"] == "ALLOW"  # retry works

# extractor returning junk (wrong type / non-numeric / inf) fails closed, never crashes or allows
E = "0x" + "e" * 40
for junk in [["not", "a", "dict"], {"foo": 1}, {"address_age_days": "abc"}, {"address_age_days": float("inf")}]:
    world[E] = junk
    r = e.check("alice", E)
    assert r["outcome"] == "HOLD" and "couldn't analyse" in r["prompt"]["text"], (junk, r)
world[E] = {"address_age_days": 900, "tx_count_30d": None, "extra_key": "ignored"}  # partial output is fine
assert e.check("alice", E)["outcome"] in ("ALLOW", "HOLD")

# audit trail
print(db.execute("SELECT layer, outcome, count(*) FROM decision_log GROUP BY 1, 2 ORDER BY 1, 2").fetchall())
srv.cleanup()
print("ok")
