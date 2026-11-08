"""Refunds and disputes (BILL-1): the credits a reversed payment bought are
voided and the workspace is put on a billing hold.

A hold means downloads only: nothing that spends credits runs (generation, AI
writing, flow emails and texts), queued jobs don't start, and every member sees
the banner HOLD_MESSAGE. The balance may go below zero when the credits were
already spent; nothing can be spent until that is paid back.

    charge.refunded          void the refunded share of the payment's credits, hold
    charge.dispute.created   void the disputed share, hold
    charge.dispute.closed    won (or an inquiry closed): credits back, hold lifted
                             when nothing else holds it; lost: stays on hold

A payment is a credit pack (purchases.stripe_payment_intent) or a plan invoice
(plan_invoices.payment_intent, or Stripe's invoice_payments lookup on newer API
versions). A reversal that arrives before the payment itself was credited is
parked ("unmatched") and applied the moment it is (apply_parked). Each charge's
refunds and each dispute is one credit_reversals row, so a replayed or older
event never voids twice: charge.amount_refunded is cumulative and only ever
raises what is voided. Admins see holds on /admin and can lift one.

BILLING_HOLD=off turns the automatic void and hold off (events are ignored, as
before this module).
"""
import logging
import os

import billing
import catalog
import db
import plans

log = logging.getLogger("campaignify")

CONTACT = "hello@campaignify.ai"
HOLD_MESSAGE = "A payment was reversed. Email us to restore access."
# reversals that keep a workspace on hold
HOLDING = ("applied", "lost")


def enabled() -> bool:
    return (os.environ.get("BILLING_HOLD") or "on").strip().lower() not in ("0", "off", "false", "no")


def on_hold(ws) -> bool:
    return bool(ws is not None and "billing_hold" in ws.keys() and ws["billing_hold"])


# ---------------------------------------------------------------- the paid thing

