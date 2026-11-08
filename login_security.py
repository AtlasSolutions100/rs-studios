"""Login hardening (roadmap M1.8: SEC-5, SEC-8, REL-9, BILL-6).

  * Login throttle in the shared `throttle` table (every worker sees it): failed
    passwords per network + email, per network, and per account from any network.
    The account limit keys on the typed email, so it says nothing about whether
    an account exists, and "Forgot password?" still works while it is on.
  * The per-account limit is a slowdown, not a lock (N9): past FAILS_PER_ACCOUNT,
    a browser that hasn't logged in to that account before gets one password try
    every ACCOUNT_SLOW_S for it, and the owner gets one email about it an hour. A
    browser that has logged in to it before (the signed KNOWN_COOKIE) isn't slowed,
    so nobody can lock someone out by guessing wrong on purpose.
  * One message for a failed login, whatever the account is (no "this account
    signs in with Google").
  * Sign-up answers the same whether or not the email has an account (SEC-8):
    the "Check your email" page, no cookie, nobody logged in. A new account waits
    for the link in that email (users.signup_pending) before its first login; an
    email that already has an account gets "You already have an account" instead.
    A second sign-up for an account still waiting replaces its password and sends
    a new link, so the newest sign-up is the one the inbox confirms.
    RS_SIGNUP_CONFIRM=0 goes back to logging a new sign-up straight in.
  * Sessions carry users.session_version; a password reset bumps it, so every
    other browser logged in to that account is logged out (N8).
  * Password reset: /forgot mails a link with a random token (only its SHA-256 is
    kept, in `login_tokens`), good for RESET_MINUTES and once. Opening it moves the
    token into the session and off the address bar (no token in page analytics
    or referrers); the new password is set on POST. A token left in the session
    is dropped after SESSION_TOKEN_MINUTES (N8).
  * Nudge-email login links (nudges.py, /m): LINK_HOURS_HARDENED (24) and single
    use. Opening one shows a "Log in" button and the token is spent on POST, so
    a mail scanner that fetches links doesn't use it up.
  * Code throttles (BILL-6): free-month and promotion codes on Billing, and
    free-month codes at sign-up.

Mail goes through mailer.send with the account sender (verify.from_address,
verify.reply_to), sent off the request thread so a known address doesn't
answer slower than an unknown one.

RS_LOGIN_HARDENING=0 turns back the parts people could notice: the old login
and sign-up messages, sign-up logging straight in, the reusable 72-hour nudge
links and the other sessions staying logged in after a reset. The throttles and
password reset stay on either way.
"""
import hashlib
import logging
import os
import secrets
import threading
import time

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

import db
import mailer
import verify

log = logging.getLogger("campaignify")

bp = Blueprint("logins", __name__)

# failed password logins in WINDOW_S
FAILS_PER_IP_EMAIL, FAILS_PER_IP, FAILS_PER_ACCOUNT = 8, 30, 20
WINDOW_S = 900
ACCOUNT_SLOW_S = 30                 # past FAILS_PER_ACCOUNT: one try per this many seconds from an unknown browser
NOTICES_PER_ACCOUNT_HOUR = 1        # "many failed logins" emails per account per hour
KNOWN_COOKIE, KNOWN_DAYS, KNOWN_MAX = "cfy_kd", 180, 5   # browsers that have logged in to an account
SESSION_TOKEN_MINUTES = 15          # a reset or login-link token waits in the session at most this long
RESET_MINUTES = 60
RESETS_PER_EMAIL_HOUR, RESET_GAP_S, RESETS_PER_IP_HOUR = 3, 60, 10
TAKEN_MAILS_PER_EMAIL_HOUR = 2      # "you already have an account" emails per address per hour
CODES_PER_HOUR = 10                 # code tries per workspace (Billing) or per network (sign-up)
LINK_HOURS_HARDENED, LINK_HOURS_OLD = 24, 72
MIN_PW, MAX_PW = 8, 200

BLOCKED_MSG = "Too many tries. Wait 15 minutes, or reset your password."
SLOW_MSG = f"Too many tries on this account. Wait {ACCOUNT_SLOW_S} seconds and try again, or reset your password."
CODES_MSG = "Too many codes tried. Try again in an hour."


def enabled() -> bool:
    return os.environ.get("RS_LOGIN_HARDENING", "1") != "0"


def confirm_first() -> bool:
    """A form sign-up waits for its emailed link before the first login (SEC-8)."""
    return enabled() and os.environ.get("RS_SIGNUP_CONFIRM", "1").strip().lower() not in ("0", "off", "false", "no")


