"""First-run nudges: "Your 2 free ads are waiting" to sign-ups that never started an ad.

The sign-up to first-ad step is where free accounts leak (4 sign-ups, 0 ads in
the first week of ads). A new self-serve owner whose workspace has no job yet
gets one email 10 minutes after signing up (STEP_1_MIN) and one more a day later
(STEP_2_H), each with a magic link that logs them in on /app, the first-ad form.
Ad-grant sign-ups (adoffer.py, the 500-credit offer) are told their free video
ad is waiting; everyone else their 2 free image ads.

While plans only is on (plan_gate.py) a sign-up has no free credits, so the two
emails say what is true then (_copy_plans): the account stopped before picking a
plan, the link logs it in and confirms the email address (which now comes before
the plan, verify.everyone()), and what the plan step says about the trial.

Rules: one email per step per person (first_run_nudges), only accounts made in
the last MAX_AGE_DAYS (so a deploy never mails old dead sign-ups), only when
email is connected, never to admins, invitees or paid workspaces. The magic link
(/m?t=...) is a signed token for that account, good for link_hours() (24, or
LINK_HOURS with RS_LOGIN_HARDENING=0) and used once (login_security.py); opening it
logs the account in and, since the email reached them, counts as confirming the
address (verify.py) so video ads are unlocked when they land.

run_once() is called every TICK_S by a daemon thread started from app.py (the
same pattern as the hourly Stripe plan sync); the same thread resends deferred
account emails every minute (mail_tick, mailer.retry_deferred). FIRST_RUN_NUDGES=0 switches the
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


def link_hours() -> int:
    """LINK_HOURS, or 24 with login hardening on (login_security.py: single use too)."""
    import login_security
    return login_security.link_hours()


def check_link(conn, secret: str, token: str):
    """The account a magic link is for, or None (bad, expired, email changed). Changes nothing."""
    try:
        data = _serializer(secret).loads(token or "", max_age=link_hours() * 3600)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict):
        return None
    user = conn.execute("SELECT * FROM users WHERE id=?", (data.get("u"),)).fetchone()
    if user is None or user["email"] != data.get("e"):
        return None
    return user


def confirm_inbox(conn, user):
    """Mark the email confirmed (the link only ever reached that inbox); returns the fresh row."""
    if not verify.is_verified(user):
        conn.execute("UPDATE users SET email_verified_at=?, verify_nonce=NULL, verify_code_hash=NULL, verify_code_exp=NULL, "
                     "verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL", (db.now(), user["id"]))
        conn.commit()
        verify.completed(conn, user, "/m")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return user


def open_link(conn, secret: str, token: str):
    """check_link, then confirm_inbox (the old reusable link; /m spends it once when hardened)."""
    user = check_link(conn, secret, token)
    return None if user is None else confirm_inbox(conn, user)


# ---------------------------------------------------------------- the emails

def _copy(step: int, grant: bool, link: str) -> tuple[str, str, str]:
    """(subject, text, html) for a step."""
    import catalog
    r = catalog.rates()
    kind = "video" if grant else "ads"
    hours = link_hours()
    import login_security
    once = " once" if login_security.enabled() else ""
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
    text = (f"Hi,\n\n{opener} {what}\n\n{do}\n\nOpen Campaignify (this link logs you in{once}, and works for "
            f"{hours} hours):\n\n{link}\n\nNothing is charged and nothing renews. If you didn't sign up, ignore this email.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"<p>Hi,</p><p>{opener} {what}</p><p>{do}</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Make ads with your free credits</a></p>'
            f'<p style="font-size:13px;color:#555">The link logs you in{once} and works for {hours} hours. '
            f"Or paste it into your browser:<br>{link}</p>"
            '<p style="font-size:13px;color:#555">Nothing is charged and nothing renews. If you didn\'t sign up for Campaignify, '
            "ignore this email.</p></div>")
    return subject, text, html


PLAN_SUBJECTS = {1: {False: "Finish setting up Campaignify", True: "Confirm your email to finish setting up Campaignify"},
                 2: {False: "Your Campaignify account is waiting for a plan",
                     True: "Still to do: confirm your email for Campaignify"}}


def _copy_plans(step: int, days: int, unconfirmed: bool, link: str) -> tuple[str, str, str]:
    """(subject, text, html) for a step while plans only is on (plan_gate.py). A sign-up has no free credits
    then, so nothing here says it does: it says what is left to do (confirm the email, pick a plan) and what
    the plan step itself says about the trial and the charge. `days` is the trial this account would start
    with (0: it has had one, so no trial is mentioned)."""
    hours = link_hours()
    import login_security
    once = " once" if login_security.enabled() else ""
    subject = PLAN_SUBJECTS[step][bool(unconfirmed)]
    opener = "You signed up for Campaignify and stopped before picking a plan." if step == 1 else \
        "A day on, your Campaignify account is still waiting for a plan."
    confirm = " Opening the link below also confirms this email address, which comes before the plan." if unconfirmed else ""
    plan = (f"Starter and Growth are free for {days} days: your card is saved on Stripe's page and charged when "
            "the trial ends, unless you cancel first." if days else "Plans are monthly, and you can cancel any time.")
    text = (f"Hi,\n\n{opener}{confirm}\n\n{plan}\n\nOpen Campaignify (this link logs you in{once}, and works for "
            f"{hours} hours):\n\n{link}\n\nNothing is charged unless you pick a plan. If you didn't sign up, "
            "ignore this email.\n\nCampaignify")
    html = ('<div style="font-family:Inter,Arial,sans-serif;font-size:15px;line-height:1.5;color:#111">'
            f"<p>Hi,</p><p>{opener}{confirm}</p><p>{plan}</p>"
            f'<p><a href="{link}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
            'border-radius:8px;text-decoration:none;font-weight:600">Carry on in Campaignify</a></p>'
            f'<p style="font-size:13px;color:#555">The link logs you in{once} and works for {hours} hours. '
            f"Or paste it into your browser:<br>{link}</p>"
            '<p style="font-size:13px;color:#555">Nothing is charged unless you pick a plan. If you didn\'t sign up for '
            "Campaignify, ignore this email.</p></div>")
    return subject, text, html


def send(conn, secret: str, user, step: int) -> bool:
    """One nudge to one person (records the step whether or not the provider took it,
    so a failing mailer never mails anyone twice)."""
    import plan_gate
    if plan_gate.on():      # no free credits to point at: the plan step is what is waiting
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (user["workspace_id"],)).fetchone()
        subject, text, html = _copy_plans(step, plan_gate.trial_days(conn, ws, user, plan_gate.TRIAL_PLANS[0]),
                                          not verify.is_verified(user), magic_link(secret, user))
    else:
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
    """Send every nudge that is due. Returns how many were sent. Nurture
    (mailer.py): while mailer.nurture_block() says no (quota hold, a code or
    confirm email waiting to be resent, EMAIL_NURTURE_DAILY_MAX, the
    transactional reserve), nothing is claimed and the step waits for a later tick."""
    if not enabled() or not mailer.configured():
        return 0
    sent = 0
    with db.connect() as conn:
        for user, step in due(conn, now):
            why = mailer.nurture_block(conn)
            if why:
                log.info("first-run nudges paused: %s", why)
                break
            if send(conn, secret, user, step):
                sent += 1
    return sent


def mail_tick(app, n: int):
    """One pass of the loop: resend deferred account emails first (every pass, a
    minute apart: a code is only good for 15), then the nudges every TICK_S."""
    try:
        st = mailer.retry_deferred()
        if st.get("resent") or st.get("failed"):
            log.info("email resend: %s", st)
    except Exception:  # noqa: BLE001
        log.exception("email resend failed")
    try:                # gallery clips kept outside the deploy bundle: fetch any the manifest lists and we lack
        import libstore
        libstore.tick()
    except Exception:  # noqa: BLE001
        log.exception("library store pass failed")
    if n % max(1, TICK_S // RETRY_TICK_S) == 0:
        with app.app_context():
            run_once(app.secret_key)
    if n % 10 == 0:      # the reminder about a day before a trial's first charge (plan_gate.py)
        try:
            import plan_gate
            with app.app_context():
                plan_gate.run_reminders()
        except Exception:  # noqa: BLE001
            log.exception("trial reminders failed")
    import weekly_report
    if n % weekly_report.TICK_PASSES == 0:      # the weekly report email (M4.4), nurture like the nudges
        try:
            with app.app_context():
                weekly_report.run_once(app.secret_key)
        except Exception:  # noqa: BLE001
            log.exception("weekly report pass failed")


RETRY_TICK_S = 60


def start(app):
    """The background loop (daemon thread), unless RS_NUDGE_LOOP=0 (tests). In process mode (M7.2) it runs in the
    worker process only."""
    import worker
    if os.environ.get("RS_NUDGE_LOOP", "1") != "1" or not worker.background_here():
        return

    def loop():
        n = 0
        while True:
            time.sleep(RETRY_TICK_S)
            n += 1
            try:
                mail_tick(app, n)
            except Exception:  # noqa: BLE001
                log.exception("first-run nudges failed")

    threading.Thread(target=loop, name="rs-first-run-nudges", daemon=True).start()