def _local_source(conn, pi, invoice_id=None):
    if pi:
        p = conn.execute("SELECT * FROM purchases WHERE stripe_payment_intent=? AND status='paid'", (pi,)).fetchone()
        if p:
            return _from_purchase(p)
        inv = conn.execute("SELECT * FROM plan_invoices WHERE payment_intent=?", (pi,)).fetchone()
        if inv:
            return _from_invoice(inv)
    if invoice_id:
        inv = conn.execute("SELECT * FROM plan_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if inv:
            return _from_invoice(inv)
    return None


def _from_purchase(p):
    name = catalog.PACK_BY_CODE.get(p["pack"], {}).get("name", p["pack"])
    return {"source": "purchase", "source_id": str(p["id"]), "workspace_id": p["workspace_id"],
            "credits": int(p["credits"] or 0), "amount": int(p["amount_cents"] or 0), "label": f"{name} pack",
            "bucket": "pack"}


def _from_invoice(inv):
    name = plans.PLANS.get(inv["plan"], {}).get("name", inv["plan"])
    return {"source": "plan_invoice", "source_id": inv["invoice_id"], "workspace_id": inv["workspace_id"],
            "credits": int(inv["credits"] or 0), "amount": int(inv["amount_cents"] or 0), "label": f"{name} plan",
            "bucket": "plan"}


def _find_source(pi, invoice_id=None):
    with db.connect() as conn:
        src = _local_source(conn, pi, invoice_id)
    if src or not pi or invoice_id:
        return src
    try:   # newer API versions: charges don't name their invoice
        invoice_id = billing.invoice_for_payment(pi)
    except billing.BillingError as e:
        log.warning("stripe webhook: couldn't look up the invoice for %s: %s", pi, e)
        return None
    if not invoice_id:
        return None
    with db.connect() as conn:
        return _local_source(conn, None, invoice_id)


# ---------------------------------------------------------------- applying

def _row(conn, kind, stripe_id):
    return conn.execute("SELECT * FROM credit_reversals WHERE kind=? AND stripe_id=?", (kind, stripe_id)).fetchone()


def set_hold(conn, ws_id, reason):
    conn.execute("UPDATE workspaces SET billing_hold=1, billing_hold_reason=?, billing_hold_at=COALESCE(billing_hold_at, ?) "
                 "WHERE id=?", (reason[:200], db.now(), ws_id))


def maybe_lift(conn, ws_id) -> bool:
    """Lift the hold when no reversal holds it any more. True when lifted."""
    if conn.execute(f"SELECT 1 FROM credit_reversals WHERE workspace_id=? AND status IN {HOLDING}", (ws_id,)).fetchone():
        return False
    conn.execute("UPDATE workspaces SET billing_hold=0, billing_hold_reason=NULL, billing_hold_at=NULL WHERE id=?", (ws_id,))
    return True


def _apply(conn, kind, stripe_id, pi, reversed_cents, src, row=None):
    """Void this reversal's share of the payment's credits (only what isn't
    voided yet) and hold the workspace. Returns the credits voided now."""
    row = row or _row(conn, kind, stripe_id)
    share = 1.0 if src["amount"] <= 0 else min(1.0, max(0, reversed_cents) / src["amount"])
    target = round(src["credits"] * share)
    others = conn.execute("SELECT COALESCE(SUM(credits),0) FROM credit_reversals WHERE source=? AND source_id=? "
                          "AND status IN ('applied','lost','cleared') AND id IS NOT ?",
                          (src["source"], src["source_id"], row["id"] if row else None)).fetchone()[0]
    target = max(0, min(target, src["credits"] - int(others or 0)))
    counted = row is not None and row["status"] in ("applied", "lost", "cleared")
    already = int(row["credits"] or 0) if counted else 0
    delta = max(0, target - already)
    word = "refunded" if kind == "refund" else "disputed"
    if delta:
        db.void_credits(conn, src["workspace_id"], delta, f"Payment {word}: {src['label']} credits removed", src["bucket"])
    t = db.now()
    fresh = row is None or not counted
    if row is None:
        conn.execute("INSERT INTO credit_reversals (workspace_id, kind, stripe_id, payment_intent, source, source_id, "
                     "amount_cents, credits, status, reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (src["workspace_id"], kind, stripe_id, pi, src["source"], src["source_id"], reversed_cents,
                      already + delta, "applied", f"{kind} on {src['label']}", t, t))
    else:
        status = row["status"] if counted and not delta else ("lost" if row["status"] == "lost" else "applied")
        conn.execute("UPDATE credit_reversals SET workspace_id=?, source=?, source_id=?, amount_cents=MAX(amount_cents, ?), "
                     "credits=?, status=?, reason=?, updated_at=? WHERE id=?",
                     (src["workspace_id"], src["source"], src["source_id"], reversed_cents, already + delta, status,
                      f"{kind} on {src['label']}", t, row["id"]))
    if delta or fresh:
        set_hold(conn, src["workspace_id"], f"{kind.capitalize()} on {src['label']} ({stripe_id})")
        log.warning("billing hold: workspace %s, %s %s on %s, %s credits voided",
                    src["workspace_id"], kind, stripe_id, src["label"], delta)
    return delta


