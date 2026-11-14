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

Everyone confirms (Koby, Nov 12 2026). The rule above only ever covered free
accounts, and with plans only (plan_gate.py) there are none: a sign-up picks a
plan, is then "paid", and was never asked. So while plans only is on, every
member confirms their email before they start a plan or make anything
(everyone(), must()): confirm_step.py holds the page and the gate, and
core.hold_check refuses any spend (ConfirmRequired) on every channel. Site
admins and Google sign-ins are already confirmed. RS_EMAIL_VERIFY=all keeps
this rule on with the free tier too; RS_EMAIL_VERIFY=video keeps the
free-account rule alone, as it was.

A wrong address is fixed without a second account (change_address): the member
types the right one with their password, the link and code go there
(users.pending_email), and it becomes the account's email only when that inbox
confirms. An address that already has an account gets the "you already have an
account" note instead, and the page says the same thing either way (SEC-8).
"""
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

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
CHANGES_PER_HOUR = 3           # "wrong address" changes per account per hour
CONFIRM_FIRST = "Confirm your email first."


def required() -> bool:
    return os.environ.get("RS_EMAIL_VERIFY", "1") != "0"


def everyone() -> bool:
    """Does every member confirm their email, whatever they pay? RS_EMAIL_VERIFY=all: yes. =video: no, the
    free-account rule stands alone. =0: nothing is asked. Anything else (unset, 1): while plans only is on
    (plan_gate.on()), because the free-account rule covers nobody then."""
    v = (os.environ.get("RS_EMAIL_VERIFY") or "").strip().lower()
    if v in ("0", "video"):
        return False
    if v == "all":
        return True
    import plan_gate
    return plan_gate.on()


class ConfirmRequired(HTTPException):
    """Something that spends or starts a plan, by a member who has to confirm their email first (403).
    confirm_step.py answers it with the confirm page, or JSON that opens the code box."""
    code = 403
    description = CONFIRM_FIRST


def must(user) -> bool:
    """This member confirms their email before going on: everyone() is on, the address is not confirmed and
    they are not a site admin."""
    return bool(required() and user is not None and not is_verified(user) and not user["is_admin"] and everyone())


def check(user):
    """Raise ConfirmRequired where a spend would start for a member who has to confirm first."""
    if must(user):
        raise ConfirmRequired()


def address(user) -> str:
    """Where this member's confirmation goes: the address they asked to change to (users.pending_email) while
    the account is unconfirmed, else the account's email."""
    if user is None:
        return ""
    try:
        pend = user["pending_email"]
    except (IndexError, KeyError):
        pend = None
    return pend if pend and not is_verified(user) else user["email"]


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
    generations wait for it (video, and anything past the unconfirmed allowance).
    With everyone() on it is every unconfirmed member, and everything waits."""
    if not required() or user is None or is_verified(user) or user["is_admin"]:
        return False
    if everyone():          # paid or not: everyone confirms
        return True
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
    "cap" (spent the unconfirmed allowance), "confirm" (everyone confirms, whatever
    they make), or None to go ahead."""
    if not blocked(conn, ws, user):
        return None
    if everyone():
        return "confirm"
    if is_video(tool):
        return "video"
    if credits > 0 and spent_on_jobs(conn, ws["id"]) + credits > unverified_credits():
        return "cap"
    return None


def message(email: str) -> str:
    """The banner while the email is unconfirmed."""
    if everyone():
        return f"Confirm your email to keep going: we sent a link and a code to {email}."
    return f"Image ads work now. Video ads need a confirmed email: we sent a link and a code to {email}."


def needs_message(reason: str, email: str) -> str:
    if reason == "confirm":
        return f"{CONFIRM_FIRST} We sent a code to {email}."
    if reason == "cap":
        return f"Confirm your email to keep making ads. We sent a code to {email}."
    return f"Confirm your email to make video ads. We sent a code to {email}."


