"""Card on file at sign-up (RS_CARD_REQUIRED, default off).

While it is on (and Stripe is configured), every free workspace saves a card
before it makes anything. The card is saved with Stripe Checkout in setup mode
(billing.create_setup_session): nothing is charged, no plan or subscription
starts, and Stripe runs Radar and 3D Secure as it decides. A plan starts only
when the member picks one on Billing; a pack only when they buy one.

  * New sign-ups (the form and Continue with Google, accounts.py): the starter
    credits (or the ad-offer credits, adoffer.py) wait on the workspace
    (card_pending_credits) and land when the card is saved.
  * One trial per card and per person: a card fingerprint or an email address
    (case, +tags and Gmail dots, preview.norm_email) that already had starter
    credits on another workspace gets the card saved but no credits again.
  * Existing free accounts without a card keep everything they made and their
    credits, and add a card on their next visit before making anything new
    (card_routes.gate: a full-page step; core.hold_check covers the API).
  * Never asked: workspaces on a paid plan, an admin comp or a free-month code,
    workspaces that have paid through Stripe (a plan or a pack), and site admins.

Switching the flag off releases any credits still waiting for a card.
"""
import logging
import os

from werkzeug.exceptions import HTTPException

import billing
import db
import plans
import preview

log = logging.getLogger("campaignify")

STARTER_REASON = "starter credits"
TRIES_PER_HOUR = 10          # card checkouts a workspace may start per hour


class CardRequired(HTTPException):
    """Something that makes or spends, on a free workspace that hasn't saved a card (402)."""
    code = 402
    description = "Add a card to make something new. Nothing is charged."


def required() -> bool:
    """RS_CARD_REQUIRED: 1 / on turns the card step on (default off)."""
    return (os.environ.get("RS_CARD_REQUIRED") or "").strip().lower() in ("1", "on", "true", "yes")


def active() -> bool:
    """The card step runs only when it is on and Stripe can take the card."""
    return required() and billing.configured()


def _key(row, k):
    return row[k] if row is not None and k in row.keys() else None


def exempt(conn, ws, user=None) -> bool:
    """A workspace that never needs to save a card here."""
    if ws is None:
        return True
    if user is not None and _key(user, "is_admin"):
        return True
    if _key(ws, "card_saved_at"):
        return True
    if plans.plan_code(ws) != "free":          # a paid plan, an admin comp, a free-month code
        return True
    if _key(ws, "stripe_customer_id"):          # paid for a plan through Stripe before
        return True
    return conn.execute("SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' LIMIT 1",
                        (ws["id"],)).fetchone() is not None


def needs_card(conn, ws, user=None) -> bool:
    return active() and not exempt(conn, ws, user)


def pending(ws) -> int:
    """Starter credits waiting for this workspace's card."""
    return int(_key(ws, "card_pending_credits") or 0)


def defer(conn, ws_id: int, credits: int, reason: str):
    """At sign-up: keep the starter credits for when the card is saved (no commit)."""
    conn.execute("UPDATE workspaces SET card_pending_credits=?, card_pending_reason=? WHERE id=?",
                 (int(credits), reason, ws_id))