def awaiting_confirm(user) -> bool:
    """A form sign-up whose emailed link hasn't been opened yet: no password login."""
    return bool(user is not None and user["signup_pending"] and not user["email_verified_at"])


def link_hours() -> int:
    """How long a nudge-email login link works."""
    return LINK_HOURS_HARDENED if enabled() else LINK_HOURS_OLD


def _norm(email) -> str:
    return (email or "").strip().lower()[:200]


def _hash(token: str) -> str:
    return hashlib.sha256(("cfy-login-token:" + (token or "")).encode()).hexdigest()


def later(fn, *args):
    """Run fn off the request thread (logged, never raises). Tests set INLINE."""
    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("login email failed")
    if INLINE:
        run()
        return
    t = threading.Thread(target=run, name="rs-login-mail", daemon=True)
    t.start()
    _pending.append(t)
    del _pending[:-50]


_later = later
INLINE = False
_pending: list = []


def drain(timeout: float = 10):
    """Wait for mail threads (tests)."""
    for t in list(_pending):
        t.join(timeout)


# ---------------------------------------------------------------- login throttle and messages

def login_blocked(ip: str, email: str, known: bool = False) -> bool:
    return login_wait(ip, email, known) is not None


def login_wait(ip: str, email: str, known: bool = False):
    """Why this password try can't go ahead now (the message), or None. Per network
    + email and per network: a hard limit for 15 minutes. Per account, from any
    network: a slowdown (one try per ACCOUNT_SLOW_S from a browser that hasn't
    logged in to it before; `known` browsers aren't slowed), never a lock (N9).
    Keyed on the typed email, so it says nothing about whether an account exists."""
    email = _norm(email)
    if db.throttled(f"login:{ip}:{email}", FAILS_PER_IP_EMAIL, WINDOW_S) or db.throttled(f"login:{ip}", FAILS_PER_IP, WINDOW_S):
        return BLOCKED_MSG
    if known or not db.throttled(f"login-acct:{email}", FAILS_PER_ACCOUNT, WINDOW_S):
        return None
    if db.throttled(f"login-slow:{email}", 1, ACCOUNT_SLOW_S):
        return SLOW_MSG
    db.hit(f"login-slow:{email}")
    return None


def login_failed(ip: str, email: str):
    email = _norm(email)
    db.hit(f"login:{ip}:{email}")
    db.hit(f"login:{ip}")
    db.hit(f"login-acct:{email}")
    # Past the account limit, tell the owner once an hour (nothing is sent for an email with no account).
    if db.throttled(f"login-acct:{email}", FAILS_PER_ACCOUNT, WINDOW_S) \
            and not db.throttled(f"login-notice:{email}", NOTICES_PER_ACCOUNT_HOUR, 3600):
        db.hit(f"login-notice:{email}")
        later(_send_fails_notice, email)


def _send_fails_notice(email: str):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u is None:
        return
    base = verify.public_url()
    text = (f"Hi,\n\nThere have been many wrong passwords typed for your Campaignify account in the last "
            f"{WINDOW_S // 60} minutes. New browsers now get one try every {ACCOUNT_SLOW_S} seconds. Browsers you've "
            "logged in with before work as usual.\n\nIf this was you, you can set a new password here:\n\n"
            f"{base}/forgot\n\nIf it wasn't you, your password still keeps them out. Setting a new one also logs out "
            "every other browser.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            "<p>Hi,</p><p>There have been many wrong passwords typed for your Campaignify account in the last "
            f"{WINDOW_S // 60} minutes. New browsers now get one try every {ACCOUNT_SLOW_S} seconds. Browsers you've "
            "logged in with before work as usual.</p>"
            f'<p>If this was you, <a href="{base}/forgot">set a new password</a>.</p>'
            '<p style="font-size:13px;color:#555">If it wasn\'t you, your password still keeps them out. '
            "Setting a new one also logs out every other browser.</p></div>")
    _mail(u, "Many failed logins on your Campaignify account", html, text, "login_fails")


# ---------------------------------------------------------------- browsers that have logged in (N9)

def _known_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="cfy-known-browser")


def _known_tag(email: str) -> str:
    return hashlib.sha256(("cfy-known:" + _norm(email)).encode()).hexdigest()[:20]


def _known_tags(req) -> list:
    raw = req.cookies.get(KNOWN_COOKIE)
    if not raw:
        return []
    try:
        tags = _known_serializer().loads(raw, max_age=KNOWN_DAYS * 86400)
    except BadSignature:
        return []
    return [t for t in tags if isinstance(t, str)][:KNOWN_MAX] if isinstance(tags, list) else []