def done_message() -> str:
    """What a member reads once their email is confirmed."""
    return "Email confirmed." if everyone() else "Email confirmed. Video ads are unlocked."


def code_message(outcome: str, left: int = 0) -> str:
    """The words for a check_code outcome that did not confirm."""
    return {"format": "Enter the 6-digit code from the email.",
            "none": "Press Resend code and we'll email you one.",
            "expired": "That code has expired. Press Resend code for a new one.",
            "locked": "Too many wrong tries. Press Resend code for a new one.",
            "taken": "That address can't be used for this account. Change it and try again.",
            "wrong": f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left."}[outcome]


def resend_wait(user, ip: str):
    """Why a confirmation email can't go out right now (once a minute, three an hour per account, ten an
    hour per network), or None after recording the send."""
    key, ipkey = f"verify:{user['id']}", f"verify-ip:{ip}"
    if db.throttled(key, 1, RESEND_GAP_S):
        return "We just sent one. Give it a minute, and check your spam folder."
    if db.throttled(key, RESEND_PER_HOUR, 3600) or db.throttled(ipkey, RESEND_PER_IP_HOUR, 3600):
        return "We've sent a few links already. Check your spam folder, or try again in an hour."
    db.hit(key)
    db.hit(ipkey)
    return None


# ---------------------------------------------------------------- tokens

def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SALT)


def new_nonce() -> str:
    return secrets.token_urlsafe(9)


def make_token(secret: str, user) -> str:
    return _serializer(secret).dumps({"u": user["id"], "n": user["verify_nonce"] or "", "e": address(user)})


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
    if user is None or address(user) != data.get("e"):
        return "bad", None
    if not data.get("n") or user["verify_nonce"] != data.get("n"):
        return ("already" if is_verified(user) else "used"), user
    if not _mark(conn, user, nonce=data["n"]):
        return "used", user
    user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    completed(conn, user, "/verify")
    return "ok", user


def _mark(conn, user, nonce=None) -> bool:
    """Mark this account's email confirmed (no commit). An address it asked to change to becomes its email in
    the same statement. False when this call did not confirm it: already confirmed, the link's nonce is gone,
    or the new address was taken by another account in the meantime."""
    to = address(user)
    sql = ("UPDATE users SET email=?, email_norm=?, pending_email=NULL, email_verified_at=?, verify_nonce=NULL, "
           "verify_code_hash=NULL, verify_code_exp=NULL, verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL")
    args = [to, db.norm_email(to), db.now(), user["id"]]
    if nonce is not None:
        sql += " AND verify_nonce=?"
        args.append(nonce)
    try:
        return conn.execute(sql, args).rowcount == 1
    except sqlite3.IntegrityError:
        return False


