"""The "Pick a plan" step (plan_gate.py, PLANS_ONLY): the page, and the gate that
sends a workspace with no plan to it before it opens anything else in the app."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

import billing
import db
import plan_gate
import plans
from core import is_owner, login_required

bp = Blueprint("plangate", __name__)

# What a workspace with no plan can still open: its library, downloads and sharing, Billing (where a plan is
# picked, a code is entered and a card is fixed), email confirmation (its step comes before this one:
# confirm_step.py), the first two onboarding questions and the
# brand brief (they come before this step: a new account sees its brand read back before it is asked for a card;
# the brief's one paid action waits for a plan, onboarding.brief), the look switch, workspace deletion and logging
# out.
ALLOWED = {"logout", "library", "media", "photo", "api_jobs", "jobs.cards",
           "billing_page", "subscribe", "checkout", "billing_portal", "billing_code",
           "verify_email", "verify_resend", "verify_code_send", "verify_code_check", "confirm.page",
           "adhub.export_page", "adhub.export_file", "share.create", "share.revoke",
           "admin_view_exit", "onboarding.intent", "onboarding.profile", "onboarding.brief", "ui_v2.set_look"}
OPEN_PREFIXES = ("plangate.", "wsdelete.")


def _wants_json() -> bool:
    return (request.is_json or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/"))


def _refuse(msg=None):
    if _wants_json():
        return jsonify({"error": msg or plan_gate.MESSAGE, "plan_required": True, "url": url_for("plangate.page")}), 402
    if msg and msg != plan_gate.MESSAGE:
        flash(msg)
    return redirect(url_for("plangate.page"))


@bp.before_app_request
def gate():
    if not plan_gate.on():
        return None
    if g.get("user") is None or g.get("ws") is None or g.get("viewing") is not None:
        return None
    ep = request.endpoint or ""
    if ep == "static" or ep.startswith(OPEN_PREFIXES) or ep in ALLOWED:
        return None
    if not (request.path.startswith("/app") or request.path.startswith("/api/")):
        return None
    with db.connect() as conn:
        if not plan_gate.needs_plan(conn, g.ws, g.user):
            return None
    return _refuse()


@bp.app_errorhandler(plan_gate.PlanRequired)
def plan_required_error(e):
    return _refuse(e.description)


@bp.route("/app/plan")
@login_required
def page():
    import stripe_setup
    stripe_setup.ready()        # right after a restart the plan prices settle in a start-up step: wait for it
    with db.connect() as conn:
        # A workspace on its welcome credits may look and skip (welcome); anyone else with no need goes on.
        welcome = plan_gate.welcome_left(conn, g.ws)
        if welcome is None and not plan_gate.needs_plan(conn, g.ws, g.user):
            return redirect(url_for("studio"))
        trials = {p["code"]: plan_gate.trial_days(conn, g.ws, g.user, p["code"]) for p in plans.PAID}
        made = conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is not None
        import first_ads            # the first ads on us (the guided first run): said here when they are still open
        gift = (first_ads.on() and any(first_ads.left(conn, g.ws["id"], g.user).values())
                and not first_ads.used_elsewhere(conn, g.ws, g.user))
    terms = {code: plan_gate.terms(code, days) for code, days in trials.items()}
    return render_template("plan_pick.html", trials=trials, terms=terms, new=not made, owner=is_owner(),
                           any_trial=any(trials.values()), days=plan_gate.trial_len(g.ws),
                           trial_line=plan_gate.timeline(None, None, days=plan_gate.trial_len(g.ws)), gift=gift,
                           canceled=request.args.get("canceled") == "1", checkout_open=billing.configured(),
                           welcome=welcome, skip_url=url_for("studio") if welcome else None)


@bp.app_context_processor
def _welcome_banner():
    """On app pages, while the workspace is on its welcome credits: what is left, and that videos and more come
    with a plan (one line, with See plans)."""
    if not plan_gate.on() or g.get("ws") is None or g.get("user") is None or g.get("viewing") is not None:
        return {}
    ep = request.endpoint or ""
    if ep in ("", "static") or ep.startswith(("plangate.", "onboarding.", "confirm.")) or not request.path.startswith("/app"):
        return {}
    with db.connect() as conn:
        left = plan_gate.welcome_left(conn, g.ws)
    if left is None:
        return {}
    return {"welcome_banner": {"left": left, "plans": url_for("plangate.page")}}


UPGRADE_AUTO_ENDPOINTS = ("desk.page", "desk.thread")


@bp.app_context_processor
def _upgrade_box():
    """The upgrade box on app pages of a workspace with no plan (web/_upgrade_dialog.html). It opens by itself once,
    on the desk, after the first ads on the welcome credits (the member's answers keep upgrade_seen); otherwise it
    waits for a refused spend (static/app.js cfyUpgrade)."""
    if not plan_gate.on() or g.get("ws") is None or g.get("user") is None or g.get("viewing") is not None:
        return {}
    ep = request.endpoint or ""
    if ep in ("", "static") or ep.startswith(("plangate.", "onboarding.", "confirm.")) or not request.path.startswith("/app"):
        return {}
    if plans.plan_code(g.ws) != "free" or g.user["is_admin"]:
        return {}
    import onboarding
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        left = plan_gate.welcome_left(conn, ws)
        auto = False
        if left is not None and ep in UPGRADE_AUTO_ENDPOINTS and onboarding.answers(g.user).get("upgrade_seen") != "1" \
                and conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone():
            auto = True
            onboarding.save(conn, g.user, upgrade_seen="1")
            conn.commit()
        trials = {code: plan_gate.trial_days(conn, ws, g.user, code) for code in plan_gate.TRIAL_PLANS}
    msg = (f"You have {left:,} free credits left for image ads. A plan adds videos and a full month of credits, "
           "and Starter and Growth start with a free trial." if left else
           "A plan adds videos and a full month of credits. Starter and Growth start with a free trial.")
    return {"upgrade_box": {"auto": auto, "left": left or 0, "message": msg, "trials": trials,
                            "plans": [plans.PLANS[c] for c in plan_gate.TRIAL_PLANS], "owner": is_owner()}}