def known_browser(req, email: str) -> bool:
    """Has this browser logged in to this account before (signed cookie, KNOWN_DAYS)?"""
    return _known_tag(email) in _known_tags(req)


def remember_browser(resp, req, email: str):
    """After a login: this browser isn't slowed for this account (login_wait)."""
    tag = _known_tag(email)
    tags = [tag] + [t for t in _known_tags(req) if t != tag]
    resp.set_cookie(KNOWN_COOKIE, _known_serializer().dumps(tags[:KNOWN_MAX]), max_age=KNOWN_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=req.is_secure)
    return resp


# ---------------------------------------------------------------- sessions (N8)

def start_session(user):
    """Log this account in on this browser: a fresh session with the account's
    session_version (a password reset bumps it and ends the others)."""
    from core import csrf_token
    session.clear()
    session["uid"] = user["id"]
    session["sv"] = int(user["session_version"] or 0)
    csrf_token()


def session_current(user) -> bool:
    """Does this browser's session still count for this account? A session from
    before session_version existed carries none and counts as version 0."""
    if not enabled():
        return True
    return int(session.get("sv") or 0) == int(user["session_version"] or 0)


def _stash(key: str, token: str):
    session[key] = token
    session[key + "_at"] = int(time.time())


def _stashed(key: str) -> str:
    """The reset or login-link token waiting in the session, if it's recent enough."""
    at = session.get(key + "_at")
    if session.get(key) and at is not None and time.time() - float(at) <= SESSION_TOKEN_MINUTES * 60:
        return session.get(key)
    session.pop(key, None)
    session.pop(key + "_at", None)
    return ""


@bp.before_app_request
def _drop_abandoned_tokens():
    """A reset or login link opened and left without pressing the button: forget
    its token after SESSION_TOKEN_MINUTES, so the next person on this browser
    can't finish it (N8)."""
    for key in ("reset_t", "magic_t"):
        if key in session:
            _stashed(key)


def failed_message(user) -> str:
    """What a failed password login says. Hardened: the same words for every
    email (a Google hint for everyone when the Google button is on)."""
    if not enabled():
        if user is not None and user["auth_provider"] == "google":
            return "This account signs in with Google. Use the Google button."
        return "That email and password don't match."
    import google_auth
    if google_auth.configured():
        return "That email and password don't match. If you signed up with Google, use the Google button."
    return "That email and password don't match."


def code_attempt(key: str) -> bool:
    """Record a code try for `key`; False when it is over CODES_PER_HOUR (nothing recorded then)."""
    if db.throttled(key, CODES_PER_HOUR, 3600):
        return False
    db.hit(key)
    return True


# ---------------------------------------------------------------- sign-up with an email that has an account

def check_email_page(email: str):
    """The sign-up form's answer for a new email and a taken one alike (SEC-8)."""
    return render_template("check_email.html", email=_norm(email), what="signup")


def signup_email_taken(email: str, ip: str, pw_hash: str = None, secret: str = None):
    """The sign-up form's answer when the email already has an account: the same
    "check your email" page anyone would get, and a note to that inbox (or, for
    an account still waiting for its link, a new link with this sign-up's password)."""
    email = _norm(email)
    db.hit(f"signup:{ip}")   # counts against the sign-up limit, so the form can't be used to test addresses
    if not db.throttled(f"signup-taken:{email}", TAKEN_MAILS_PER_EMAIL_HOUR, 3600):
        db.hit(f"signup-taken:{email}")
        later(_send_taken, email, pw_hash, secret or current_app.secret_key)
    return check_email_page(email)


def _send_taken(email: str, pw_hash: str = None, secret: str = None):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u is None:
            return
        if awaiting_confirm(u) and pw_hash and secret:
            # Signed up again before opening the first link: this sign-up's password, a
            # new link, and the old link stops working (its nonce is gone).
            cur = conn.execute("UPDATE users SET pw_hash=?, verify_nonce=? WHERE id=? AND signup_pending=1 "
                               "AND email_verified_at IS NULL", (pw_hash, verify.new_nonce(), u["id"]))
            conn.commit()
            if cur.rowcount == 1:
                u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
                verify.send_link(conn, secret, u, first_login=True)
                return
    base = verify.public_url()
    google = u["auth_provider"] == "google"
    how = ("You made it with Google, so use Continue with Google on the log in page." if google else
           "Log in with your email and password.")
    text = (f"Hi,\n\nSomeone, maybe you, tried to make a new Campaignify account with this email. "
            f"You already have one, so nothing new was made.\n\n{how}\n\n{base}/login\n\n"
            f"Forgot your password? Set a new one here:\n\n{base}/forgot\n\n"
            "If this wasn't you, you can ignore this email. Your account hasn't changed.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            "<p>Hi,</p><p>Someone, maybe you, tried to make a new Campaignify account with this email. "
            "You already have one, so nothing new was made.</p>"
            f"<p>{how}</p>"
            f'<p><a href="{base}/login" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Log in</a></p>'
            f'<p>Forgot your password? <a href="{base}/forgot">Set a new one</a>.</p>'
            '<p style="font-size:13px;color:#555">If this wasn\'t you, you can ignore this email. '
            "Your account hasn't changed.</p></div>")
    _mail(u, "You already have a Campaignify account", html, text, "account_exists")


