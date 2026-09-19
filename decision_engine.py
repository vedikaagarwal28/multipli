"""Three-layer decision engine for outgoing transactions, per user (no lists are shared between users).

  Layer 1  deterministic: the user's own blacklist blocks; their whitelist allows, but only if the
           entry passed a behaviour check within its cadence (6h young / 24h others), else it is
           re-checked right now.
  Layer 2  LightGBM (risk_model.py) on features from the external extractor.
  Layer 3  policy: anything flagged is held and turned into a review prompt; the user's choice
           executes and feeds back into Layer 1.

Whitelist entries are never trusted forever: recheck_due() (run every 15 min from cron:
`python decision_engine.py recheck`) re-scores them on a risk-based cadence and pauses any whose
behaviour drifted, prompting the user with what changed.
"""
import math
import os
import re
import sys
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from risk_model import FEATS, RiskModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_list (
    user_id          text        NOT NULL,
    address          text        NOT NULL CHECK (address ~ '^0x[0-9a-f]{40}$'),
    list             text        NOT NULL CHECK (list IN ('whitelist', 'blacklist')),
    status           text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    note             text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    baseline_score   int,          -- risk when the user (re)confirmed it; drift is measured from here
    baseline_band    text,
    baseline_features jsonb,
    last_score       int,
    last_checked_at  timestamptz,
    next_check_at    timestamptz,
    check_every_hours int,          -- cadence; a payment needs a check younger than this
    failed_checks    int         NOT NULL DEFAULT 0,
    suspended_reason text,
    PRIMARY KEY (user_id, address)  -- one list per wallet per user: blacklisting replaces a whitelisting
);
CREATE INDEX IF NOT EXISTS wallet_list_due ON wallet_list (next_check_at)
    WHERE list = 'whitelist' AND status = 'active';

