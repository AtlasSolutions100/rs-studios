"""Refunds and disputes (BILL-1): the credits a reversed payment bought are
voided and, for a full refund or any dispute, the workspace is put on a
billing hold.

A hold means downloads only: nothing that spends credits runs (generation, AI
writing, flow emails and texts), queued jobs don't start, and every member sees
the banner HOLD_MESSAGE. Voided credits that were already spent leave the pack
bucket below zero: that much is owed (db.owed) and the next pack pays it back
first; plan credits can still be spent once nothing holds the workspace.

    charge.refunded, in full     void all of the payment's credits, hold
    charge.refunded, partly      void the refunded share only, no hold (N4, the
                                 same rule as Crowdcut: a goodwill or duplicate
                                 refund doesn't freeze anyone)
    charge.dispute.created       void the disputed share, hold
    charge.dispute.closed        won (or an inquiry closed): credits back, hold lifted
                                 when nothing else holds it; lost: stays on hold

A payment is a credit pack (purchases.stripe_payment_intent) or a plan invoice
(plan_invoices.payment_intent, stored at invoice.paid, or Stripe's
invoice_payments lookup on newer API versions). A reversal that can't be
matched yet is parked ("unmatched") and never dropped (N1): it is applied the
moment its payment is credited (apply_parked), and the hourly plan sync retries
it (retry_parked). When the Stripe lookup fails the webhook answers 503 so
Stripe retries too. A parked reversal on one of our customers fires the
payment_reversed alert until it is applied. Each charge's refunds and each
dispute is one credit_reversals row, so a replayed or older event never voids
twice: charge.amount_refunded is cumulative and only ever raises what is voided.
Admins see holds on /admin and can lift one, optionally restoring the voided
credits.

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
# a plan's renewal payment failed (plans.renewal_paused): the same downloads-only pause
RENEWAL_MESSAGE = "Your plan's renewal payment didn't go through. Update your card on Billing to keep making ads."
# reversals that keep a workspace on hold
HOLDING = ("applied", "lost")
# reversals whose credits are voided (and count against the payment's credits)
COUNTED = ("applied", "lost", "cleared", "voided")
# final: a won dispute or credits an admin gave back; a late event changes nothing
SETTLED = ("won", "restored")
# a parked reversal is retried for this long (retry_parked)
PARK_DAYS = 30


def enabled() -> bool:
    return (os.environ.get("BILLING_HOLD") or "on").strip().lower() not in ("0", "off", "false", "no")


def on_hold(ws) -> bool:
    return bool(ws is not None and "billing_hold" in ws.keys() and ws["billing_hold"])


def reason(ws):
    """"reversal" (a refund or dispute hold), "renewal" (the plan's renewal
    payment failed, plans.renewal_paused) or None when the workspace may spend."""
    if on_hold(ws):
        return "reversal"
    if plans.renewal_paused(ws):
        return "renewal"
    return None


# What a paused flow says (messaging.py, sms.py), by reason.
FLOW_REASON = {"reversal": "a payment on this workspace was reversed (email us to restore access)",
               "renewal": "the plan's renewal payment didn't go through (update your card on Billing)"}


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
    """(the paid thing, None), or (None, the lookup error) when Stripe couldn't be read."""
    with db.connect() as conn:
        src = _local_source(conn, pi, invoice_id)
    if src or not pi or invoice_id:
        return src, None
    try:   # newer API versions: charges don't name their invoice
        invoice_id = billing.invoice_for_payment(pi)
    except billing.BillingError as e:
        log.warning("stripe webhook: couldn't look up the invoice for %s: %s", pi, e)
        return None, str(e)[:200] or "lookup failed"
    if not invoice_id:
        return None, None
    with db.connect() as conn:
        return _local_source(conn, None, invoice_id), None


def _customer_workspace(conn, customer):
    """The workspace whose Stripe customer this is (plan checkouts store it), or None."""
    if not customer:
        return None
    r = conn.execute("SELECT id FROM workspaces WHERE stripe_customer_id=? LIMIT 1", (customer,)).fetchone()
    return r["id"] if r else None


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