def release_pending(conn, ws_id: int) -> int:
    """The flag went off with credits still waiting: grant them now (commits). Returns credits granted."""
    row = conn.execute("SELECT card_pending_credits, card_pending_reason FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    n = int(row["card_pending_credits"] or 0) if row else 0
    if n <= 0:
        return 0
    if conn.execute("UPDATE workspaces SET card_pending_credits=0 WHERE id=? AND card_pending_credits=?",
                    (ws_id, n)).rowcount != 1:
        conn.commit()
        return 0
    db.adjust_credits(conn, ws_id, n, row["card_pending_reason"] or STARTER_REASON)
    conn.commit()
    return n


def _had_starter(conn, ws_id: int) -> bool:
    return conn.execute("SELECT 1 FROM ledger WHERE workspace_id=? AND reason LIKE 'starter credits%' AND delta>0 LIMIT 1",
                        (ws_id,)).fetchone() is not None


def trial_used(conn, ws_id: int, fingerprint: str | None, email: str | None) -> str | None:
    """Why this card or person can't have the starter credits again: "card", "email", or None."""
    if fingerprint and conn.execute("SELECT 1 FROM card_trials WHERE fingerprint=? AND workspace_id!=? AND trial=1 "
                                    "LIMIT 1", (fingerprint, ws_id)).fetchone():
        return "card"
    norm = preview.norm_email(email or "")
    if not norm or "@" not in norm:
        return None
    if conn.execute("SELECT 1 FROM card_trials WHERE email_norm=? AND workspace_id!=? AND trial=1 LIMIT 1",
                    (norm, ws_id)).fetchone():
        return "email"
    if conn.execute("SELECT 1 FROM ad_signup_grants WHERE email_norm=? AND workspace_id!=? LIMIT 1",
                    (norm, ws_id)).fetchone():
        return "email"
    domain = norm.rsplit("@", 1)[1]
    domains = ("gmail.com", "googlemail.com") if domain == "gmail.com" else (domain,)
    for d in domains:
        for r in conn.execute("SELECT email, workspace_id FROM users WHERE email LIKE ? AND workspace_id!=?",
                              ("%@" + d, ws_id)):
            if preview.norm_email(r["email"]) == norm and _had_starter(conn, r["workspace_id"]):
                return "email"
    return None


def record_card(conn, ws_id: int, *, customer, pm_id, fingerprint, label, email) -> dict:
    """The card is saved on Stripe: mark the workspace and grant its waiting
    starter credits unless this card or person already had them (commits).
    Safe to call twice (the return page and the webhook): the second call
    changes nothing. Returns {"ok", "already", "credits", "reused"}."""
    cur = conn.execute("UPDATE workspaces SET card_saved_at=?, card_customer_id=COALESCE(card_customer_id, ?), "
                       "card_pm_id=?, card_label=? WHERE id=? AND card_saved_at IS NULL",
                       (db.now(), customer, pm_id, (label or "")[:60], ws_id))
    if cur.rowcount != 1:
        conn.commit()
        return {"ok": True, "already": True, "credits": 0, "reused": None}
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    had = _had_starter(conn, ws_id)
    waiting = pending(ws)
    reused = trial_used(conn, ws_id, fingerprint, email) if waiting > 0 and not had else None
    granted = 0
    if waiting > 0 and not had and not reused:
        db.adjust_credits(conn, ws_id, waiting, ws["card_pending_reason"] or STARTER_REASON)
        granted = waiting
    conn.execute("UPDATE workspaces SET card_pending_credits=0 WHERE id=?", (ws_id,))
    conn.execute("INSERT INTO card_trials (workspace_id, fingerprint, email_norm, trial, credits, created_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (ws_id, fingerprint or None, preview.norm_email(email or "") or None, 1 if (had or granted) else 0,
                  granted, db.now()))
    conn.commit()
    if reused:
        log.info("card saved on workspace %s without starter credits (%s already had a trial)", ws_id, reused)
    return {"ok": True, "already": False, "credits": granted, "reused": reused}


def card_label(card: dict) -> str:
    brand = (card.get("display_brand") or card.get("brand") or "card").replace("_", " ").title()
    return f"{brand} ending {card.get('last4') or '????'}"


def from_session(conn, sess: dict, ws_id: int | None = None, email: str | None = None) -> dict:
    """Read a setup-mode Checkout Session (billing.get_setup_session) and save its card.
    Returns {"ok": False, "why": "not_ours" | "other_workspace" | "declined"} when it
    can't, else record_card's answer (plus "workspace_id")."""
    meta = sess.get("metadata") or {}
    if meta.get("app") != billing.APP_TAG or meta.get("kind") != "card" \
            or not str(meta.get("workspace_id", "")).isdigit():
        return {"ok": False, "why": "not_ours"}
    sid = int(meta["workspace_id"])
    if ws_id is not None and sid != int(ws_id):
        return {"ok": False, "why": "other_workspace"}
    si = sess.get("setup_intent")
    if isinstance(si, str):
        si = billing._get(f"/setup_intents/{si}", [("expand[]", "payment_method")])
    si = si or {}
    if sess.get("status") != "complete" or si.get("status") != "succeeded":
        return {"ok": False, "why": "declined", "workspace_id": sid}
    pm = si.get("payment_method") or {}
    if isinstance(pm, str):
        pm = billing._get(f"/payment_methods/{pm}")
    card = pm.get("card") or {}
    if email is None:
        uid = str(meta.get("user_id") or "")
        u = conn.execute("SELECT email FROM users WHERE id=? AND workspace_id=?", (int(uid), sid)).fetchone() \
            if uid.isdigit() else None
        u = u or conn.execute("SELECT email FROM users WHERE workspace_id=? ORDER BY role='owner' DESC, id LIMIT 1",
                              (sid,)).fetchone()
        email = u["email"] if u else ""
    customer = sess.get("customer")
    customer = customer.get("id") if isinstance(customer, dict) else customer
    out = record_card(conn, sid, customer=customer, pm_id=pm.get("id"), fingerprint=card.get("fingerprint"),
                      label=card_label(card), email=email)
    out["workspace_id"] = sid
    return out


def webhook(sess: dict) -> dict:
    """checkout.session.completed with mode=setup: the same save as the return page,
    for a member who closed the tab before coming back."""
    meta = sess.get("metadata") or {}
    if meta.get("app") != billing.APP_TAG or meta.get("kind") != "card":
        return {"ignored": "not a campaignify card"}
    try:
        full = billing.get_setup_session(sess["id"]) if isinstance(sess.get("setup_intent"), str) or not sess.get(
            "setup_intent") else sess
    except billing.BillingError as e:
        log.warning("card session %s couldn't be read: %s", sess.get("id"), e)
        return {"pending": "stripe unreachable"}
    with db.connect() as conn:
        out = from_session(conn, full)
    if not out.get("ok"):
        return {"ignored": out.get("why")}
    return {"ok": True, "card": "saved", "credits": out.get("credits", 0)}