def completed(conn, user, path: str = "/app"):
    """The member confirmed their email: the funnel's verify_completed step (site_events), once per workspace, the
    address goes on the cards they saved before (card_on_file.email_confirmed, L-2), and a new workspace gets its
    welcome credits (plan_gate.grant_welcome). Never raises."""
    try:
        import card_on_file
        card_on_file.email_confirmed(conn, user)
    except Exception:  # noqa: BLE001
        log.warning("card rows not updated after user %s confirmed", user["id"])
    try:
        import plan_gate     # not a free plan: the welcome credits, once, for the brand setup and first image ads
        conn.execute("SAVEPOINT welcome")
        try:
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (user["workspace_id"],)).fetchone()
            granted = plan_gate.grant_welcome(conn, ws, user)
        except Exception:
            conn.execute("ROLLBACK TO welcome")     # only the grant: the confirmation itself stands
            raise
        finally:
            conn.execute("RELEASE welcome")
        if granted:
            conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("welcome credits not granted for user %s", user["id"])
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
    "expired" | "locked" | "wrong" | "taken" (the address it asked to change
    to belongs to another account by now)."""
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
    if not _mark(conn, user):
        conn.rollback()
        now = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        return ("already" if is_verified(now) else "taken"), 0
    conn.commit()
    completed(conn, conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())
    return "ok", left


# ---------------------------------------------------------------- the email (link + code)

# What the email says in place of the free credits when there are none to describe.
WHY_CONFIRM = "This is the address your receipts, reminders and password resets go to."


def _no_free_credits() -> bool:
    """A sign-up gets no starter credits (plans only), so the email must not describe any."""
    import plan_gate
    return plan_gate.on()


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
    if everyone() or _no_free_credits():
        what = WHY_CONFIRM      # no free credits to describe: plans only, or a member who already has a plan
    else:
        # Say what the free credits buy (2 image ads by default); mention the watermarked preview only when it's on.
        what = _free_what(conn, user)
        what = what[0].upper() + what[1:] + (" land when you save a card in Campaignify (nothing is charged), and then "
                                             "image ads work right away. Video ads need this confirmation"
                                             if _card_waiting(conn, user) else
                                             " are ready: image ads work right away. Video ads need this confirmation")
        what += (f", and so does your free {preview.SECONDS}-second video preview." if preview.enabled() else ".")
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
        mailer.send(frm=from_address(), to=address(user), subject=f"Your Campaignify code: {code}",
                    html=html, text=text, reply_to=reply_to(), tags={"kind": "verify_email"})
        return True
    except mailer.MailError as e:
        # Not sent (Nov 8 2026: the shared quota ran out). The send doesn't count against
        # the once-a-minute Resend limit, so the box can say "try again in a minute" and
        # mean it; a deferred copy goes out within the code's 15 minutes if the provider
        # takes mail again (mailer.retry_deferred), and a new Resend replaces it.
        log.warning("verification email to user %s not sent: %s", user["id"], e)
        _refund_resend(user)
        return False


def _send_first_login(conn, user, link: str) -> bool:
    if _no_free_credits():
        what = WHY_CONFIRM
    else:
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
        mailer.send(frm=from_address(), to=address(user), subject="Confirm your email to log in to Campaignify",
                    html=html, text=text, reply_to=reply_to(), tags={"kind": "verify_signup"})
        return True
    except mailer.MailError as e:
        # A quota or rate refusal keeps the copy: mailer.retry_deferred sends it when the
        # provider takes mail again (the link works 48 hours), so the sign-up isn't stuck.
        log.warning("sign-up confirmation email to user %s not sent%s: %s", user["id"],
                    " (deferred, resent later)" if getattr(e, "queued", False) else "", e)
        _refund_resend(user)
        return False


def _refund_resend(user):
    try:
        db.unhit(f"verify:{user['id']}")
    except Exception:  # noqa: BLE001
        pass


def code_sent(user) -> bool:
    """The newest code email reached the provider (False only when the log says it didn't)."""
    return mailer.delivered(address(user), "verify_email") is not False


# ---------------------------------------------------------------- a wrong address

def valid_address(email: str) -> bool:
    """The sign-up form's test of an email address."""
    return "@" in email and "." in email.split("@")[-1] and len(email) <= 200 and " " not in email


def change_address(conn, secret: str, user, new: str) -> bool:
    """An unconfirmed member typed another address (commits). It is kept as pending_email and the link and
    code go there; the account's email changes only when that inbox confirms (_mark). The old link and code
    stop working. Typing the account's own address again drops the change.

    An address that already has an account is kept as pending too, but gets the "you already have an
    account" note instead of a code, so the answer here is the same for a free and a taken address
    (SEC-8) and the account can never take it. False only when the email could not be sent."""
    new = (new or "").strip().lower()
    pend = None if new == user["email"] else new
    conn.execute("UPDATE users SET pending_email=?, verify_nonce=?, verify_code_hash=NULL, verify_code_exp=NULL, "
                 "verify_code_tries=0 WHERE id=? AND email_verified_at IS NULL", (pend, new_nonce(), user["id"]))
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if pend and conn.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (pend, user["id"])).fetchone():
        import login_security
        login_security.note_taken(pend)
        return True
    return send_link(conn, secret, user)