def _apply(conn, kind, stripe_id, pi, reversed_cents, src, row=None, full=True):
    """Void this reversal's share of the payment's credits (only what isn't
    voided yet). A full refund or a dispute also holds the workspace; a partial
    refund doesn't (status 'voided'). Returns the credits voided now."""
    row = row or _row(conn, kind, stripe_id)
    if row is not None and row["status"] in SETTLED:
        return 0
    hold = kind == "dispute" or bool(full)
    share = 1.0 if src["amount"] <= 0 else min(1.0, max(0, reversed_cents) / src["amount"])
    if hold and kind == "refund":
        share = 1.0
    target = round(src["credits"] * share)
    others = conn.execute("SELECT COALESCE(SUM(credits),0) FROM credit_reversals WHERE source=? AND source_id=? "
                          f"AND status IN {COUNTED} AND id IS NOT ?",
                          (src["source"], src["source_id"], row["id"] if row else None)).fetchone()[0]
    target = max(0, min(target, src["credits"] - int(others or 0)))
    counted = row is not None and row["status"] in COUNTED
    already = int(row["credits"] or 0) if counted else 0
    delta = max(0, target - already)
    word = "refunded" if kind == "refund" else "disputed"
    if delta:
        part = "" if hold else " (part of it)"
        db.void_credits(conn, src["workspace_id"], delta, f"Payment {word}{part}: {src['label']} credits removed",
                        src["bucket"])
    prev = row["status"] if row is not None else None
    if prev == "lost":
        status = "lost"
    elif hold:
        status = "cleared" if prev == "cleared" and not delta else "applied"
    else:
        status = prev if prev in ("applied", "cleared") else "voided"
    t = db.now()
    if row is None:
        conn.execute("INSERT INTO credit_reversals (workspace_id, kind, stripe_id, payment_intent, source, source_id, "
                     "amount_cents, credits, status, reason, created_at, updated_at, full) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (src["workspace_id"], kind, stripe_id, pi, src["source"], src["source_id"], reversed_cents,
                      already + delta, status, f"{kind} on {src['label']}", t, t, 1 if hold else 0))
    else:
        conn.execute("UPDATE credit_reversals SET workspace_id=?, source=?, source_id=?, amount_cents=MAX(amount_cents, ?), "
                     "credits=?, status=?, reason=?, full=MAX(full, ?), last_error=NULL, updated_at=? WHERE id=?",
                     (src["workspace_id"], src["source"], src["source_id"], reversed_cents, already + delta, status,
                      f"{kind} on {src['label']}", 1 if hold else 0, t, row["id"]))
    if status == "applied" and (delta or prev != "applied"):
        set_hold(conn, src["workspace_id"], f"{kind.capitalize()} on {src['label']} ({stripe_id})")
        log.warning("billing hold: workspace %s, %s %s on %s, %s credits voided",
                    src["workspace_id"], kind, stripe_id, src["label"], delta)
    elif delta:
        log.warning("partial refund: workspace %s, %s on %s, %s credits voided, no hold",
                    src["workspace_id"], stripe_id, src["label"], delta)
    return delta


