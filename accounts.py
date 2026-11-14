"""Self-serve account creation, shared by the sign-up form (app.py) and
"Continue with Google" (google_auth.py). Blueprints import from here, never from app.py.

A new account gets its own workspace and the starter credits (or the ad offer,
adoffer.py), or joins a team through an invite link. While RS_CARD_REQUIRED is on
the credits wait for the card step (card_on_file.py), which every new workspace
goes through after sign-up. Owner setup links
(owners.py) are handled by the sign-up form itself and never come through here."""
import logging

from flask import redirect, session, url_for

import adoffer
import card_on_file
import catalog
import compcodes
import conversions
import db
import gads
import login_security
import plans
import site_events
import verify

log = logging.getLogger("rs.accounts")

SIGNUPS_PER_HOUR, SIGNUPS_PER_DAY = 5, 10


class SignupRefused(Exception):
    """Why the account can't be made (shown to the person as is). `status` is the HTTP status for the form."""

    def __init__(self, message: str, status: int = 200):
        super().__init__(message)
        self.status = status


class EmailTaken(SignupRefused):
    """The email already has an account (the sign-up form mails that inbox instead of saying so, login_security.py)."""


def signup_throttled(ip: str) -> bool:
    """Too many new accounts from this network (per hour or per day)."""
    return db.throttled(f"signup:{ip}", SIGNUPS_PER_HOUR, 3600) or db.throttled(f"signup:{ip}", SIGNUPS_PER_DAY, 86400)


def create_self_serve_account(conn, email: str, name: str, pw_hash: str, req, ip: str, invite_code: str = "",
                              auth_provider=None, verified: bool = False, pending: bool = False) -> dict:
    """Insert the user (and their workspace, unless an invite joins a team), grant
    the starter or ad-offer credits and use up the invite. Nothing is committed here.

    `verified` marks the email confirmed right away (Google told us so); otherwise
    the account gets a verify nonce and confirms by email (verify.py). `pending`
    (the sign-up form, login_security.confirm_first) keeps it from logging in until
    the emailed link is opened. The invite is checked before the email, so a bad
    invite says the same thing whether or not the email has an account (SEC-8).
    Returns {"uid", "ws_id", "ad_credits", "joins", "invited"}. Raises SignupRefused."""
    email = email.strip().lower()
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
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise EmailTaken("That email already has an account. Log in instead.")
    if not (inv and inv["workspace_id"]):
        ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                             ((name or email.split("@")[0]) + "'s studio", 0, db.now())).lastrowid
        import plan_gate
        if plan_gate.on():
            # PLANS_ONLY: no credits at sign-up. Confirming the email brings the welcome credits
            # (plan_gate.grant_welcome); after those, a plan (its trial brings its own).
            grant = (0, "")
        else:
            # Ad-sourced sign-ups get SIGNUP_CREDITS_ADS instead, once per person (adoffer.py).
            ad_credits = adoffer.claim(conn, ws_id, email, ip, gads.parse_cookie(req.cookies.get(gads.COOKIE)),
                                       bool(req.cookies.get(adoffer.BROWSER_COOKIE)), db.now())
            grant = (ad_credits, adoffer.REASON) if ad_credits else (catalog.starter_credits(), "starter credits")
        if grant[0] and card_on_file.active():
            card_on_file.defer(conn, ws_id, *grant)    # they land when the card is saved (card_on_file.py)
        elif grant[0]:
            db.adjust_credits(conn, ws_id, *grant)
    joins = bool(inv and inv["workspace_id"])
    # Everyone confirms by email before the free credits work (verify.py), unless the
    # sign-in provider already confirmed the address.
    cur = conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at, role, "
                       "email_verified_at, verify_nonce, first_touch, auth_provider, signup_pending, email_norm) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (email, name, pw_hash, 0, ws_id, db.now(), "member" if joins else "owner",
                        db.now() if verified else None, None if verified else verify.new_nonce(),
                        gads.first_touch_json(req.cookies.get(gads.COOKIE)), auth_provider,
                        1 if pending and not verified else 0, db.norm_email(email)))
    if inv:
        conn.execute("UPDATE invites SET used_by=? WHERE code=?", (cur.lastrowid, invite_code))
    if verified and not joins:
        # Google already confirmed the address: the welcome credits now, as confirming by email gives them.
        import plan_gate
        plan_gate.grant_welcome(conn, conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone(),
                                conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
    return {"uid": cur.lastrowid, "ws_id": ws_id, "ad_credits": ad_credits, "joins": joins, "invited": inv is not None}


def _check_comp_code(conn, comp_code: str):
    """Would this free-month code work on a new workspace? Tried on a throwaway one
    and rolled back, so an email that already has an account gets the same answer
    about a bad code as a new one (SEC-8). Raises SignupRefused."""
    ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                         ("code check", 0, db.now())).lastrowid
    try:
        compcodes.redeem(conn, ws_id, None, comp_code, commit=False)
    except compcodes.Refused as e:
        raise SignupRefused(str(e)) from None
    finally:
        conn.rollback()


