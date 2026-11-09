"""The card step (card_on_file.py, RS_CARD_REQUIRED): the full page, Stripe
Checkout in setup mode and its way back, and the gate that sends a free
workspace without a card to that page before it makes anything new."""
import logging
import uuid

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import accounts
import billing
import card_on_file
import catalog
import db
import verify
from core import is_owner, login_required

log = logging.getLogger("campaignify")
bp = Blueprint("card", __name__)

# What a workspace without a card can still open: its library and downloads,
# Billing (a plan or a pack makes it paying, which needs no card step), email
# confirmation, the Home page's job polling and signing out.
ALLOWED = {"logout", "library", "media", "photo", "api_jobs", "jobs.cards", "jobs.delete",
           "billing_page", "subscribe", "checkout", "billing_portal", "billing_code",
           "verify_email", "verify_resend", "verify_code_send", "verify_code_check",
           "adhub.export_page", "adhub.export_file", "share.create", "share.revoke",
           "admin_view_exit"}
MESSAGE = card_on_file.CardRequired.description


def _wants_json() -> bool:
    return (request.is_json or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/"))


@bp.before_app_request
def gate():
    if g.get("user") is None or g.get("ws") is None or g.get("viewing") is not None:
        return None
    ep = request.endpoint or ""
    if ep == "static" or ep.startswith("card.") or ep in ALLOWED:
        return None
    if not (request.path.startswith("/app") or request.path.startswith("/api/")):
        return None
    if not card_on_file.required():
        if card_on_file.pending(g.ws):        # switched off: credits waiting for a card land now
            with db.connect() as conn:
                card_on_file.release_pending(conn, g.ws["id"])
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        return None
    with db.connect() as conn:
        if not card_on_file.needs_card(conn, g.ws, g.user):
            if card_on_file.pending(g.ws) and not card_on_file.active():
                card_on_file.release_pending(conn, g.ws["id"])
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
            return None
    if _wants_json():
        return jsonify({"error": MESSAGE, "card_required": True, "url": url_for("card.page")}), 402
    return redirect(url_for("card.page"))


@bp.app_template_global("card_required")
def card_required() -> bool:
    """For page copy: the card step is on."""
    return card_on_file.active()


def _new_account(ws) -> bool:
    """Signed up while the card step was on: its starter credits are still waiting."""
    return card_on_file.pending(ws) > 0


@bp.route("/app/card")
@login_required
def page():
    with db.connect() as conn:
        needed = card_on_file.needs_card(conn, g.ws, g.user)
    if not needed:
        return redirect(url_for("studio"))
    n = card_on_file.pending(g.ws)
    r = catalog.rates()
    return render_template("card.html", new=_new_account(g.ws), credits=n, images=n // max(r["image"], 1),
                           owner=is_owner(), canceled=request.args.get("canceled") == "1",
                           declined=request.args.get("declined") == "1"), 200


@bp.route("/app/card/start", methods=["POST"])
@login_required
def start():
    if not is_owner():
        abort(403, "Only the workspace owner can add the card.")
    with db.connect() as conn:
        if not card_on_file.needs_card(conn, g.ws, g.user):
            return redirect(url_for("studio"))
    # Card-testing guard (M-2): per workspace, per network and app-wide, in the shared throttle table.
    ip = request.remote_addr or "unknown"
    key, ip_key = f"card:{g.ws['id']}", f"card-ip:{ip}"
    if db.throttled(key, card_on_file.TRIES_PER_HOUR, 3600) \
            or db.throttled(ip_key, card_on_file.TRIES_PER_IP_HOUR, 3600):
        flash("Too many tries in the last hour. Wait a little and try again.")
        return redirect(url_for("card.page"))
    if db.throttled("card-all", card_on_file.tries_per_hour_all(), 3600):
        log.warning("card step: the app-wide limit of %s checkouts an hour was reached", card_on_file.tries_per_hour_all())
        flash("The card step is busy right now. Try again in a few minutes. Nothing was charged.")
        return redirect(url_for("card.page"))
    if not verify.is_verified(g.user) and accounts.signup_throttled(ip):
        # many new accounts from this network: the card step waits for a confirmed email
        flash("Confirm your email first, then add the card. Open the link we sent you, or ask for a new one from "
              "the banner. Nothing was charged.")
        return redirect(url_for("card.page"))
    for k in (key, ip_key, "card-all"):
        db.hit(k)
    meta = {"app": billing.APP_TAG, "kind": "card", "workspace_id": g.ws["id"]}
    base = request.host_url.rstrip("/")
    try:
        customer = g.ws["card_customer_id"]
        if not customer:
            customer = billing.create_customer(email=g.user["email"], metadata=meta,
                                               idempotency_key=f"campaignify-card-customer-{g.ws['id']}")
            with db.connect() as conn:
                conn.execute("UPDATE workspaces SET card_customer_id=? WHERE id=? AND card_customer_id IS NULL",
                             (customer, g.ws["id"]))
                conn.commit()
        sess = billing.create_setup_session(
            customer=customer,
            success_url=f"{base}{url_for('card.done')}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}{url_for('card.page')}?canceled=1",
            metadata={**meta, "user_id": g.user["id"]}, client_reference_id=f"cfy-card-{g.ws['id']}",
            idempotency_key=f"campaignify-card-{g.ws['id']}-{uuid.uuid4().hex[:12]}")
    except billing.BillingError as e:
        log.warning("card checkout failed for workspace %s: %s", g.ws["id"], e)
        flash("The card page couldn't open. Try again in a minute. Nothing was charged.")
        return redirect(url_for("card.page"))
    return redirect(sess["url"], code=303)


@bp.route("/app/card/done")
@login_required
def done():
    sid = (request.args.get("session_id") or "").strip()[:200]
    if not sid.startswith("cs_"):
        return redirect(url_for("card.page"))
    try:
        sess = billing.get_setup_session(sid)
    except billing.BillingError as e:
        log.warning("card session %s couldn't be read: %s", sid, e)
        flash("We couldn't confirm your card yet. Refresh this page in a moment. Nothing was charged.")
        return redirect(url_for("card.page"))
    with db.connect() as conn:
        out = card_on_file.from_session(conn, sess, ws_id=g.ws["id"], user=g.user)
    if not out.get("ok"):
        if out.get("why") == "declined":
            flash("Your card wasn't saved, so nothing was charged. Try again or use another card.")
            return redirect(url_for("card.page", declined=1))
        abort(404)
    if out.get("credits"):
        flash(f"Card saved. Nothing was charged. Your {out['credits']:,} free credits are ready.", "ok")
    elif out.get("reused"):
        flash("Card saved. Nothing was charged. This card or email already had free credits on another account, "
              "so this one starts without them. Buy a pack or pick a plan on Billing to make ads.", "ok")
    else:
        flash("Card saved. Nothing was charged.", "ok")
    return redirect(url_for("studio"))


@bp.app_errorhandler(card_on_file.CardRequired)
def card_required_error(e):
    if _wants_json():
        return jsonify({"error": e.description, "card_required": True, "url": url_for("card.page")}), 402
    return redirect(url_for("card.page"))
