"""The "Confirm your email" step, for everyone (Koby, Nov 12 2026; the rule is verify.everyone()).

Before this, only free accounts were asked to confirm (verify.py), and with plans only there are none: a sign-up
picked a plan, counted as paid and was never asked. Now a member with an unconfirmed email confirms it before they
start a plan or make anything.

    GET  /app/confirm     the step: the 6-digit code from the email, "Send a new code", "Wrong address?"
    POST /app/confirm     do=check (the code), do=resend, do=change (a new address, with the account's password)

Where it sits. A new account still answers the two questions and sees its brand read back first (the same order the
plan step keeps: something of theirs on screen before anything is asked of them). The step comes when they go on from
there: before the plan page, so no trial starts and no card is taken for an address nobody has proven. In the
current look, which has no questions, it is the first page after sign-up.

Who is asked. Every member whose email is unconfirmed while verify.everyone() is on, on a plan or not: an account that
was comped, used a free-month code or started a plan before this step existed is asked at its next visit. Site admins
and Google sign-ins are confirmed already. An admin viewing another workspace is never stopped.

What stays open (ALLOWED): the library, downloads, Billing and Stripe's portal (so a plan can always be canceled or a
card fixed), the confirm endpoints, removing a share link, deleting the workspace, the look switch and logging out.
Everything else under /app and /api answers with this page, or for a script with JSON that opens the code box
(static/app.js cfyVerify): {"error", "verify": true, "reason": "confirm", "url"}, status 403.

Nothing that spends gets past it on any channel: core.hold_check raises verify.ConfirmRequired before a job is queued
or an AI call is reserved, which covers the API and the MCP tools as well as the pages.

The step's address carries nothing: where the member was going is kept in the session (NEXT_KEY), not in the
query. That matters for measurement, not only tidiness: in the current look this is the first page after sign-up,
where the sign-up conversion fires, and the ad pixels do not load on a page whose query has anything they do not
know (pixels.page_ok).

The page works without script. It sends a fresh code by itself only when the last one can no longer be used, under the
same limits as Resend (verify.resend_wait). A site admin can open it to look at it; their email is already confirmed.
"""
from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

import db
import mailer
import verify
from core import login_required

bp = Blueprint("confirm", __name__)

ALLOWED = {"logout", "library", "media", "photo", "api_jobs", "jobs.cards",
           "billing_page", "billing_portal",
           "verify_email", "verify_resend", "verify_code_send", "verify_code_check",
           "adhub.export_page", "adhub.export_file", "share.revoke",
           "admin_view_exit", "onboarding.intent", "onboarding.profile", "onboarding.brief", "ui_v2.set_look"}
OPEN_PREFIXES = ("confirm.", "wsdelete.")
WRONG_PASSWORDS, WRONG_WINDOW_S = 5, 900      # wrong passwords on "Wrong address?" per account, per 15 minutes