def _mail(user, subject, html, text, kind) -> bool:
    try:
        mailer.send(frm=verify.from_address(), to=user["email"], subject=subject, html=html, text=text,
                    reply_to=verify.reply_to(), tags={"kind": kind})
        return True
    except mailer.MailError as e:
        log.warning("%s email to user %s not sent: %s", kind, user["id"], e)
        return False


# ---------------------------------------------------------------- password reset tokens

def issue_reset(conn, user) -> str:
    """A new reset token for this account (commits). Returns the raw token."""
    token = secrets.token_urlsafe(32)
    t = int(time.time())
    conn.execute("DELETE FROM login_tokens WHERE expires_at<?", (t - 7 * 86400,))
    conn.execute("INSERT INTO login_tokens (token_hash, kind, user_id, email, expires_at, used_at, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (_hash(token), "reset", user["id"], user["email"],
                                            t + RESET_MINUTES * 60, None, db.now()))
    conn.commit()
    return token


def reset_user(conn, token: str):
    """The account a reset token is for, or None (unknown, used, expired, email changed)."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM login_tokens WHERE token_hash=? AND kind='reset'", (_hash(token),)).fetchone()
    if row is None or row["used_at"] or row["expires_at"] <= time.time():
        return None
    u = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if u is None or u["email"] != row["email"]:
        return None
    return u


def use_reset(conn, token: str, new_password: str):
    """Spend the token and set the password (commits). Returns the user, or None
    when the token can't be used (someone else spent it first included)."""
    u = reset_user(conn, token)
    if u is None:
        return None
    cur = conn.execute("UPDATE login_tokens SET used_at=? WHERE token_hash=? AND kind='reset' AND used_at IS NULL "
                       "AND expires_at>?", (db.now(), _hash(token), int(time.time())))
    if cur.rowcount != 1:
        conn.rollback()
        return None
    # Every other reset link for this account stops working too.
    conn.execute("UPDATE login_tokens SET used_at=? WHERE user_id=? AND kind='reset' AND used_at IS NULL",
                 (db.now(), u["id"]))
    # The new password ends every other session (N8) and finishes a sign-up still waiting for its link.
    conn.execute("UPDATE users SET pw_hash=?, session_version=session_version+1, signup_pending=0 WHERE id=?",
                 (generate_password_hash(new_password), u["id"]))
    was_verified = verify.is_verified(u)
    if not was_verified:   # the link reached that inbox, as /verify and /m count it
        conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, "
                     "verify_code_exp=NULL, verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL",
                     (db.now(), u["id"]))
    conn.execute("DELETE FROM throttle WHERE key IN (?, ?)", (f"login-acct:{u['email']}", f"login-slow:{u['email']}"))
    conn.commit()
    if not was_verified:
        verify.completed(conn, u, "/reset")
    return conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()


def _send_reset(email: str):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u is None:
            return
        token = issue_reset(conn, u)
    link = verify.public_url() + "/reset?t=" + token
    text = (f"Hi,\n\nSomeone asked to reset the password for your Campaignify account. "
            f"Set a new one with this link. It works once, for {RESET_MINUTES} minutes:\n\n{link}\n\n"
            "If you didn't ask, ignore this email. Your password stays the same.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            "<p>Hi,</p><p>Someone asked to reset the password for your Campaignify account.</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Set a new password</a></p>'
            f'<p style="font-size:13px;color:#555">The link works once, for {RESET_MINUTES} minutes. '
            f"Or paste it into your browser:<br>{link}</p>"
            '<p style="font-size:13px;color:#555">If you didn\'t ask, ignore this email. Your password stays the same.</p></div>')
    _mail(u, "Reset your Campaignify password", html, text, "password_reset")


# ---------------------------------------------------------------- nudge login links (/m)

