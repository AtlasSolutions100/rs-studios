"""Credit holds for AI runs (BILL-2, observation 6): reserve first, refuse
instead of under-collect.

Every AI run (planner, competitor research, copy studio, optimizer, landing
page copy, flow drafts, SEO suggestions, product import, Create's angles,
scripts and shorten, paid setup guide replies) takes the most it can cost out of
the balance BEFORE the provider is called:

    hold = credits.reserve(ws_id, quote, "AI ad copy")    # None: not enough, nothing called
    ...the AI call...
    credits.settle(hold, usage.credits)                    # keep what it used, give back the rest
    credits.release(hold)                                  # nothing used: give it all back

The hold is an ordinary atomic debit (db.adjust_credits: plan credits first,
refused on a billing hold), so two runs started at once can never spend the
same credits. A member is never charged more than the hold. The ledger shows
each step on its own row, tied by ledger.hold_id:

    -300  Competitor research: held up to 300
    +180  Competitor research: used 120, 180 back
    +300  Competitor research: nothing used, 300 back      (release)

A run that crashes leaves its hold; the worker gives it back after
STALE_MINUTES (release_stale). A run that still finishes after that is charged
what it used if the balance covers it, else what is there (logged).

Unused credits go back pack first (they never expire), then plan; plan credits
go back to the plan bucket only while a plan is on (like db.refund_job).

Generation jobs already work this way: queue_job takes the exact price in the
same transaction that creates the job and db.refund_job gives it back on
failure, so they don't need a hold.

CREDIT_HOLDS=off goes back to charging after the run (at most what's left).
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify")

STALE_MINUTES = 30
SWEEP_SECONDS = 60
_last_sweep = [0.0]


def enabled() -> bool:
    return (os.environ.get("CREDIT_HOLDS") or "on").strip().lower() not in ("0", "off", "false", "no")


class Hold:
    """What reserve() took. id is None when nothing is held: post-paid (CREDIT_HOLDS=off)
    or a zero quote."""
    __slots__ = ("id", "workspace_id", "amount", "label", "post_paid")

    def __init__(self, id, workspace_id, amount, label, post_paid=False):
        self.id, self.workspace_id, self.amount, self.label, self.post_paid = id, workspace_id, amount, label, post_paid

    def __repr__(self):
        return f"Hold(id={self.id}, ws={self.workspace_id}, amount={self.amount})"


def _ws(conn, ws_id):
    return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def reserve(ws_id: int, amount: int, label: str, ref: str | None = None, conn=None) -> Hold | None:
    """Take up to `amount` credits out of the balance for one AI run. None when
    the balance can't cover it (or the workspace is on a billing hold): call
    nothing. Commits on its own connection; pass the caller's `conn` when it may
    hold uncommitted writes, so they are committed first instead of blocking."""
    if conn is not None and conn.in_transaction:
        conn.commit()
    amount = max(0, int(amount or 0))
    label = (label or "AI writing")[:80]
    with db.connect() as c:
        if not enabled():
            ws = _ws(c, ws_id)
            if ws is None or db.balance(ws) < amount:
                return None
            return Hold(None, ws_id, amount, label, post_paid=True)
        if amount == 0:
            return Hold(None, ws_id, 0, label)
        for _ in range(3):
            if db.adjust_credits(c, ws_id, -amount, f"{label}: held up to {amount:,}"):
                lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                plan_part = -int(c.execute("SELECT plan_delta FROM ledger WHERE id=?", (lid,)).fetchone()[0] or 0)
                t = db.now()
                hid = c.execute("INSERT INTO credit_holds (workspace_id, amount, plan_part, label, ref, status, charged, "
                                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (ws_id, amount, plan_part, label, ref, "held", 0, t, t)).lastrowid
                c.execute("UPDATE ledger SET hold_id=? WHERE id=?", (hid, lid))
                c.commit()
                return Hold(hid, ws_id, amount, label)
            c.rollback()
            # The conditional debit lost a race (another spend moved the buckets): try again while it still fits.
            ws = _ws(c, ws_id)
            if ws is None or db.balance(ws) < amount or ("billing_hold" in ws.keys() and ws["billing_hold"]):
                return None
    return None


def link(hold: Hold | None, ref: str):
    """Tie a hold to the run it pays for ("competitor:12", "campaign:7") once that row exists."""
    if hold is None or hold.id is None:
        return
    with db.connect() as c:
        c.execute("UPDATE credit_holds SET ref=?, updated_at=? WHERE id=?", (ref, db.now(), hold.id))
        c.commit()


def load(hold_id) -> Hold | None:
    if not hold_id:
        return None
    with db.connect() as c:
        h = c.execute("SELECT * FROM credit_holds WHERE id=?", (int(hold_id),)).fetchone()
    return Hold(h["id"], h["workspace_id"], h["amount"], h["label"]) if h else None


def _give_back(c, h, n: int, reason: str):
    """Put `n` of hold row `h` back (pack part first) and write the ledger row (even for 0)."""
    n = max(0, min(int(n), int(h["amount"])))
    pack_part = int(h["amount"]) - int(h["plan_part"] or 0)
    back_pack = min(pack_part, n)
    back_plan = n - back_pack
    if back_plan:
        import plans
        if plans.plan_code(_ws(c, h["workspace_id"])) == "free":
            back_pack, back_plan = n, 0
    if n:
        c.execute("UPDATE workspaces SET credits = credits + ?, plan_credits = plan_credits + ? WHERE id=?",
                  (back_pack, back_plan, h["workspace_id"]))
    c.execute("INSERT INTO ledger (workspace_id, delta, reason, created_at, plan_delta, hold_id) VALUES (?,?,?,?,?,?)",
              (h["workspace_id"], n, reason, db.now(), back_plan, h["id"]))


def settle(hold: Hold | None, actual: int) -> int:
    """The run is over and cost `actual` credits: keep that (at most the hold),
    give the rest back. Returns the credits charged. Safe to call twice."""
    if hold is None:
        return 0
    actual = max(0, int(actual or 0))
    if hold.id is None:
        if not hold.post_paid or not actual:
            return 0
        with db.connect() as c:      # CREDIT_HOLDS=off: the old post-paid charge (at most the quote), at most what's left
            take = min(actual, hold.amount, max(0, db.balance(_ws(c, hold.workspace_id))))
            if take and db.adjust_credits(c, hold.workspace_id, -take, hold.label):
                c.commit()
                return take
        return 0
    with db.connect() as c:
        h = c.execute("SELECT * FROM credit_holds WHERE id=?", (hold.id,)).fetchone()
        if h is None:
            return 0
        charge = min(actual, int(h["amount"]))
        label = h["label"] or "AI writing"
        if h["status"] == "held":
            cur = c.execute("UPDATE credit_holds SET status='settled', charged=?, updated_at=? WHERE id=? AND status='held'",
                            (charge, db.now(), h["id"]))
            if cur.rowcount == 1:
                back = int(h["amount"]) - charge
                if not charge:
                    why = f"{label}: nothing used, {back:,} back"
                elif back:
                    why = f"{label}: used {charge:,}, {back:,} back"
                else:
                    why = f"{label}: used all {charge:,} held"
                _give_back(c, h, back, why)
                c.commit()
                return charge
            h = c.execute("SELECT * FROM credit_holds WHERE id=?", (hold.id,)).fetchone()
        if h["status"] == "settled":
            return int(h["charged"] or 0)
        # released: the run outlived STALE_MINUTES and its hold went back. Charge it now, if the balance covers it.
        take = 0
        if charge:
            have = max(0, db.balance(_ws(c, h["workspace_id"])))
            take = min(charge, have)
            if take and not db.adjust_credits(c, h["workspace_id"], -take, f"{label}: used {take:,} (late)",
                                              hold_ok=True):
                take = 0
            if take < charge:
                log.warning("credit hold %s: run finished after its hold was released; charged %s of %s",
                            h["id"], take, charge)
        c.execute("UPDATE credit_holds SET status='settled', charged=?, updated_at=? WHERE id=?", (take, db.now(), h["id"]))
        c.commit()
        return take


def release(hold: Hold | None, why: str = "") -> int:
    """Nothing was used (the run failed before any AI call, or was refused): give
    the whole hold back. Returns the credits given back."""
    if hold is None or hold.id is None:
        return 0
    return _release_id(hold.id, why)


def _release_id(hid, why=""):
    with db.connect() as c:
        h = c.execute("SELECT * FROM credit_holds WHERE id=?", (hid,)).fetchone()
        if h is None:
            return 0
        cur = c.execute("UPDATE credit_holds SET status='released', updated_at=? WHERE id=? AND status='held'",
                        (db.now(), hid))
        if cur.rowcount != 1:
            return 0
        _give_back(c, h, h["amount"], f"{h['label'] or 'AI writing'}: {why or 'nothing used'}, {int(h['amount']):,} back")
        c.commit()
        return int(h["amount"])


def release_ref(ref: str, why: str = "the run stopped") -> int:
    """Give back every open hold of a run that is known to be dead (a stale planner or competitor run)."""
    with db.connect() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM credit_holds WHERE ref=? AND status='held'", (ref,))]
    return sum(_release_id(i, why) for i in ids)


def release_stale(minutes: int = STALE_MINUTES) -> int:
    """Holds older than `minutes` belong to runs that died (deploy, crash): give
    them back. Returns how many were released."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    with db.connect() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM credit_holds WHERE status='held' AND created_at<?", (cutoff,))]
    n = 0
    for i in ids:
        if _release_id(i, "the run stopped"):
            n += 1
    if n:
        log.warning("released %s stale credit hold(s)", n)
    return n


def maybe_release_stale():
    """The worker loop's call: at most once a minute, never raises."""
    if time.time() - _last_sweep[0] < SWEEP_SECONDS:
        return
    _last_sweep[0] = time.time()
    try:
        release_stale()
    except Exception:  # noqa: BLE001
        log.exception("stale credit hold sweep failed")


def held(conn, ws_id: int) -> int:
    """Credits held right now for runs in progress."""
    return int(conn.execute("SELECT COALESCE(SUM(amount),0) FROM credit_holds WHERE workspace_id=? AND status='held'",
                            (ws_id,)).fetchone()[0] or 0)
