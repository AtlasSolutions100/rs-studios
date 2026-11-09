"""Continue with Google: OpenID Connect against Google's endpoints with `requests`
(no SDK). GET /auth/google starts it, /auth/google/callback finishes it.

Settings: GOOGLE_OAUTH_CLIENT_ID (public; DEFAULT_CLIENT_ID when unset) and
GOOGLE_OAUTH_CLIENT_SECRET (a secret). The button shows only when both are set
(configured()). The redirect URI is RS_PUBLIC_URL + /auth/google/callback, so in
production it is exactly https://campaignify.ai/auth/google/callback, whatever
the request's Host header says.

An existing account with that email is logged in (and its email counts as
confirmed: Google verified it). A new email gets a self-serve account exactly as
the sign-up form makes one (accounts.self_serve_signup), with an unusable random
password and users.auth_provider = "google". Owner setup links (owners.py) are not
supported here. Tokens are never logged."""
import logging
import os
import secrets
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, flash, g, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

import accounts
import db
import liveids
import login_security
import verify

log = logging.getLogger("rs.google_auth")
bp = Blueprint("google_auth", __name__)

# The OAuth client id of the Campaignify web app in Google Cloud. It is public
# (it's in the redirect to Google); deploy.yml passes GOOGLE_OAUTH_CLIENT_ID only
# when the repo variable is set, so this is the one place the default lives.
DEFAULT_CLIENT_ID = liveids.PRODUCTION["GOOGLE_OAUTH_CLIENT_ID"]   # read through liveids (M7.4)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
TIMEOUT = 10
# session keys while the round trip to Google is in flight
S_STATE, S_NONCE, S_NEXT, S_INVITE = "g_state", "g_nonce", "g_next", "g_invite"

FAILED = "Google sign-in didn't work. Try again, or use your email and password."


def client_id() -> str:
    return (liveids.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()


def configured() -> bool:
    """True only when both the client id and the client secret are set."""
    return bool(client_id() and client_secret())


def redirect_uri() -> str:
    return verify.public_url() + "/auth/google/callback"


def safe_next(value) -> str:
    """A `next` we follow: a path on this site (never another host)."""
    value = value or ""
    return value if value.startswith("/") and not value.startswith("//") else ""


# ---------------------------------------------------------------- Google's HTTP (patched in tests)

def exchange_code(code: str) -> dict:
    """The token response for an authorization code (id_token, access_token, ...)."""
    r = requests.post(TOKEN_URL, data={"code": code, "client_id": client_id(), "client_secret": client_secret(),
                                       "redirect_uri": redirect_uri(), "grant_type": "authorization_code"},
                      timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(f"token endpoint answered {r.status_code}")
    return r.json()


def verify_id_token(id_token: str) -> dict:
    """Google's own check of the id_token (signature, expiry): its claims."""
    r = requests.get(TOKENINFO_URL, params={"id_token": id_token}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(f"tokeninfo answered {r.status_code}")
    return r.json()


def claims_for(code: str, nonce: str) -> dict:
    """The verified identity behind an authorization code, or ValueError.
    Checks the audience, the issuer, the nonce and that Google verified the email."""
    tok = exchange_code(code)
    id_token = tok.get("id_token") if isinstance(tok, dict) else None
    if not id_token:
        raise ValueError("no id_token in the token response")
    info = verify_id_token(id_token)
    if info.get("aud") != client_id():
        raise ValueError("id_token is for another client")
    if info.get("iss") not in ISSUERS:
        raise ValueError("id_token is not from Google")
    if nonce and info.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    verified = info.get("email_verified")
    if verified not in (True, "true"):
        raise ValueError("email not verified by Google")
    email = (info.get("email") or "").strip().lower()
    if "@" not in email or len(email) > 200:
        raise ValueError("no usable email in the id_token")
    return {"email": email, "name": (info.get("name") or "").strip()[:80], "sub": info.get("sub")}


# ---------------------------------------------------------------- routes

@bp.route("/auth/google")
def start():
    if not configured():
        abort(404)
    if g.get("user"):
        return redirect(url_for("studio"))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session[S_STATE], session[S_NONCE] = state, nonce
    nxt = safe_next(request.args.get("next"))
    if nxt:
        session[S_NEXT] = nxt
    else:
        session.pop(S_NEXT, None)
    invite = (request.args.get("invite") or "").strip()
    if invite:
        session[S_INVITE] = invite
    else:
        session.pop(S_INVITE, None)
    q = {"client_id": client_id(), "redirect_uri": redirect_uri(), "response_type": "code",
         "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
         "access_type": "online"}
    return redirect(AUTH_URL + "?" + urlencode(q))


@bp.route("/auth/google/callback")
def callback():
    if not configured():
        abort(404)
    state, nonce = session.pop(S_STATE, None), session.pop(S_NONCE, None)
    nxt, invite = session.pop(S_NEXT, ""), session.pop(S_INVITE, "")
    if request.args.get("error"):
        log.info("google sign-in refused: %s", request.args.get("error")[:60])
        flash("Google sign-in was cancelled. You can try again.")
        return redirect(url_for("login"))
    sent = request.args.get("state") or ""
    if not state or not sent or not secrets.compare_digest(sent, state) or not request.args.get("code"):
        flash("That sign-in attempt expired. Try again.")
        return redirect(url_for("login"))
    try:
        who = claims_for(request.args.get("code"), nonce or "")
    except ValueError as e:        # Google's answer didn't check out; the message never carries a token
        log.warning("google sign-in refused: %s", str(e)[:120])
        flash(FAILED)
        return redirect(url_for("login"))
    except Exception:  # noqa: BLE001  network or a malformed answer; never log the tokens
        log.exception("google sign-in failed")
        flash(FAILED)
        return redirect(url_for("login"))
    email, name = who["email"], who["name"]
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u is not None:
            if not verify.is_verified(u):
                # Google verified the address, so it counts as confirmed (as /verify does).
                conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, "
                             "verify_code_exp=NULL, verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL",
                             (db.now(), u["id"]))
                conn.commit()
                verify.completed(conn, u, "/auth/google/callback")
            if login_security.awaiting_confirm(u):
                # A form sign-up that never opened its link: whoever typed that password
                # didn't prove the inbox, Google just did. The password goes; the account
                # signs in with Google (or a reset) from now on (SEC-8).
                conn.execute("UPDATE users SET pw_hash=?, signup_pending=0, auth_provider='google' WHERE id=?",
                             (generate_password_hash(secrets.token_urlsafe(32)), u["id"]))
                conn.commit()
            u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
            login_security.start_session(u)
            return redirect(nxt or url_for("studio"))
    ip = request.remote_addr or "unknown"
    if accounts.signup_throttled(ip):
        flash("Too many new accounts from your network. Try again later.")
        return redirect(url_for("login"))
    try:
        # Nobody knows this password; the account signs in with Google (or a later reset).
        return accounts.self_serve_signup(email, name, generate_password_hash(secrets.token_urlsafe(32)), request, ip,
                                          _secret(), invite_code=invite, auth_provider="google", verified=True,
                                          dest=nxt or None)
    except accounts.SignupRefused as e:
        flash(str(e))
        return redirect(url_for("signup", invite=invite or None))


def _secret() -> str:
    from flask import current_app
    return current_app.secret_key
