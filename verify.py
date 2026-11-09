"""Email verification for free accounts (stress-test fix, #50; relaxed for video only).

Video costs real vendor money, so a free account confirms its email before it
makes any video (talking/UGC, product video, motion copy, 1080p, the free
talking-video preview). Everything else (image ads, copy, pages...) works
straight away with the starter or ad-offer credits, up to UNVERIFIED_CREDITS
of generations; past that the email has to be confirmed too.

Signup sends one email with both a signed, expiring, single-use link
(/verify?t=..., 48 hours) and a 6-digit code (15 minutes, CODE_TRIES tries,
stored as an HMAC, never logged). When an unconfirmed member starts a video the
app shows a small "Confirm your email" box: it sends a fresh code if the last
one is gone, takes the code and carries on with what they started. A banner
offers Resend (rate limited: once a minute, three an hour, ten an hour per IP).

Never blocked: workspaces on a paid plan (live subscription or an admin comp),
workspaces that bought a credit pack, and site admins. Accounts that existed
before this check are marked verified when the column is added (db.py).
RS_EMAIL_VERIFY=0 switches the check off.
"""
import hashlib
import hmac
import logging
import os
import secrets
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import db
import mailer
import plans

log = logging.getLogger("campaignify")

SALT = "cfy-email-verify"
MAX_AGE_S = 48 * 3600          # a link works for 48 hours
RESEND_PER_HOUR = 3            # resend clicks per account per hour
RESEND_GAP_S = 60              # and at most one a minute
RESEND_PER_IP_HOUR = 10
CODE_TTL_S = 15 * 60           # a code works for 15 minutes
CODE_TRIES = 5                 # wrong guesses per code, then Resend
CHECKS_PER_IP_HOUR = 30        # code checks per network per hour
DEFAULT_UNVERIFIED_CREDITS = 250   # generation credits an unconfirmed free account may spend (about 11 image ads)


def required() -> bool:
    return os.environ.get("RS_EMAIL_VERIFY", "1") != "0"


def from_address() -> str:
    """The From for account emails: RS_ACCOUNT_MAIL_FROM, else no-reply on the
    shared sending domain (already verified with Resend for flows)."""
    custom = (os.environ.get("RS_ACCOUNT_MAIL_FROM") or "").strip()
    return custom or f'"Campaignify" <no-reply@{mailer.domain()}>'


def reply_to() -> str:
    return (os.environ.get("RS_ACCOUNT_MAIL_REPLY_TO") or "hello@campaignify.ai").strip()


def public_url() -> str:
    """Links in emails use the configured site address, never the request's Host header."""
    return (os.environ.get("RS_PUBLIC_URL") or "https://campaignify.ai").rstrip("/")


def is_verified(user) -> bool:
    return bool(user and user["email_verified_at"])


def paid(conn, ws) -> bool:
    """A paying workspace: a live (or comped) plan, or a credit pack bought."""
    if ws is None:
        return False
    if plans.plan_code(ws) != "free":
        return True
    return conn.execute("SELECT 1 FROM purchases WHERE workspace_id=? AND status='paid' LIMIT 1",
                        (ws["id"],)).fetchone() is not None


def blocked(conn, ws, user) -> bool:
    """True while this member's email is unconfirmed and that matters: a free
    workspace (no plan, no pack bought), not a site admin. needs() says which
    generations wait for it (video, and anything past the unconfirmed allowance)."""
    if not required() or user is None or is_verified(user) or user["is_admin"]:
        return False
    return not paid(conn, ws)


def unverified_credits() -> int:
    """RS_UNVERIFIED_CREDITS: what an unconfirmed free account may spend on (non-video) generations."""
    try:
        return max(0, int(os.environ.get("RS_UNVERIFIED_CREDITS", str(DEFAULT_UNVERIFIED_CREDITS))))
    except ValueError:
        return DEFAULT_UNVERIFIED_CREDITS


def is_video(tool) -> bool:
    """Does this tool make a video? (An unknown tool counts as video.)"""
    import catalog
    return bool(tool) and catalog.DISPLAY_TOOLS[tool].get("kind") != "image"


def spent_on_jobs(conn, ws_id) -> int:
    """Credits this workspace has spent on generations, net of refunds."""
    return -int(conn.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE workspace_id=? AND job_id IS NOT NULL",
                             (ws_id,)).fetchone()[0] or 0)


def needs(conn, ws, user, tool, credits: int = 0):
    """Why this member must confirm their email before this generation: "video",
    "cap" (spent the unconfirmed allowance), or None to go ahead."""
    if not blocked(conn, ws, user):
        return None
    if is_video(tool):
        return "video"
    if credits > 0 and spent_on_jobs(conn, ws["id"]) + credits > unverified_credits():
        return "cap"
    return None


def message(email: str) -> str:
    """The banner while the email is unconfirmed."""
    return f"Image ads work now. Video ads need a confirmed email: we sent a link and a code to {email}."