CREATE TABLE IF NOT EXISTS review (
    id          bigserial   PRIMARY KEY,
    user_id     text        NOT NULL,
    address     text        NOT NULL,
    kind        text        NOT NULL CHECK (kind IN ('transaction', 'whitelist_recheck')),
    tx          jsonb,
    prompt      jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    choice      text
);
CREATE INDEX IF NOT EXISTS review_open ON review (user_id) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS decision_log (
    id         bigserial   PRIMARY KEY,
    user_id    text        NOT NULL,
    address    text        NOT NULL,
    tx         jsonb,
    layer      int         NOT NULL,
    outcome    text        NOT NULL CHECK (outcome IN ('ALLOW', 'BLOCK', 'HOLD')),
    risk_score int,
    detail     text,
    review_id  bigint      REFERENCES review(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

RETRY_HOURS = 1          # extractor failed during a background recheck
SCORE_JUMP = 15          # risk_score rise vs baseline that pauses a whitelist entry
BAND_RANK = {"ALLOW": 0, "REVIEW": 1, "BLOCK": 2}
TX_CHOICES = {"allow_once", "whitelist", "reject_once", "blacklist"}
RECHECK_CHOICES = {"keep", "remove", "blacklist"}
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def recheck_hours(assessment, features):
    """How long a whitelist entry stays trusted: re-checked in the background on this cadence, and
    re-checked at payment time if the last check is older. Young (<30 days) or already-risky wallets
    change fastest: 6h. Everything else: 24h."""
    age = features.get("address_age_days")
    if assessment["band"] != "ALLOW" or age is None or age < 30:
        return 6
    return 24


def normalize(address):
    a = (address or "").strip().lower()
    if not ADDRESS_RE.match(a):
        raise ValueError(f"not an Ethereum address: {address!r}")
    return a


class DecisionEngine:
    def __init__(self, dsn, extract, model=None):
        """extract(address) -> dict of risk_model.FEATS (the feature-extraction tool); may raise."""
        self.db = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.db.execute(SCHEMA)
        self.extract = extract
        self.model = model or RiskModel()

    def _features(self, address):
        """Call the extraction tool and validate its output (trust boundary): unknown keys ignored,
        missing/None/NaN = unknown, anything non-numeric or infinite raises -> caller fails closed."""
        raw = self.extract(address)
        if not isinstance(raw, dict) or not raw.keys() & set(FEATS):
            raise ValueError(f"extractor returned no known parameters: {type(raw).__name__}")
        out = {}
        for f in FEATS:
            v = raw.get(f)
            v = None if v is None else float(v)
            if v is not None and math.isinf(v):
                raise ValueError(f"extractor returned {f}={v}")
            out[f] = None if v is None or math.isnan(v) else v
        return out

    # ---- entry point -------------------------------------------------------------------------

    def check(self, user_id, address, tx=None):
        """Decide on an outgoing tx to `address`. Returns {outcome: ALLOW|BLOCK|HOLD, layer, ...};
        HOLD carries review_id + prompt for the user."""
        address, context = normalize(address), []
        entry = self._entry(user_id, address)

        # Layer 1: the user's own lists
        if entry and entry["list"] == "blacklist":
            return self._log(user_id, address, tx, 1, "BLOCK", detail=f"on your blacklist since {_d(entry['created_at'])}")
        if entry and entry["status"] == "active":
            if entry["fresh"]:
                return self._log(user_id, address, tx, 1, "ALLOW", entry["last_score"],
                                 f"whitelisted, behaviour checked within {entry['check_every_hours']}h")
            result = self._recheck(entry)  # stale: verify behaviour before trusting the whitelist
            if result["state"] == "ok":
                return self._log(user_id, address, tx, 1, "ALLOW", result["risk_score"], "whitelisted, re-checked now")
            if result["state"] == "error":
                context.append("This wallet is on your whitelist, but its routine behaviour check couldn't run "
                               "right now, so we can't confirm it's still safe.")
                return self._hold(user_id, address, tx, None, None, context, error=result["error"])
            entry = self._entry(user_id, address)  # just suspended
        if entry and entry["status"] == "suspended":
            context.append(f"This wallet was on your whitelist (added {_d(entry['created_at'])}) but was paused: "
                           f"{entry['suspended_reason']}")

        # Layer 2: model
        try:
            features = self._features(address)
        except Exception as e:  # fail closed: never auto-allow what we couldn't analyse
            return self._hold(user_id, address, tx, None, None, context, error=str(e))
        a = self.model.score(features)
        if a["band"] == "ALLOW" and not context:
            return self._log(user_id, address, tx, 2, "ALLOW", a["risk_score"], "model: no anomaly")

        # Layer 3: hold for the user
        return self._hold(user_id, address, tx, features, a, context)

    # ---- layer 3: user decisions -------------------------------------------------------------

    def resolve(self, user_id, review_id, choice, note=None):
        """Apply the user's decision on a review. Returns {outcome, ...}; outcome is what to do with
        the held transaction (ALLOW/BLOCK), or the list action for whitelist rechecks."""
        r = self.db.execute("SELECT * FROM review WHERE id = %s AND user_id = %s", (review_id, user_id)).fetchone()
        if not r:
            raise LookupError("no such review for this user")
        allowed = TX_CHOICES if r["kind"] == "transaction" else RECHECK_CHOICES
        if choice not in allowed:
            raise ValueError(f"choice must be one of {sorted(allowed)}")
        addr, p = r["address"], r["prompt"]
        with self.db.transaction():  # the choice and its list change land together or not at all
            claimed = self.db.execute("UPDATE review SET resolved_at = now(), choice = %s "
                                      "WHERE id = %s AND resolved_at IS NULL RETURNING id", (choice, review_id)).fetchone()
            if not claimed:
                raise ValueError("review already resolved")
            if choice == "blacklist":
                self._put(user_id, addr, "blacklist", note)
            elif choice in ("whitelist", "keep"):
                if p.get("risk_score") is None:
                    # analysis failed when the prompt was made: trust without a baseline, re-check at once
                    self._put(user_id, addr, "whitelist", note, checked_at=None)
                else:
                    self._put(user_id, addr, "whitelist", note, p, checked_at=r["created_at"])
            elif choice == "remove":
                self.remove(user_id, addr)
            if r["kind"] == "transaction":
                outcome = "ALLOW" if choice in ("allow_once", "whitelist") else "BLOCK"
                self._log(user_id, addr, r["tx"], 3, outcome, p.get("risk_score"), f"user chose {choice}", review_id)
                return {"outcome": outcome, "choice": choice}
        return {"outcome": choice, "choice": choice}

    # ---- list management ---------------------------------------------------------------------

    def whitelist(self, user_id, address, note=None):
        """Manually whitelist: scores it first so drift can be measured. Returns the assessment so
        the caller can warn if the user is trusting a risky wallet."""
        address = normalize(address)
        features = self._features(address)
        a = self.model.score(features)
        self._put(user_id, address, "whitelist", note, _snapshot(a, features))
        return a

    def blacklist(self, user_id, address, note=None):
        self._put(user_id, normalize(address), "blacklist", note)

    def remove(self, user_id, address):
        """Delete the user's entry (whitelist or blacklist) and its stored snapshot. True if one existed."""
        address = normalize(address)
        with self.db.transaction():
            self._supersede(user_id, address)
            return self.db.execute("DELETE FROM wallet_list WHERE user_id = %s AND address = %s RETURNING address",
                                   (user_id, address)).fetchone() is not None

    def entries(self, user_id, list_name=None):
        """What the user has on their lists (for a 'manage my wallets' view), newest first."""
        return self.db.execute("""SELECT address, list, status, note, created_at, last_score, last_checked_at,
                                         next_check_at, check_every_hours, suspended_reason
                                    FROM wallet_list WHERE user_id = %s AND (%s::text IS NULL OR list = %s)
                                   ORDER BY created_at DESC""", (user_id, list_name, list_name)).fetchall()

    def open_reviews(self, user_id):
        return self.db.execute("SELECT id, kind, address, created_at, prompt FROM review "
                               "WHERE user_id = %s AND resolved_at IS NULL ORDER BY id", (user_id,)).fetchall()

    # ---- periodic whitelist verification -----------------------------------------------------

    def recheck_due(self, limit=200):
        """Re-verify whitelist entries whose next check is due. Safe to run concurrently: rows are
        leased (next_check_at pushed out) with SKIP LOCKED before the slow extractor call."""
        rows = self.db.execute("""
            UPDATE wallet_list w SET next_check_at = now() + make_interval(hours => %s)
             WHERE (user_id, address) IN (
                   SELECT user_id, address FROM wallet_list
                    WHERE list = 'whitelist' AND status = 'active' AND next_check_at <= now()
                    ORDER BY next_check_at LIMIT %s FOR UPDATE SKIP LOCKED)
            RETURNING w.*""", (RETRY_HOURS, limit)).fetchall()
        return [{"user_id": r["user_id"], "address": r["address"], **self._recheck(r)} for r in rows]

    def _recheck(self, entry):
        uid, addr = entry["user_id"], entry["address"]
        try:
            features = self._features(addr)
        except Exception as e:
            self.db.execute("UPDATE wallet_list SET failed_checks = failed_checks + 1, "
                            "next_check_at = now() + make_interval(hours => %s) WHERE user_id = %s AND address = %s",
                            (RETRY_HOURS, uid, addr))
            return {"state": "error", "error": str(e)}
        a = self.model.score(features)
        drift = self._drift(entry, a, features)
        if not drift:
            h = recheck_hours(a, features)
            self.db.execute("""UPDATE wallet_list SET last_score = %s, last_checked_at = now(), failed_checks = 0,
                                      next_check_at = now() + make_interval(hours => %s), check_every_hours = %s
                                WHERE user_id = %s AND address = %s""",
                            (a["risk_score"], h, h, uid, addr))
            return {"state": "ok", "risk_score": a["risk_score"]}
        reason = "; ".join(drift)
        prompt = self._recheck_prompt(entry, a, features, drift)
        with self.db.transaction():  # pause + prompt together: never paused silently
            self.db.execute("""UPDATE wallet_list SET status = 'suspended', suspended_reason = %s, last_score = %s,
                                      last_checked_at = now(), failed_checks = 0 WHERE user_id = %s AND address = %s""",
                            (reason, a["risk_score"], uid, addr))
            rid = self.db.execute("INSERT INTO review (user_id, address, kind, prompt) "
                                  "VALUES (%s, %s, 'whitelist_recheck', %s) RETURNING id",
                                  (uid, addr, Jsonb(prompt))).fetchone()["id"]
        return {"state": "suspended", "risk_score": a["risk_score"], "reason": reason, "review_id": rid}

    def _drift(self, entry, a, features):
        if entry["baseline_score"] is None:  # whitelisted while analysis was down: first check sets the baseline
            self._put(entry["user_id"], entry["address"], "whitelist", entry["note"], _snapshot(a, features))
            return []
        out, base = [], entry["baseline_features"] or {}
        if BAND_RANK[a["band"]] > BAND_RANK[entry["baseline_band"]]:
            out.append(f"risk band went from {entry['baseline_band']} to {a['band']}")
        elif a["risk_score"] - entry["baseline_score"] >= SCORE_JUMP:
            out.append(f"risk score rose from {entry['baseline_score']} to {a['risk_score']}")
        before, now = base.get("flagged_counterparty_share") or 0, features.get("flagged_counterparty_share") or 0
        if now > before:
            out.append(f"it started transacting with known scam/sanctioned addresses ({before:.1%} → {now:.1%} of transfers)")
        return out

    # ---- prompts -----------------------------------------------------------------------------

    def _hold(self, user_id, address, tx, features, a, context, error=None):
        prompt = _tx_prompt(address, tx, a, context, error)
        prompt["features"] = features
        with self.db.transaction():
            rid = self.db.execute("INSERT INTO review (user_id, address, kind, tx, prompt) "
                                  "VALUES (%s, %s, 'transaction', %s, %s) RETURNING id",
                                  (user_id, address, Jsonb(tx), Jsonb(prompt))).fetchone()["id"]
            out = self._log(user_id, address, tx, 3, "HOLD", a and a["risk_score"], prompt["headline"], rid)
        return {**out, "review_id": rid, "prompt": prompt}

    def _recheck_prompt(self, entry, a, features, drift):
        base = entry["baseline_features"] or {}
        changed = []
        for f in a["factors"]:
            b, n = base.get(f["parameter"]), f["value"]
            if b is not None and n is not None and _moved(f["parameter"], b, n):
                changed.append(f"{f['parameter']}: {_v(b)} → {_v(n)} ({f['text']})")
        lines = [f"A wallet on your whitelist changed behaviour, so we paused it until you confirm.",
                 f"Address: {entry['address']}  ({_etherscan(entry['address'])})",
                 f"Whitelisted: {_d(entry['created_at'])}" + (f"  note: {entry['note']}" if entry["note"] else ""),
                 f"Risk score: {entry['baseline_score']} when you whitelisted it → {a['risk_score']} now ({a['band']})",
                 "", "Why it was paused:", *[f"  • {d}" for d in drift]]
        if changed:
            lines += ["", "What changed since you whitelisted it:", *[f"  • {c}" for c in changed]]
        lines += ["", "Until you decide, payments to it are treated like any unknown wallet (analysed and held if flagged).",
                  "", "Your options:",
                  "  keep      — you still trust it; re-whitelist with today's behaviour as the new baseline",
                  "  remove    — take it off your whitelist; future payments get checked like any unknown wallet",
                  "  blacklist — block all future payments to it"]
        return {"kind": "whitelist_recheck", "address": entry["address"], "headline": lines[0],
                "risk_score": a["risk_score"], "band": a["band"], "baseline_score": entry["baseline_score"],
                "drift": drift, "changed": changed, "factors": a["factors"], "features": features,
                "options": sorted(RECHECK_CHOICES), "text": "\n".join(lines)}

    # ---- db helpers --------------------------------------------------------------------------

    def _entry(self, user_id, address):
        return self.db.execute("""SELECT *, coalesce(last_checked_at > now() - make_interval(hours => check_every_hours),
                                                    false) AS fresh
                                    FROM wallet_list WHERE user_id = %s AND address = %s""",
                               (user_id, address)).fetchone()

    def _put(self, user_id, address, lst, note, snap=None, checked_at="now"):
        with self.db.transaction():
            self._supersede(user_id, address)
            self._upsert(user_id, address, lst, note, snap or {}, checked_at)

    def _upsert(self, user_id, address, lst, note, snap, checked_at):
        wl = lst == "whitelist"
        hours = recheck_hours(snap, snap.get("features") or {}) if wl and snap else 0
        checked = None if checked_at is None or not snap else (datetime.now().astimezone() if checked_at == "now" else checked_at)
        self.db.execute("""
            INSERT INTO wallet_list (user_id, address, list, note, baseline_score, baseline_band, baseline_features,
                                     last_score, last_checked_at, next_check_at, check_every_hours)
            VALUES (%(u)s, %(a)s, %(l)s, %(n)s, %(s)s, %(b)s, %(f)s, %(s)s, %(c)s,
                    CASE WHEN %(wl)s THEN coalesce(%(c)s, now()) + make_interval(hours => %(h)s) END,
                    CASE WHEN %(wl)s THEN %(h)s END)
            ON CONFLICT (user_id, address) DO UPDATE SET
                list = EXCLUDED.list, note = coalesce(EXCLUDED.note, wallet_list.note), status = 'active',
                suspended_reason = NULL, failed_checks = 0, created_at = now(),
                baseline_score = EXCLUDED.baseline_score, baseline_band = EXCLUDED.baseline_band,
                baseline_features = EXCLUDED.baseline_features, last_score = EXCLUDED.last_score,
                last_checked_at = EXCLUDED.last_checked_at, next_check_at = EXCLUDED.next_check_at,
                check_every_hours = EXCLUDED.check_every_hours""",
            dict(u=user_id, a=address, l=lst, n=note, s=snap.get("risk_score") if wl else None,
                 b=snap.get("band") if wl else None, f=Jsonb(snap.get("features")) if wl and snap else None,
                 c=checked if wl else None, wl=wl, h=hours))

    def _supersede(self, user_id, address):
        """A list change answers any open whitelist-recheck prompt for that wallet."""
        self.db.execute("UPDATE review SET resolved_at = now(), choice = 'superseded' WHERE user_id = %s "
                        "AND address = %s AND kind = 'whitelist_recheck' AND resolved_at IS NULL", (user_id, address))

    def _log(self, user_id, address, tx, layer, outcome, risk=None, detail=None, review_id=None):
        self.db.execute("INSERT INTO decision_log (user_id, address, tx, layer, outcome, risk_score, detail, review_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (user_id, address, Jsonb(tx), layer, outcome, risk, detail, review_id))
        return {"outcome": outcome, "layer": layer, "risk_score": risk, "detail": detail}


# ---- prompt text -------------------------------------------------------------------------------

def _tx_prompt(address, tx, a, context, error):
    if a is None:
        headline = "Payment held: we couldn't analyse this wallet, so we won't send automatically."
        lines = [headline, f"Address: {address}  ({_etherscan(address)})", *context,
                 f"Reason: the risk analysis failed ({error}).",
                 "", "Only continue if you've confirmed the address with the recipient yourself."]
        rec = "reject_once"
    else:
        high = a["band"] == "BLOCK"
        headline = (f"Payment held: this wallet looks riskier than {a['risk_score']}% of legitimate wallets"
                    + (" — high risk." if high else "."))
        up = [f for f in a["factors"] if f["impact"] > 0.05][:4]
        down = [f for f in a["factors"] if f["impact"] < -0.05][:2]
        lines = [headline, f"Address: {address}  ({_etherscan(address)})"]
        if tx:
            lines.append("Transaction: " + ", ".join(f"{k}={v}" for k, v in tx.items()))
        lines += [f"Risk score: {a['risk_score']}/100 ({a['band']}; review from 90, high risk from 99)", *context,
                  "", "Why it was flagged:", *[f"  • {_explain(f)}" for f in up]]
        if down:
            lines += ["", "In its favour:", *[f"  • {_explain(f)}" for f in down]]
        val = {f["parameter"]: f["value"] for f in a["factors"]}
        if val.get("address_age_days") is not None and val["address_age_days"] < 30:
            scam_link = bool(val.get("first_funder_flagged")) or (val.get("flagged_counterparty_share") or 0) > 0
            lines += ["", "Note: brand-new wallets score high by nature. If you (or the person you're paying) just "
                          "created this wallet, " + (
                          "being new does NOT explain its links to known scam/sanctioned addresses above." if scam_link
                          else "that alone may explain the score — confirm the address with them through a channel "
                               "other than the one that sent it to you.")]
        rec = "reject_once" if high else None
    lines += ["", "Your options:",
              "  allow_once  — send this payment only; the wallet stays unknown and is checked again next time",
              "  whitelist   — send, and trust this wallet from now on (we keep re-checking its behaviour and "
              "will pause it if it changes)",
              "  reject_once — don't send this payment; nothing is remembered",
              "  blacklist   — don't send, and block all future payments to this wallet"]
    if rec:
        lines.append(f"Recommended: {rec}")
    return {"kind": "transaction", "address": address, "headline": headline,
            "risk_score": a and a["risk_score"], "band": a and a["band"], "factors": a and a["factors"],
            "context": context, "error": error, "recommendation": rec, "options": sorted(TX_CHOICES),
            "text": "\n".join(lines)}


def _explain(f):
    s = f["text"]
    lg = f.get("pct_of_legit_at_or_below")
    if f["value"] is None or lg is None:
        return s
    if lg >= 50:
        s += f" — higher than {lg}% of legitimate wallets"
    else:
        s += f" — lower than {100 - lg}% of legitimate wallets"
    sc = f["pct_of_scam_at_or_below"]
    return s + f" ({sc if lg < 50 else 100 - sc}% of scam wallets are {'this low or lower' if lg < 50 else 'this high or higher'})"


def _moved(f, b, n):
    if f == "address_age_days":  # always grows, not a behaviour change
        return False
    return abs(n - b) > max(0.1 * abs(b), 0.05 if f.endswith(("ratio", "share")) else 1)


def _snapshot(a, features):
    return {"risk_score": a["risk_score"], "band": a["band"], "features": features}


def _v(x):
    return f"{x:.3g}" if isinstance(x, float) else str(x)


def _d(ts):
    return ts.strftime("%Y-%m-%d")


def _etherscan(a):
    return f"https://etherscan.io/address/{a}"


if __name__ == "__main__":
    # cron, every 15 min (6h cadence needs finer than hourly): */15 * * * *  DATABASE_URL=... FEATURE_EXTRACTOR=pkg.module:function python decision_engine.py recheck
    if sys.argv[1:] != ["recheck"]:
        sys.exit("usage: python decision_engine.py recheck")
    mod, fn = os.environ["FEATURE_EXTRACTOR"].split(":")
    engine = DecisionEngine(os.environ["DATABASE_URL"], getattr(__import__(mod, fromlist=[fn]), fn))
    for r in engine.recheck_due():
        print(r)
