"""Self-serve account creation, shared by the sign-up form (app.py) and
"Continue with Google" (google_auth.py). Blueprints import from here, never from app.py.

A new account gets its own workspace and the starter credits (or the ad offer,
adoffer.py), or joins a team through an invite link. Owner setup links
(owners.py) are handled by the sign-up form itself and never come through here."""
import logging

from flask import redirect, session, url_for

import adoffer
import catalog
import compcodes
import conversions
import db
import gads
import plans
import site_events
import verify
from core import csrf_token

log = logging.getLogger("rs.accounts")

SIGNUPS_PER_HOUR, SIGNUPS_PER_DAY = 5, 10


class SignupRefused(Exception):
    """Why the account can't be made (shown to the person as is). `status` is the HTTP status for the form."""

    def __init__(self, message: str, status: int = 200):
        super().__init__(message)
        self.status = status


def signup_throttled(ip: str) -> bool:
    """Too many new accounts from this network (per hour or per day)."""
    return db.throttled(f"signup:{ip}", SIGNUPS_PER_HOUR, 3600) or db.throttled(f"signup:{ip}", SIGNUPS_PER_DAY, 86400)


def create_self_serve_account(conn, email: str, name: str, pw_hash: str, req, ip: str, invite_code: str = "",
                              auth_provider=None, verified: bool = False) -> dict:
    """Insert the user (and their workspace, unless an invite joins a team), grant
    the starter or ad-offer credits and use up the invite. Nothing is committed here.

    `verified` marks the email confirmed right away (Google told us so); otherwise
    the account gets a verify nonce and confirms by email (verify.py).
    Returns {"uid", "ws_id", "ad_credits", "joins", "invited"}. Raises SignupRefused."""
    email = email.strip().lower()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise SignupRefused("That email already has an account. Log in instead.")
    inv = None
    ad_credits = 0
    if invite_code:
        inv = conn.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL", (invite_code,)).fetchone()
        if not inv:
            raise SignupRefused("That invite link has expired or was already used. Clear the invite code "
                                "to create your own free account instead.")
    if inv and inv["workspace_id"]:
        ws_id = inv["workspace_id"]
        target = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        maker = conn.execute("SELECT is_admin FROM users WHERE id=?", (inv["created_by"],)).fetchone() \
            if inv["created_by"] else None
        members = conn.execute("SELECT COUNT(*) FROM users WHERE workspace_id=?", (ws_id,)).fetchone()[0]
        # Team invites count against the plan's seats; operator (site admin) invites don't.
        if inv["created_by"] and not (maker and maker["is_admin"]) and not plans.within(target, "seats", members):
            raise SignupRefused("That team is full on its current plan. Ask the owner to upgrade or free a seat.")
    else:
        ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                             ((name or email.split("@")[0]) + "'s studio", 0, db.now())).lastrowid
        # Ad-sourced sign-ups get SIGNUP_CREDITS_ADS instead, once per person (adoffer.py).
        ad_credits = adoffer.claim(conn, ws_id, email, ip, gads.parse_cookie(req.cookies.get(gads.COOKIE)),
                                   bool(req.cookies.get(adoffer.BROWSER_COOKIE)), db.now())
        if ad_credits:
            db.adjust_credits(conn, ws_id, ad_credits, adoffer.REASON)
        elif catalog.starter_credits():
            db.adjust_credits(conn, ws_id, catalog.starter_credits(), "starter credits")
    joins = bool(inv and inv["workspace_id"])
    # Everyone confirms by email before the free credits work (verify.py), unless the
    # sign-in provider already confirmed the address.
    cur = conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at, role, "
                       "email_verified_at, verify_nonce, first_touch, auth_provider) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (email, name, pw_hash, 0, ws_id, db.now(), "member" if joins else "owner",
                        db.now() if verified else None, None if verified else verify.new_nonce(),
                        gads.first_touch_json(req.cookies.get(gads.COOKIE)), auth_provider))
    if inv:
        conn.execute("UPDATE invites SET used_by=? WHERE code=?", (cur.lastrowid, invite_code))
    return {"uid": cur.lastrowid, "ws_id": ws_id, "ad_credits": ad_credits, "joins": joins, "invited": inv is not None}


def self_serve_signup(email: str, name: str, pw_hash: str, req, ip: str, secret: str, invite_code: str = "",
                      auth_provider=None, verified: bool = False, dest: str = None, comp_code: str = ""):
    """The whole self-serve sign-up: create the account, log it in, send the
    confirmation email when it needs one, record the funnel step and the
    conversions, and answer with the redirect (to `dest` or Home) and its cookies.
    A free-month code (`comp_code`, compcodes.py) is used in the same transaction:
    a code that doesn't work refuses the sign-up with its reason.
    Raises SignupRefused when the account can't be made (nothing is written then)."""
    comp_row = None
    with db.connect() as conn:
        made = create_self_serve_account(conn, email, name, pw_hash, req, ip, invite_code,
                                         auth_provider=auth_provider, verified=verified)
        if comp_code:
            try:
                comp_row = compcodes.redeem(conn, made["ws_id"], made["uid"], comp_code, commit=False)
            except compcodes.Refused as e:
                conn.rollback()
                raise SignupRefused(str(e)) from None
        conn.commit()
        uid, ws_id, ad_credits = made["uid"], made["ws_id"], made["ad_credits"]
        session.clear()
        session["uid"] = uid
        csrf_token()
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if verify.blocked(conn, ws_row, u):
            db.hit(f"verify:{uid}")
            verify.send_link(conn, secret, u)
        if not made["joins"]:
            # the funnel's sign-up step (site_events.py), with this visitor's first touch
            site_events.on_signup(conn, ws_id, uid, req,
                                  detail="invite" if made["invited"] else "ad_offer" if ad_credits else "starter")
    db.hit(f"signup:{ip}")
    # Google Ads sign_up conversion, fired on the first page after this.
    gads.queue(session, "sign_up", transaction_id=f"signup-{uid}")
    # and the server-side copy for Meta / TikTok / Reddit (same event id), when a token is set
    try:
        with db.connect() as conn:
            conversions.on_signup(conn, uid, conversions.context(req))
    except Exception:  # noqa: BLE001  measurement never breaks sign-up
        log.exception("server-side sign-up conversion not queued")
    if comp_row is not None:
        from flask import flash
        flash(compcodes.applied_message(comp_row), "ok")
    # New accounts land on Home, which is the first-ad form until they've made one
    # (the setup guide is a link from there, not a detour).
    resp = redirect(dest or url_for("studio"))
    if req.cookies.get(gads.COOKIE):
        resp.delete_cookie(gads.COOKIE)   # it's on the account now
    if ad_credits:   # this browser has had the ad offer (adoffer.py)
        resp.set_cookie(adoffer.BROWSER_COOKIE, "1", max_age=adoffer.BROWSER_COOKIE_DAYS * 86400, httponly=True,
                        samesite="Lax", secure=req.is_secure)
    return resp