def needs_message(reason: str, email: str) -> str:
    if reason == "cap":
        return f"Confirm your email to keep making ads. We sent a code to {email}."
    return f"Confirm your email to make video ads. We sent a code to {email}."


# ---------------------------------------------------------------- tokens

def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SALT)


def new_nonce() -> str:
    return secrets.token_urlsafe(9)


def make_token(secret: str, user) -> str:
    return _serializer(secret).dumps({"u": user["id"], "n": user["verify_nonce"] or "", "e": user["email"]})


def confirm(conn, secret: str, token: str, max_age: int | None = None) -> tuple[str, object]:
    """Check a link and mark the account verified (no commit). Returns (outcome, user):
    "ok" | "already" | "expired" | "used" | "bad"."""
    try:
        data = _serializer(secret).loads(token or "", max_age=MAX_AGE_S if max_age is None else max_age)
    except SignatureExpired:
        return "expired", None
    except BadSignature:
        return "bad", None
    if not isinstance(data, dict):
        return "bad", None
    user = conn.execute("SELECT * FROM users WHERE id=?", (data.get("u"),)).fetchone()
    if user is None or user["email"] != data.get("e"):
        return "bad", None
    if not data.get("n") or user["verify_nonce"] != data.get("n"):
        return ("already" if is_verified(user) else "used"), user
    cur = conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, "
                       "verify_code_exp=NULL, verify_code_tries=0 WHERE id=? AND verify_nonce=?",
                       (db.now(), user["id"], data["n"]))
    if cur.rowcount != 1:
        return "used", user
    completed(conn, user, "/verify")
    return "ok", user


def completed(conn, user, path: str = "/app"):
    """The member confirmed their email: the funnel's verify_completed step (site_events), once per workspace, and
    the address goes on the cards they saved before (card_on_file.email_confirmed, L-2). Never raises."""
    try:
        import card_on_file
        card_on_file.email_confirmed(conn, user)
    except Exception:  # noqa: BLE001
        log.warning("card rows not updated after user %s confirmed", user["id"])
    try:
        import site_events
        site_events.on_workspace_step(conn, "verify_completed", user["workspace_id"], detail=None, path=path, once=True)
    except Exception:  # noqa: BLE001
        log.warning("verify_completed not recorded for user %s", user["id"])


# ---------------------------------------------------------------- codes

def _code_hash(secret: str, user_id: int, code: str) -> str:
    return hmac.new(str(secret).encode(), f"cfy-code:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def new_code(conn, secret: str, user) -> str:
    """A fresh 6-digit code for this member (replaces the last one; no commit)."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    conn.execute("UPDATE users SET verify_code_hash=?, verify_code_exp=?, verify_code_tries=0 WHERE id=?",
                 (_code_hash(secret, user["id"], code), int(time.time()) + CODE_TTL_S, user["id"]))
    return code


def code_live(user) -> bool:
    """Is there a code in the member's inbox they can still use?"""
    return bool(user["verify_code_hash"]) and int(user["verify_code_exp"] or 0) > time.time() \
        and int(user["verify_code_tries"] or 0) < CODE_TRIES


def check_code(conn, secret: str, user, code: str) -> tuple[str, int]:
    """Check a typed code and confirm the email when it's right (commits).
    Returns (outcome, tries left): "ok" | "already" | "format" | "none" |
    "expired" | "locked" | "wrong"."""
    user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if is_verified(user):
        return "already", 0
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        return "format", CODE_TRIES - int(user["verify_code_tries"] or 0)
    if not user["verify_code_hash"]:
        return "none", 0
    if int(user["verify_code_tries"] or 0) >= CODE_TRIES:
        return "locked", 0
    if int(user["verify_code_exp"] or 0) <= time.time():
        return "expired", 0
    # Count the try before comparing, so parallel guesses can't get past the limit.
    cur = conn.execute("UPDATE users SET verify_code_tries=verify_code_tries+1 WHERE id=? AND verify_code_hash=? "
                       "AND verify_code_tries<?", (user["id"], user["verify_code_hash"], CODE_TRIES))
    conn.commit()
    if cur.rowcount != 1:
        return "locked", 0
    left = CODE_TRIES - int(user["verify_code_tries"] or 0) - 1
    if not hmac.compare_digest(_code_hash(secret, user["id"], code), user["verify_code_hash"]):
        return ("locked" if left <= 0 else "wrong"), max(left, 0)
    conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, verify_code_exp=NULL, "
                 "verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL", (db.now(), user["id"]))
    conn.commit()
    completed(conn, user)
    return "ok", left


# ---------------------------------------------------------------- the email (link + code)