def self_serve_signup(email: str, name: str, pw_hash: str, req, ip: str, secret: str, invite_code: str = "",
                      auth_provider=None, verified: bool = False, dest: str = None, comp_code: str = "",
                      confirm_first: bool = False):
    """The whole self-serve sign-up: create the account, log it in, send the
    confirmation email when it needs one, record the funnel step and the
    conversions, and answer with the redirect (to `dest` or Home) and its cookies.
    A free-month code (`comp_code`, compcodes.py) is used in the same transaction:
    a code that doesn't work refuses the sign-up with its reason.

    `confirm_first` (the sign-up form while login_security.confirm_first() is on):
    nobody is logged in. The account waits (signup_pending) and its inbox gets the
    confirmation link; the answer is the same "Check your email" page an email that
    already has an account gets, with no cookie set, and the first login happens
    when the link is opened (first_login). Raises EmailTaken for a taken email
    (after checking the code, so a bad code reads the same either way).
    Raises SignupRefused when the account can't be made (nothing is written then)."""
    comp_row = None
    with db.connect() as conn:
        try:
            made = create_self_serve_account(conn, email, name, pw_hash, req, ip, invite_code,
                                             auth_provider=auth_provider, verified=verified,
                                             pending=confirm_first)
        except EmailTaken:
            conn.rollback()
            if confirm_first and comp_code:
                _check_comp_code(conn, comp_code)
            raise
        if comp_code:
            try:
                comp_row = compcodes.redeem(conn, made["ws_id"], made["uid"], comp_code, commit=False)
            except compcodes.Refused as e:
                conn.rollback()
                raise SignupRefused(str(e)) from None
        conn.commit()
        uid, ws_id, ad_credits = made["uid"], made["ws_id"], made["ad_credits"]
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if confirm_first:
            db.hit(f"verify:{uid}")
            login_security.later(_send_first_link, uid, secret)
        else:
            login_security.start_session(u)
            ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if verify.blocked(conn, ws_row, u):
                db.hit(f"verify:{uid}")
                verify.send_link(conn, secret, u)
        if not made["joins"]:
            # the funnel's sign-up step (site_events.py), with this visitor's first touch
            site_events.on_signup(conn, ws_id, uid, req,
                                  detail="invite" if made["invited"] else "ad_offer" if ad_credits else
                                  "plans_only" if _plans_only() else "starter")
    db.hit(f"signup:{ip}")
    # the server-side sign-up copy for Meta / TikTok / Reddit (same event id as the tag), when a token is set
    try:
        with db.connect() as conn:
            conversions.on_signup(conn, uid, conversions.context(req))
    except Exception:  # noqa: BLE001  measurement never breaks sign-up
        log.exception("server-side sign-up conversion not queued")
    if confirm_first:
        return login_security.check_email_page(email)
    # Google Ads sign_up conversion, fired on the first page after this.
    gads.queue(session, "sign_up", transaction_id=f"signup-{uid}")
    if comp_row is not None:
        from flask import flash
        flash(compcodes.applied_message(comp_row), "ok")
    # New accounts land on Home, which is the first-ad form until they've made one
    # (the setup guide is a link from there, not a detour).
    return _landing(redirect(dest or url_for("studio")), req, ad_credits)


def _plans_only() -> bool:
    import plan_gate
    return plan_gate.on()


def _landing(resp, req, ad_credits):
    if req.cookies.get(gads.COOKIE):
        resp.delete_cookie(gads.COOKIE)   # it's on the account now
    if ad_credits:   # this browser has had the ad offer (adoffer.py)
        resp.set_cookie(adoffer.BROWSER_COOKIE, "1", max_age=adoffer.BROWSER_COOKIE_DAYS * 86400, httponly=True,
                        samesite="Lax", secure=req.is_secure)
    return resp


def _send_first_link(uid: int, secret: str):
    """The confirmation email of a form sign-up (off the request thread)."""
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if u is not None and not verify.is_verified(u):
            verify.send_link(conn, secret, u, first_login=True)


def first_login(conn, u, req):
    """The emailed link of a form sign-up was opened (app.verify_email): log that
    account in for the first time, with what sign-up used to do at this point (the
    Google Ads sign_up conversion, the free-month code note, the ad-offer cookie).
    Returns the redirect to Home."""
    conn.execute("UPDATE users SET signup_pending=0 WHERE id=?", (u["id"],))
    conn.commit()
    u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    login_security.start_session(u)
    gads.queue(session, "sign_up", transaction_id=f"signup-{u['id']}")
    comp = conn.execute("SELECT * FROM comp_codes WHERE workspace_id=? AND redeemed_by=?",
                        (u["workspace_id"], u["id"])).fetchone()
    from flask import flash
    flash(compcodes.applied_message(comp) if comp is not None else "Email confirmed. You're logged in.", "ok")
    ad_credits = conn.execute("SELECT 1 FROM ledger WHERE workspace_id=? AND reason=? LIMIT 1",
                              (u["workspace_id"], adoffer.REASON)).fetchone() is not None
    return _landing(redirect(url_for("studio")), req, ad_credits)
