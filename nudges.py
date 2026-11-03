"""First-run nudges: "Your 2 free ads are waiting" to sign-ups that never started an ad.

The sign-up to first-ad step is where free accounts leak (4 sign-ups, 0 ads in
the first week of ads). A new self-serve owner whose workspace has no job yet
gets one email 10 minutes after signing up (STEP_1_MIN) and one more a day later
(STEP_2_H), each with a magic link that logs them in on /app, the first-ad form.
Ad-grant sign-ups (adoffer.py, the 500-credit offer) are told their free video
ad is waiting; everyone else their 2 free image ads.

Rules: one email per step per person (first_run_nudges), only accounts made in
the last MAX_AGE_DAYS (so a deploy never mails old dead sign-ups), only when
email is connected, never to admins, invitees or paid workspaces. The magic link
(/m?t=...) is a signed token for that account, good for LINK_HOURS; opening it
logs the account in and, since the email reached them, counts as confirming the
address (verify.py) so video ads are unlocked when they land.

run_once() is called every TICK_S by a daemon thread started from app.py (the
same pattern as the hourly Stripe plan sync). FIRST_RUN_NUDGES=0 switches the
emails off (the thread still runs and does nothing).
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import db
import mailer
import verify

log = logging.getLogger("campaignify")

STEP_1_MIN = 10
STEP_2_H = 24
MAX_AGE_DAYS = 7
LINK_HOURS = 72
TICK_S = 300
SALT = "cfy-magic-login"
SUBJECTS = {1: {"ads": "Your 2 free ads are waiting", "video": "Your free video ad is waiting"},
            2: {"ads": "Still here: your 2 free ads", "video": "Still here: your free video ad"}}


def enabled() -> bool:
    return os.environ.get("FIRST_RUN_NUDGES", "1") != "0"


def _iso(dt) -> str:
    return dt.replace(microsecond=0).isoformat()


def _now():
    return datetime.now(timezone.utc)


def is_grant(conn, workspace_id: int) -> bool:
    """Did this workspace get the paid-ad sign-up offer (adoffer.py)?"""
    return conn.execute("SELECT 1 FROM ad_signup_grants WHERE workspace_id=?", (workspace_id,)).fetchone() is not None


def due(conn, now=None) -> list:
    """(user row, step) pairs to email now: self-serve owners of a free workspace
    with no job, old enough for the step, not yet sent that step."""
    now = now or _now()
    out = []
    for step, age in ((1, timedelta(minutes=STEP_1_MIN)), (2, timedelta(hours=STEP_2_H))):
        rows = conn.execute(
            "SELECT u.* FROM users u WHERE u.role='owner' AND u.is_admin=0 AND u.created_at<=? AND u.created_at>=? "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id=u.workspace_id) "
            "AND NOT EXISTS (SELECT 1 FROM first_run_nudges n WHERE n.user_id=u.id AND n.step=?) ORDER BY u.id",
            (_iso(now - age), _iso(now - timedelta(days=MAX_AGE_DAYS)), step)).fetchall()
        for u in rows:
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
            if ws is not None and not verify.paid(conn, ws):
                out.append((u, step))
    return out


# ---------------------------------------------------------------- magic link

def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SALT)


def magic_token(secret: str, user) -> str:
    return _serializer(secret).dumps({"u": user["id"], "e": user["email"]})


def magic_link(secret: str, user) -> str:
    return verify.public_url() + "/m?t=" + magic_token(secret, user)


def open_link(conn, secret: str, token: str):
    """The account a magic link logs in, or None (bad, expired, email changed).
    Marks the email confirmed: the link only ever reached that inbox."""
    try:
        data = _serializer(secret).loads(token or "", max_age=LINK_HOURS * 3600)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = conn.execute("SELECT * FROM users WHERE id=?", (data.get("u"),)).fetchone()
    if user is None or user["email"] != data.get("e"):
        return None
    if not verify.is_verified(user):
        conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, verify_code_exp=NULL, "
                     "verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL", (db.now(), user["id"]))
        conn.commit()
        verify.completed(conn, user, "/m")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return user


# ---------------------------------------------------------------- the emails

def _copy(step: int, grant: bool, link: str) -> tuple[str, str, str]:
    """(subject, text, html) for a step."""
    import catalog
    r = catalog.rates()
    kind = "video" if grant else "ads"
    subject = SUBJECTS[step][kind]
    if grant:
        what = (f"Your free credits cover a 5-second product video of your product ({r['video5']} credits), "
                f"or {r['image']}-credit image ads.")
        do = "Paste your product page or add one photo, press the button, and your video is ready in a few minutes."
    else:
        what = f"Your free credits cover 2 image ads ({r['image']} credits each)."
        do = "Paste your product page or add one photo, pick a template, and your 2 ads are ready in about a minute."
    opener = "You signed up for Campaignify, and your free ads are still waiting." if step == 1 else \
        "A day on, your free ads are still waiting in Campaignify."
    text = (f"Hi,\n\n{opener} {what}\n\n{do}\n\nOpen Campaignify (this link logs you in, and works for "
            f"{LINK_HOURS} hours):\n\n{link}\n\nNo card, nothing to cancel. If you didn't sign up, ignore this email.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"<p>Hi,</p><p>{opener} {what}</p><p>{do}</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Make my free ads</a></p>'
            f'<p style="font-size:13px;color:#555">The link logs you in and works for {LINK_HOURS} hours. '
            f"Or paste it into your browser:<br>{link}</p>"
            '<p style="font-size:13px;color:#555">No card, nothing to cancel. If you didn\'t sign up for Campaignify, '
            "ignore this email.</p></div>")
    return subject, text, html


def send(conn, secret: str, user, step: int) -> bool:
    """One nudge to one person (records the step whether or not the provider took it,
    so a failing mailer never mails anyone twice)."""
    grant = is_grant(conn, user["workspace_id"])
    subject, text, html = _copy(step, grant, magic_link(secret, user))
    conn.execute("INSERT OR IGNORE INTO first_run_nudges (user_id, step, sent_at) VALUES (?,?,?)",
                 (user["id"], step, db.now()))
    conn.commit()
    try:
        mailer.send(frm=verify.from_address(), to=user["email"], subject=subject, html=html, text=text,
                    reply_to=verify.reply_to(), tags={"kind": f"first_run_nudge_{step}"})
        return True
    except mailer.MailError as e:
        log.warning("first-run nudge %s to user %s not sent: %s", step, user["id"], e)
        return False


def run_once(secret: str, now=None) -> int:
    """Send every nudge that is due. Returns how many were sent."""
    if not enabled() or not mailer.configured():
        return 0
    sent = 0
    with db.connect() as conn:
        for user, step in due(conn, now):
            if send(conn, secret, user, step):
                sent += 1
    return sent


def start(app):
    """The background loop (daemon thread), unless RS_NUDGE_LOOP=0 (tests)."""
    if os.environ.get("RS_NUDGE_LOOP", "1") != "1":
        return

    def loop():
        while True:
            time.sleep(TICK_S)
            try:
                with app.app_context():
                    run_once(app.secret_key)
            except Exception:  # noqa: BLE001
                log.exception("first-run nudges failed")

    threading.Thread(target=loop, name="rs-first-run-nudges", daemon=True).start()