def spend_link(conn, token: str, user, max_age_s: int) -> bool:
    """Use a nudge login link once (commits). False when it was used before."""
    t = int(time.time())
    conn.execute("DELETE FROM login_tokens WHERE expires_at<?", (t - 7 * 86400,))
    cur = conn.execute("INSERT OR IGNORE INTO login_tokens (token_hash, kind, user_id, email, expires_at, used_at, "
                       "created_at) VALUES (?,?,?,?,?,?,?)",
                       (_hash(token), "magic", user["id"], user["email"], t + max_age_s, db.now(), db.now()))
    conn.commit()
    return cur.rowcount == 1


def link_used(conn, token: str) -> bool:
    return conn.execute("SELECT 1 FROM login_tokens WHERE token_hash=? AND kind='magic'",
                        (_hash(token),)).fetchone() is not None


# ---------------------------------------------------------------- routes

def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _log_in(user):
    start_session(user)


@bp.after_request
def _noindex(resp):
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Ask for a reset link. The answer is the same whether or not the email has an account."""
    if request.method == "POST":
        email = _norm(request.form.get("email"))
        ip = _client_ip()
        if "@" not in email:
            flash("Enter the email you log in with.")
            return render_template("forgot.html", email=email), 400
        if db.throttled(f"reset-ip:{ip}", RESETS_PER_IP_HOUR, 3600):
            flash("Too many reset requests from your network. Try again in an hour.")
            return render_template("forgot.html", email=email), 429
        db.hit(f"reset-ip:{ip}")
        key = f"reset:{email}"
        if not db.throttled(key, 1, RESET_GAP_S) and not db.throttled(key, RESETS_PER_EMAIL_HOUR, 3600):
            db.hit(key)
            _later(_send_reset, email)
        return render_template("check_email.html", email=email, what="reset", minutes=RESET_MINUTES)
    return render_template("forgot.html", email="")


@bp.route("/reset", methods=["GET", "POST"])
def reset():
    """The link in the reset email. ?t= is moved into the session first, so the
    page that asks for the new password has a clean address."""
    if request.method == "GET" and request.args.get("t"):
        _stash("reset_t", request.args.get("t")[:200])
        return redirect(url_for("logins.reset"))
    token = _stashed("reset_t")
    with db.connect() as conn:
        u = reset_user(conn, token)
        if u is None:
            session.pop("reset_t", None)
            flash("That reset link has expired or was already used. Ask for a new one below.")
            return redirect(url_for("logins.forgot"))
        if request.method == "POST":
            pw = request.form.get("password") or ""
            if len(pw) < MIN_PW or len(pw) > MAX_PW:
                flash(f"Use a password of at least {MIN_PW} characters.")
                return render_template("reset.html", email=u["email"]), 400
            done = use_reset(conn, token, pw)
            if done is None:
                session.pop("reset_t", None)
                flash("That reset link has expired or was already used. Ask for a new one below.")
                return redirect(url_for("logins.forgot"))
    if request.method == "POST":
        _log_in(done)
        flash("Password changed. You're logged in. Other browsers were logged out.", "ok")
        return remember_browser(redirect(url_for("studio")), request, done["email"])
    return render_template("reset.html", email=u["email"])


@bp.route("/m", methods=["GET", "POST"])
def magic_login():
    """The link in a first-run nudge email (nudges.py): logs that account in and
    opens Home, the first-ad form. Reaching the inbox confirms the address too.
    Hardened: works once, for 24 hours, and only from the Log in button on the
    page the link opens (a mail scanner's fetch doesn't spend it)."""
    import nudges
    secret = current_app.secret_key
    if not enabled():
        with db.connect() as conn:
            u = nudges.open_link(conn, secret, request.args.get("t") or "")
        if u is None:
            flash("That link has expired. Log in with your email and password.")
            return redirect(url_for("login"))
        _log_in(u)
        return redirect(url_for("studio"))
    if request.method == "GET" and request.args.get("t"):
        _stash("magic_t", request.args.get("t")[:400])
        return redirect(url_for("logins.magic_login"))
    token = _stashed("magic_t")
    with db.connect() as conn:
        u = nudges.check_link(conn, secret, token)
        used = u is not None and link_used(conn, token)
        if u is None or used:
            session.pop("magic_t", None)
            flash("That link was already used or has expired. Log in with your email and password, "
                  "or reset your password." if token else "That link has expired. Log in with your email and password.")
            return redirect(url_for("login"))
        if request.method == "GET":
            return render_template("magic.html", email=u["email"], signed_in=bool(g.user))
        if not spend_link(conn, token, u, nudges.link_hours() * 3600):
            session.pop("magic_t", None)
            flash("That link was already used. Log in with your email and password, or reset your password.")
            return redirect(url_for("login"))
        u = nudges.confirm_inbox(conn, u)
    _log_in(u)
    return redirect(url_for("studio"))
