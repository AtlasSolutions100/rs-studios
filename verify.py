"""Email verification for free accounts (stress-test fix).

A free account's starter credits and its free talking-video preview cost real
vendor money, so they only work once the member has confirmed their email:
signup sends a signed, expiring, single-use link (/verify?t=...); until it is
clicked, generation is refused with a friendly message and a banner offers to
resend the link (rate limited).

Never blocked: workspaces on a paid plan (live subscription or an admin comp),
workspaces that bought a credit pack, and site admins. Accounts that existed
before this check are marked verified when the column is added (db.py).
RS_EMAIL_VERIFY=0 switches the check off.
"""
import logging
import os
import secrets

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
    """True when this member may not spend the free credits or claim the free preview yet."""
    if not required() or user is None or is_verified(user) or user["is_admin"]:
        return False
    return not paid(conn, ws)


def message(email: str) -> str:
    return f"Confirm your email to use your free credits — we sent a link to {email}."


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
    cur = conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL WHERE id=? AND verify_nonce=?",
                       (db.now(), user["id"], data["n"]))
    if cur.rowcount != 1:
        return "used", user
    return "ok", user


# ---------------------------------------------------------------- the email

def _free_what(conn, user) -> str:
    """'your free Campaignify credits (2 image ads)': what the starter credits on this account buy."""
    import catalog
    r = catalog.rates()
    row = conn.execute("SELECT delta FROM ledger WHERE workspace_id=? AND reason LIKE 'starter credits%' "
                       "ORDER BY id LIMIT 1", (user["workspace_id"],)).fetchone()
    n = row["delta"] if row and row["delta"] > 0 else catalog.starter_credits()
    images = n // max(r["image"], 1)
    ads = f"{images} image ad{'' if images == 1 else 's'}"
    if n >= r["video5"]:
        ads = f"your first video ad or {ads}"
    return f"your free Campaignify credits ({ads})"


def send_link(conn, secret: str, user) -> bool:
    """Email the confirmation link (makes a nonce if the account has none; commits).
    False when email isn't connected or the send failed (logged)."""
    if not user["verify_nonce"]:
        conn.execute("UPDATE users SET verify_nonce=? WHERE id=?", (new_nonce(), user["id"]))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    link = public_url() + "/verify?t=" + make_token(secret, user)
    import preview
    # Say what the free credits buy (2 image ads by default); mention the watermarked preview only when it's on.
    what = _free_what(conn, user)
    if preview.enabled():
        what += f" and your free {preview.SECONDS}-second talking-video preview (480p, watermarked)"
    text = (f"Hi,\n\nConfirm your email to start using {what}:\n\n{link}\n\nThe link works for 48 hours. If you "
            "didn't sign up for Campaignify, ignore this email and nothing happens.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"<p>Hi,</p><p>Confirm your email to start using {what}.</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Confirm my email</a></p>'
            f'<p style="font-size:13px;color:#555">Or paste this link into your browser:<br>{link}</p>'
            '<p style="font-size:13px;color:#555">The link works for 48 hours. If you didn\'t sign up for '
            "Campaignify, ignore this email and nothing happens.</p></div>")
    try:
        mailer.send(frm=from_address(), to=user["email"], subject="Confirm your email for Campaignify",
                    html=html, text=text, reply_to=reply_to(), tags={"kind": "verify_email"})
        return True
    except mailer.MailError as e:
        log.warning("verification email to user %s not sent: %s", user["id"], e)
        return False