def _free_what(conn, user) -> str:
    """'your free Campaignify credits (2 image ads)': what the starter credits on this account buy."""
    import catalog
    r = catalog.rates()
    row = conn.execute("SELECT delta FROM ledger WHERE workspace_id=? AND reason LIKE 'starter credits%' "
                       "ORDER BY id LIMIT 1", (user["workspace_id"],)).fetchone()
    n = row["delta"] if row and row["delta"] > 0 else (_card_waiting(conn, user) or catalog.starter_credits())
    images = n // max(r["image"], 1)
    ads = f"{images} image ad{'' if images == 1 else 's'}"
    if n >= r["video5"]:
        ads = f"your first video ad or {ads}"
    return f"your free Campaignify credits ({ads})"


def _card_waiting(conn, user) -> int:
    """Starter credits waiting for this workspace's card (card_on_file.py, RS_CARD_REQUIRED)."""
    row = conn.execute("SELECT card_pending_credits FROM workspaces WHERE id=?", (user["workspace_id"],)).fetchone()
    return int(row["card_pending_credits"] or 0) if row else 0


def send_link(conn, secret: str, user, first_login: bool = False) -> bool:
    """Email the confirmation link and a fresh 6-digit code, together (makes a
    nonce if the account has none; commits). False when email isn't connected
    or the send failed (logged, without the code).

    `first_login`: a form sign-up that can't log in until the link is opened
    (login_security.confirm_first). That email has the link only: there is no
    box to type a code into before the first login."""
    if not user["verify_nonce"]:
        conn.execute("UPDATE users SET verify_nonce=? WHERE id=?", (new_nonce(), user["id"]))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    link = public_url() + "/verify?t=" + make_token(secret, user)
    if first_login:
        return _send_first_login(conn, user, link)
    code = new_code(conn, secret, user)
    conn.commit()
    import preview
    # Say what the free credits buy (2 image ads by default); mention the watermarked preview only when it's on.
    what = _free_what(conn, user)
    what = what[0].upper() + what[1:] + (" land when you save a card in Campaignify (nothing is charged), and then "
                                         "image ads work right away. Video ads need this confirmation"
                                         if _card_waiting(conn, user) else
                                         " are ready: image ads work right away. Video ads need this confirmation")
    what += (f", and so does your free {preview.SECONDS}-second talking-video preview." if preview.enabled() else ".")
    mins = CODE_TTL_S // 60
    text = (f"Hi,\n\nYour Campaignify code is {code}\n\nType it in the box in Campaignify. It works for {mins} minutes.\n\n"
            f"Or confirm with this link (it works for 48 hours):\n\n{link}\n\n"
            f"{what}\n\n"
            "If you didn't sign up for Campaignify, ignore this email and nothing happens.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            "<p>Hi,</p><p>Your Campaignify code is:</p>"
            f'<p style="font-size:30px;font-weight:700;letter-spacing:6px;margin:8px 0 4px">{code}</p>'
            f'<p style="font-size:13px;color:#555;margin-top:0">Type it in the box in Campaignify. It works for {mins} minutes.</p>'
            '<p>Or confirm with this link:</p>'
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Confirm my email</a></p>'
            f'<p style="font-size:13px;color:#555">Or paste this link into your browser:<br>{link}</p>'
            f"<p>{what}</p>"
            '<p style="font-size:13px;color:#555">The link works for 48 hours. If you didn\'t sign up for '
            "Campaignify, ignore this email and nothing happens.</p></div>")
    try:
        mailer.send(frm=from_address(), to=user["email"], subject=f"Your Campaignify code: {code}",
                    html=html, text=text, reply_to=reply_to(), tags={"kind": "verify_email"})
        return True
    except mailer.MailError as e:
        log.warning("verification email to user %s not sent: %s", user["id"], e)
        return False


def _send_first_login(conn, user, link: str) -> bool:
    what = _free_what(conn, user)
    what = what[0].upper() + what[1:] + " are waiting in your account." + (
        " They land when you save a card. Nothing is charged." if _card_waiting(conn, user) else "")
    text = ("Hi,\n\nOpen this link to confirm your email and log in to Campaignify. "
            f"It works once, for 48 hours:\n\n{link}\n\n{what}\n\n"
            "If you didn't sign up for Campaignify, ignore this email and nothing happens.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            "<p>Hi,</p><p>Open this link to confirm your email and log in to Campaignify.</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Confirm and log in</a></p>'
            f'<p style="font-size:13px;color:#555">Or paste this link into your browser:<br>{link}</p>'
            f"<p>{what}</p>"
            '<p style="font-size:13px;color:#555">The link works once, for 48 hours. If you didn\'t sign up for '
            "Campaignify, ignore this email and nothing happens.</p></div>")
    try:
        mailer.send(frm=from_address(), to=user["email"], subject="Confirm your email to log in to Campaignify",
                    html=html, text=text, reply_to=reply_to(), tags={"kind": "verify_signup"})
        return True
    except mailer.MailError as e:
        log.warning("sign-up confirmation email to user %s not sent: %s", user["id"], e)
        return False