def _held(conn, ws_id) -> bool:
    r = conn.execute("SELECT billing_hold FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    return bool(r and r["billing_hold"])


def _alert_unapplied(kind, stripe_id, ws_id, amount_cents, err=None):
    """A reversal on one of our payments that couldn't be applied yet (N1)."""
    try:
        import alerts
        why = f"Stripe lookup failed: {err}." if err else "It isn't matched to a credited payment yet."
        alerts.payment_reversed(f"{kind.capitalize()} not applied yet", f"unapplied:{kind}:{stripe_id}", ws_id,
                                amount_cents if isinstance(amount_cents, int) else None,
                                note=f"{why} It's parked and retried every hour; no credits are voided and no "
                                     "hold is placed until it matches. Check the payment in Stripe.")
    except Exception:  # noqa: BLE001 — an alert must never fail the webhook
        log.exception("payment_reversed alert failed for %s", stripe_id)


def _reverse(kind, stripe_id, pi, reversed_cents, invoice_id=None, full=True, customer=None, ours=False):
    if not stripe_id:
        return {"ignored": "no id"}
    src, err = _find_source(pi, invoice_id)
    with db.connect() as conn:
        row = _row(conn, kind, stripe_id)
        if not src:
            # Not a payment of ours, or not credited yet: park it (never dropped). apply_parked
            # runs when the payment is credited and retry_parked every hour.
            ws_id = _customer_workspace(conn, customer) or (ours if type(ours) is int else None)
            mine = bool(ours or ws_id or (row is not None and row["ours"]))
            t = db.now()
            if row is None:
                conn.execute("INSERT OR IGNORE INTO credit_reversals (kind, stripe_id, payment_intent, amount_cents, "
                             "status, created_at, updated_at, customer, ours, full, last_error, workspace_id) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (kind, stripe_id, pi, reversed_cents, "unmatched", t, t, customer, 1 if mine else 0,
                              1 if full else 0, err, None))
            elif row["status"] == "unmatched":
                conn.execute("UPDATE credit_reversals SET amount_cents=MAX(amount_cents, ?), full=MAX(full, ?), "
                             "customer=COALESCE(customer, ?), ours=MAX(ours, ?), last_error=?, "
                             "payment_intent=COALESCE(payment_intent, ?), updated_at=? WHERE id=?",
                             (reversed_cents, 1 if full else 0, customer, 1 if mine else 0, err, pi, t, row["id"]))
            conn.commit()
        else:
            voided = _apply(conn, kind, stripe_id, pi, reversed_cents, src, row, full=full)
            conn.commit()
            return {"ok": True, "workspace": src["workspace_id"], "voided": voided,
                    "hold": _held(conn, src["workspace_id"])}
    if mine:
        _alert_unapplied(kind, stripe_id, ws_id, reversed_cents, err)
    if err:
        # Stripe couldn't be read: 503 so Stripe sends it again (it's parked meanwhile).
        return {"error": "couldn't match the payment yet", "parked": True, "retry": True}
    if mine:
        return {"ok": True, "parked": True, "workspace": ws_id}
    return {"ignored": "not a campaignify payment (yet)", "parked": True}


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
            total += _apply(conn, row["kind"], row["stripe_id"], pi, int(row["amount_cents"] or 0), src, row,
                            full=bool(row["full"]))
    return total


def retry_parked(limit: int = 200) -> dict:
    """The hourly plan sync's call (N1): try every parked reversal again. A
    payment credited since is matched locally; one of our customers' (or one
    whose Stripe lookup failed) is looked up in Stripe again. Anything of ours
    still unmatched fires payment_reversed. Never raises per row."""
    out = {"applied": 0, "parked": 0}
    if not enabled():
        return out
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=PARK_DAYS)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM credit_reversals WHERE status='unmatched' AND created_at>=? ORDER BY id LIMIT ?",
                            (since, limit)).fetchall()
    for row in rows:
        try:
            pi = row["payment_intent"]
            with db.connect() as conn:
                src = _local_source(conn, pi) if pi else None
                ws_id = _customer_workspace(conn, row["customer"])
            mine = bool(row["ours"] or ws_id)
            err = None
            if not src and pi and (mine or row["last_error"]):
                src, err = _find_source(pi)
            with db.connect() as conn:
                fresh = _row(conn, row["kind"], row["stripe_id"])
                if fresh is None or fresh["status"] != "unmatched":
                    continue
                if src:
                    _apply(conn, row["kind"], row["stripe_id"], pi, int(row["amount_cents"] or 0), src, fresh,
                           full=bool(row["full"]))
                    conn.commit()
                    out["applied"] += 1
                    log.warning("parked %s %s applied to workspace %s", row["kind"], row["stripe_id"],
                                src["workspace_id"])
                    continue
                conn.execute("UPDATE credit_reversals SET attempts=attempts+1, last_error=?, ours=MAX(ours, ?), "
                             "updated_at=? WHERE id=?", (err, 1 if mine else 0, db.now(), row["id"]))
                conn.commit()
            out["parked"] += 1
            if mine or (err and int(row["attempts"] or 0) >= 2):
                _alert_unapplied(row["kind"], row["stripe_id"], ws_id, int(row["amount_cents"] or 0), err)
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            log.exception("retrying parked reversal %s failed", row["stripe_id"])
    return out