def _wants_json() -> bool:
    return (request.is_json or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/"))


NEXT_KEY = "confirm_next"       # where the member was going, kept in the session: the step's address carries nothing


def _next(raw) -> str:
    """A page of ours to go on to afterwards, or "" (never this step itself, never another site)."""
    raw = raw or ""
    ok = raw.startswith("/") and not raw.startswith("//") and "\\" not in raw and not raw.startswith("/app/confirm")
    return raw if ok else ""


def _refuse():
    if _wants_json():
        to = verify.address(g.get("user"))
        return jsonify({"error": verify.needs_message("confirm", to), "verify": True, "reason": "confirm",
                        "url": url_for("confirm.page")}), 403
    if request.method == "GET":
        session[NEXT_KEY] = _next(request.full_path.rstrip("?"))
    return redirect(url_for("confirm.page"))


@bp.before_app_request
def gate():
    user = g.get("user")
    if user is None or g.get("ws") is None or g.get("viewing") is not None or not verify.must(user):
        return None
    ep = request.endpoint or ""
    if not ep or ep == "static" or ep.startswith(OPEN_PREFIXES) or ep in ALLOWED:
        return None
    if not (request.path.startswith("/app") or request.path.startswith("/api/")):
        return None
    if _questions_first(user, ep):
        return None
    return _refuse()


def _questions_first(user, ep: str) -> bool:
    """Is onboarding about to send this request to the questions or the brief (onboarding._start, the same
    conditions)? Then this step waits, whichever of the two hooks runs first."""
    import onboarding
    if request.method != "GET" or ep not in onboarding.START_ENDPOINTS:
        return False
    if request.args.get("canceled") or request.args.get("declined"):
        return False
    with db.connect() as conn:
        if onboarding.due(conn, user, g.ws):
            return True
        return ep not in ("card.page", "plangate.page") and onboarding.brief_due(conn, user, g.ws)


@bp.app_errorhandler(verify.ConfirmRequired)
def confirm_required(e):
    if g.get("user") is None:
        return e.get_response()
    return _refuse()


def _send_if_needed(user) -> str:
    """Send a fresh code when the last one can no longer be used. Returns a line to show, or ""."""
    if verify.code_live(user) and verify.code_sent(user):
        return ""
    wait = verify.resend_wait(user, request.remote_addr or "unknown")
    if wait:
        return ""           # the sign-up email (or the last Resend) is still on its way
    with db.connect() as conn:
        sent = verify.send_link(conn, current_app.secret_key, user)
    return "" if sent else mailer.COULD_NOT_SEND_CODE


def _render(user, err="", change_err="", new_email="", status=200):
    with db.connect() as conn:
        import plans
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (user["workspace_id"],)).fetchone()
        has_plan = ws is not None and plans.plan_code(ws) != "free"
    return render_template("confirm_email.html", addr=verify.address(user), err=err, change_err=change_err,
                           new_email=new_email, has_plan=has_plan, looking=not verify.must(user),
                           why=verify.WHY_CONFIRM, minutes=verify.CODE_TTL_S // 60), status


def _onward():
    """Go on to where the member was headed when the step stopped them (Home when it was nowhere)."""
    return redirect(_next(session.pop(NEXT_KEY, "")) or url_for("studio"))


@bp.route("/app/confirm", methods=["GET", "POST"])
@login_required
def page():
    with db.connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    if not verify.must(user):
        if request.method == "GET" and user["is_admin"]:
            return _render(user)           # an admin looking at the step
        if request.method == "POST":
            flash("Your email is already confirmed.", "ok")
        return _onward()
    here = url_for("confirm.page")
    ip = request.remote_addr or "unknown"
    if request.method == "GET":
        import onboarding
        onboarding.step("confirm", "seen")
        return _render(user, err=_send_if_needed(user))

    do = request.form.get("do") or "check"
    if do == "resend":
        wait = verify.resend_wait(user, ip)
        if wait:
            flash(wait)
            return redirect(here)
        with db.connect() as conn:
            sent = verify.send_link(conn, current_app.secret_key, user)
        flash(f"Sent a new code to {verify.address(user)}. It can take a minute; check spam too." if sent
              else mailer.COULD_NOT_SEND_CODE)
        return redirect(here)

    if do == "change":
        new = (request.form.get("email") or "").strip().lower()
        if db.throttled(f"verify-change-pw:{user['id']}", WRONG_PASSWORDS, WRONG_WINDOW_S):
            return _render(user, change_err="Too many wrong passwords. Wait 15 minutes and try again.",
                           new_email=new, status=429)
        if not verify.valid_address(new):
            return _render(user, change_err="Enter the email address you want to use.", new_email=new, status=400)
        if not check_password_hash(user["pw_hash"], request.form.get("password") or ""):
            db.hit(f"verify-change-pw:{user['id']}")
            return _render(user, change_err="That password isn't right.", new_email=new, status=400)
        if db.throttled(f"verify-change:{user['id']}", verify.CHANGES_PER_HOUR, 3600):
            return _render(user, change_err="You've changed it a few times already. Try again in an hour.",
                           new_email=new, status=429)
        db.hit(f"verify-change:{user['id']}")
        db.hit(f"verify:{user['id']}")
        with db.connect() as conn:
            sent = verify.change_address(conn, current_app.secret_key, user, new)
        flash(f"We sent a code to {new}. It can take a minute; check spam too." if sent else mailer.COULD_NOT_SEND_CODE)
        return redirect(here)

    ipkey = f"verify-check-ip:{ip}"
    if db.throttled(ipkey, verify.CHECKS_PER_IP_HOUR, 3600):
        return _render(user, err="Too many tries from here. Use the link in the email, or try again in an hour.",
                       status=429)
    db.hit(ipkey)
    with db.connect() as conn:
        outcome, left = verify.check_code(conn, current_app.secret_key, user, request.form.get("code") or "")
    if outcome in ("ok", "already"):
        flash(verify.done_message(), "ok")
        return _onward()
    msg = verify.code_message(outcome, left).replace("Press Resend code", "Press Send a new code")
    return _render(user, err=msg, status=400)
