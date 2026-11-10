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
    Only confirmed emails count (L-2): a card row gets its email_norm when its
    member has confirmed the address (at save, or later in verify.completed),
    and the email match only looks at confirmed rows and confirmed members, so
    signing up with someone else's address can't use up their trial. The
    member lookup is by the indexed users.email_norm (L-3), not a scan.
  * Existing free accounts without a card keep everything they made and their
    credits, and add a card on their next visit before making anything new
    (card_routes.gate: a full-page step; core.hold_check covers the API).
  * Never asked: workspaces on a paid plan, an admin comp or a free-month code,
    workspaces that have paid real money (a paid pack, or a paid plan invoice
    above $0), and site admins. A Stripe customer id alone isn't enough (L-1):
    it is set by any subscription checkout, even one with no card collected (a
    100% promotion code) or whose first invoice never paid.
  * Card-testing guard (M-2): starting a card checkout is throttled per
    workspace, per network and across the whole app (the shared throttle
    table), and needs a confirmed email when the network is past the sign-up
    limit (accounts.signup_throttled).

Switching the flag off releases any credits still waiting for a card.
"""
import logging
import os

from werkzeug.exceptions import HTTPException

import billing
import db
import plans
import preview
import verify

log = logging.getLogger("campaignify")

STARTER_REASON = "starter credits"
TRIES_PER_HOUR = 10          # card checkouts a workspace may start per hour
TRIES_PER_IP_HOUR = 15       # ... a network (one office or household may hold a few accounts)
DEFAULT_TRIES_PER_HOUR_ALL = 300   # ... the whole app (RS_CARD_STARTS_PER_HOUR); a card-testing run hits this


def tries_per_hour_all() -> int:
    """RS_CARD_STARTS_PER_HOUR: card checkouts the whole app may start per hour (default 300)."""
    try:
        return max(1, int(os.environ.get("RS_CARD_STARTS_PER_HOUR") or DEFAULT_TRIES_PER_HOUR_ALL))
    except ValueError:
        return DEFAULT_TRIES_PER_HOUR_ALL


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
    # Paid real money before: a pack, or a plan invoice above $0 (not just a Stripe customer id, L-1).
    if conn.execute("SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' AND amount_cents>0 LIMIT 1",
                    (ws["id"],)).fetchone() is not None:
        return True
    return conn.execute("SELECT 1 FROM plan_invoices WHERE workspace_id=? AND amount_cents>0 LIMIT 1",
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


def terms(pending_credits: int = 0) -> dict:
    """What saving a card means in money, read from the billing config (the card step's timeline, web/v2/card.html).

    A card is saved with Stripe Checkout in setup mode: nothing is charged, no trial runs and no charge date is
    set, so the timeline has no dates. Money moves only when the member picks a plan (plans.PLANS) or buys a
    pack (catalog.PACKS); the cheapest of each is named so the step can say what "later" would cost."""
    import catalog
    paid = sorted((p for p in plans.PLANS.values() if p["usd"] > 0), key=lambda p: p["usd"])
    packs = sorted(catalog.PACKS, key=lambda p: p["usd"])
    image = catalog.price_credits("ad_image", batch=1)
    n = max(0, int(pending_credits or 0))
    return {"today_usd": 0, "charge_date": None, "trial_days": 0, "auto_renews": False,
            "credits": n, "images": n // image if image else 0, "image_credits": image,
            "plan_from": {"name": paid[0]["name"], "usd": paid[0]["usd"], "credits": paid[0]["credits"]} if paid else None,
            "pack_from": {"name": packs[0]["name"], "usd": packs[0]["usd"], "credits": packs[0]["credits"]} if packs else None}


def _had_starter(conn, ws_id: int) -> bool:
    return conn.execute("SELECT 1 FROM ledger WHERE workspace_id=? AND reason LIKE 'starter credits%' AND delta>0 LIMIT 1",
                        (ws_id,)).fetchone() is not None


def confirmed(user) -> bool:
    """Does this member's email count for one-trial-per-person (L-2)? Confirmed, or no confirm step at all."""
    return user is not None and (verify.is_verified(user) or not verify.required())


# A member whose email counts (L-2): confirmed, unless the app has no confirm step (RS_EMAIL_VERIFY=0).
def _confirmed_sql(alias: str) -> str:
    return "1" if not verify.required() else f"{alias}.email_verified_at IS NOT NULL"


def trial_used(conn, ws_id: int, fingerprint: str | None, email: str | None) -> str | None:
    """Why this card or person can't have the starter credits again: "card", "email", or None.
    Email matches count only confirmed addresses (L-2), found by the indexed users.email_norm (L-3)."""
    if fingerprint and conn.execute("SELECT 1 FROM card_trials WHERE fingerprint=? AND workspace_id!=? AND trial=1 "
                                    "LIMIT 1", (fingerprint, ws_id)).fetchone():
        return "card"
    norm = preview.norm_email(email or "")
    if not norm or "@" not in norm:
        return None
    # card rows carry email_norm only once their member confirmed it (record_card, email_confirmed)
    if conn.execute("SELECT 1 FROM card_trials WHERE email_norm=? AND workspace_id!=? AND trial=1 LIMIT 1",
                    (norm, ws_id)).fetchone():
        return "email"
    # an ad-offer grant counts when that workspace's member with this address confirmed it
    if conn.execute("SELECT 1 FROM ad_signup_grants a JOIN users u ON u.workspace_id=a.workspace_id "
                    f"AND u.email_norm=a.email_norm WHERE a.email_norm=? AND a.workspace_id!=? AND {_confirmed_sql('u')} "
                    "LIMIT 1", (norm, ws_id)).fetchone():
        return "email"
    for r in conn.execute(f"SELECT workspace_id FROM users u WHERE email_norm=? AND workspace_id!=? AND {_confirmed_sql('u')}",
                          (norm, ws_id)):
        if _had_starter(conn, r["workspace_id"]):
            return "email"
    return None


def email_confirmed(conn, user) -> int:
    """The member just confirmed their email (verify.completed): put it on the card rows they saved before, so it
    counts for one-trial-per-person from now on, even after the account is deleted (L-2). Commits; returns rows."""
    norm = preview.norm_email(user["email"] or "")
    if "@" not in norm:
        return 0
    n = conn.execute("UPDATE card_trials SET email_norm=? WHERE user_id=? AND email_norm IS NULL",
                     (norm, user["id"])).rowcount
    conn.commit()
    return n


def record_card(conn, ws_id: int, *, customer, pm_id, fingerprint, label, email, user=None) -> dict:
    """The card is saved on Stripe: mark the workspace and grant its waiting
    starter credits unless this card or person already had them (commits).
    Safe to call twice (the return page and the webhook): the second call
    changes nothing. `user` is the member who saved it: the row keeps the
    email only if theirs is confirmed (L-2). Returns {"ok", "already", "credits", "reused"}."""
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
    keep_email = confirmed(user) and preview.norm_email(user["email"] or "") == preview.norm_email(email or "")
    conn.execute("INSERT INTO card_trials (workspace_id, fingerprint, email_norm, trial, credits, created_at, user_id) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (ws_id, fingerprint or None, (preview.norm_email(email or "") or None) if keep_email else None,
                  1 if (had or granted) else 0, granted, db.now(), user["id"] if user is not None else None))
    conn.commit()
    if reused:
        log.info("card saved on workspace %s without starter credits (%s already had a trial)", ws_id, reused)
    return {"ok": True, "already": False, "credits": granted, "reused": reused}


def card_label(card: dict) -> str:
    brand = (card.get("display_brand") or card.get("brand") or "card").replace("_", " ").title()
    return f"{brand} ending {card.get('last4') or '????'}"


def from_session(conn, sess: dict, ws_id: int | None = None, user=None) -> dict:
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
    if user is not None:   # the return page: the member who's logged in, read fresh (confirmed or not)
        user = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?", (user["id"], sid)).fetchone()
    if user is None:
        uid = str(meta.get("user_id") or "")
        user = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?", (int(uid), sid)).fetchone() \
            if uid.isdigit() else None
        user = user or conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY role='owner' DESC, id LIMIT 1",
                                    (sid,)).fetchone()
    email = user["email"] if user is not None else ""
    customer = sess.get("customer")
    customer = customer.get("id") if isinstance(customer, dict) else customer
    out = record_card(conn, sid, customer=customer, pm_id=pm.get("id"), fingerprint=card.get("fingerprint"),
                      label=card_label(card), email=email, user=user)
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