# ---------------------------------------------------------------- webhook entry points

def _meta_ours(obj: dict):
    """Our app tag in the charge's metadata (packs): the workspace id when it names one, else True/False."""
    meta = obj.get("metadata") or {}
    if meta.get("app") != billing.APP_TAG:
        return False
    ws = str(meta.get("workspace_id") or "")
    return int(ws) if ws.isdigit() else True


def charge_refunded(charge: dict) -> dict:
    """amount_refunded is cumulative. Refunded in full (charge.refunded, or all of
    it): void everything and hold. Partly: void that share only, no hold."""
    if not enabled():
        return {"ignored": "billing holds are off"}
    amount = int(charge.get("amount") or 0)
    refunded = int(charge.get("amount_refunded") or 0)
    full = bool(charge.get("refunded")) or (amount > 0 and refunded >= amount) or amount <= 0
    ours = _meta_ours(charge)
    return _reverse("refund", charge.get("id"), billing._id(charge.get("payment_intent")), refunded,
                    billing._id(charge.get("invoice")), full=full, customer=billing._id(charge.get("customer")),
                    ours=ours)


def dispute_created(dispute: dict) -> dict:
    if not enabled():
        return {"ignored": "billing holds are off"}
    ours = _meta_ours(dispute)
    return _reverse("dispute", dispute.get("id"), billing._id(dispute.get("payment_intent")),
                    int(dispute.get("amount") or 0), full=True, ours=ours)


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
    """Workspaces on hold, newest first, each with its reversals and the credits
    a lift with "restore" would give back."""
    out = []
    for w in conn.execute("SELECT id, name, credits, plan_credits, billing_hold_reason, billing_hold_at FROM workspaces "
                          "WHERE billing_hold=1 ORDER BY billing_hold_at DESC, id DESC").fetchall():
        rows = conn.execute("SELECT * FROM credit_reversals WHERE workspace_id=? ORDER BY id DESC LIMIT 10",
                            (w["id"],)).fetchall()
        voided = conn.execute(f"SELECT COALESCE(SUM(credits),0) FROM credit_reversals WHERE workspace_id=? "
                              f"AND status IN {HOLDING}", (w["id"],)).fetchone()[0]
        out.append(dict(w, reversals=[dict(r) for r in rows], voided=int(voided or 0)))
    return out


def parked(conn) -> list[dict]:
    """Reversals on our customers' payments that aren't matched yet (N1), for /admin."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM credit_reversals WHERE status='unmatched' AND ours=1 ORDER BY id DESC LIMIT 20").fetchall()]


def lift(conn, ws_id, restore: bool = False) -> int:
    """An admin lifts the hold (the customer paid it back or it was a goodwill
    refund). By default voided credits stay voided; restore=True gives them back
    to the bucket they came from (status 'restored'). Returns the credits given back."""
    back = 0
    rows = conn.execute(f"SELECT * FROM credit_reversals WHERE workspace_id=? AND status IN {HOLDING}",
                        (ws_id,)).fetchall()
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    for r in rows:
        n = int(r["credits"] or 0)
        if restore and n:
            bucket = "plan" if r["source"] == "plan_invoice" and plans.plan_code(ws) != "free" else "pack"
            db.adjust_credits(conn, ws_id, n, f"Hold lifted: {r['kind']} credits restored", bucket=bucket)
            back += n
        conn.execute("UPDATE credit_reversals SET status=?, updated_at=? WHERE id=?",
                     ("restored" if restore else "cleared", db.now(), r["id"]))
    maybe_lift(conn, ws_id)
    return back