def _reverse(kind, stripe_id, pi, reversed_cents, invoice_id=None):
    if not stripe_id:
        return {"ignored": "no id"}
    src = _find_source(pi, invoice_id)
    with db.connect() as conn:
        row = _row(conn, kind, stripe_id)
        if not src:
            # Not a payment of ours, or not credited yet: park it; apply_parked runs when it is.
            if row is None and pi:
                t = db.now()
                conn.execute("INSERT OR IGNORE INTO credit_reversals (kind, stripe_id, payment_intent, amount_cents, "
                             "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                             (kind, stripe_id, pi, reversed_cents, "unmatched", t, t))
            elif row is not None and row["status"] == "unmatched":
                conn.execute("UPDATE credit_reversals SET amount_cents=MAX(amount_cents, ?), updated_at=? WHERE id=?",
                             (reversed_cents, db.now(), row["id"]))
            conn.commit()
            return {"ignored": "not a campaignify payment (yet)"}
        voided = _apply(conn, kind, stripe_id, pi, reversed_cents, src, row)
        conn.commit()
    return {"ok": True, "workspace": src["workspace_id"], "voided": voided, "hold": True}


def apply_parked(conn, pi) -> int:
    """A refund or dispute that came in before this payment was credited: apply
    it now (the caller has just credited the payment and commits after)."""
    if not pi or not enabled():
        return 0
    total = 0
    for row in conn.execute("SELECT * FROM credit_reversals WHERE payment_intent=? AND status='unmatched' ORDER BY id",
                            (pi,)).fetchall():
        src = _local_source(conn, pi)
        if src:
            total += _apply(conn, row["kind"], row["stripe_id"], pi, int(row["amount_cents"] or 0), src, row)
    return total


# ---------------------------------------------------------------- webhook entry points

def charge_refunded(charge: dict) -> dict:
    if not enabled():
        return {"ignored": "billing holds are off"}
    return _reverse("refund", charge.get("id"), billing._id(charge.get("payment_intent")),
                    int(charge.get("amount_refunded") or 0), billing._id(charge.get("invoice")))


def dispute_created(dispute: dict) -> dict:
    if not enabled():
        return {"ignored": "billing holds are off"}
    return _reverse("dispute", dispute.get("id"), billing._id(dispute.get("payment_intent")),
                    int(dispute.get("amount") or 0))


def dispute_closed(dispute: dict) -> dict:
    """won, or an inquiry closed without a chargeback: the credits come back and
    the hold lifts unless something else holds it. lost: it stays on hold."""
    if not enabled():
        return {"ignored": "billing holds are off"}
    did, status = dispute.get("id"), dispute.get("status")
    if status == "lost":
        out = dispute_created(dispute)     # voids now if the created event never came; a no-op otherwise
        with db.connect() as conn:
            conn.execute("UPDATE credit_reversals SET status='lost', updated_at=? WHERE kind='dispute' AND stripe_id=? "
                         "AND status='applied'", (db.now(), did))
            conn.commit()
        return dict(out, outcome="lost")
    if status not in ("won", "warning_closed"):
        return {"ignored": f"dispute {status}"}
    with db.connect() as conn:
        row = _row(conn, "dispute", did)
        if row is None or row["status"] not in HOLDING + ("cleared",):
            if row is not None and row["status"] == "unmatched":
                conn.execute("UPDATE credit_reversals SET status='won', updated_at=? WHERE id=?", (db.now(), row["id"]))
                conn.commit()
            return {"ignored": "no hold from this dispute"}
        back = int(row["credits"] or 0)
        if back:
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (row["workspace_id"],)).fetchone()
            bucket = "plan" if row["source"] == "plan_invoice" and plans.plan_code(ws) != "free" else "pack"
            db.adjust_credits(conn, row["workspace_id"], back, "Dispute won: credits restored", bucket=bucket)
        conn.execute("UPDATE credit_reversals SET status='won', updated_at=? WHERE id=?", (db.now(), row["id"]))
        lifted = maybe_lift(conn, row["workspace_id"])
        conn.commit()
    return {"ok": True, "workspace": row["workspace_id"], "restored": back, "hold": not lifted}


# ---------------------------------------------------------------- admin

def holds(conn) -> list[dict]:
    """Workspaces on hold, newest first, each with its reversals."""
    out = []
    for w in conn.execute("SELECT id, name, credits, plan_credits, billing_hold_reason, billing_hold_at FROM workspaces "
                          "WHERE billing_hold=1 ORDER BY billing_hold_at DESC, id DESC").fetchall():
        rows = conn.execute("SELECT * FROM credit_reversals WHERE workspace_id=? ORDER BY id DESC LIMIT 10",
                            (w["id"],)).fetchall()
        out.append(dict(w, reversals=[dict(r) for r in rows]))
    return out


def lift(conn, ws_id) -> None:
    """An admin lifts the hold (the customer paid it back or it was a goodwill
    refund). Voided credits stay voided; top up separately if they should return."""
    conn.execute(f"UPDATE credit_reversals SET status='cleared', updated_at=? WHERE workspace_id=? AND status IN {HOLDING}",
                 (db.now(), ws_id))
    maybe_lift(conn, ws_id)
