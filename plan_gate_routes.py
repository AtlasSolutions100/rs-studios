"""The "Pick a plan" step (plan_gate.py, PLANS_ONLY): the page, and the gate that
sends a workspace with no plan to it before it opens anything else in the app."""
from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

import billing
import db
import plan_gate
import plans
from core import is_owner, login_required

bp = Blueprint("plangate", __name__)

# What a workspace with no plan can still open: its library, downloads and sharing, Billing (where a plan is
# picked, a code is entered and a card is fixed), email confirmation, the first two onboarding questions (they
# come before this step, as they do before the card step), the look switch, workspace deletion and logging out.
ALLOWED = {"logout", "library", "media", "photo", "api_jobs", "jobs.cards",
           "billing_page", "subscribe", "checkout", "billing_portal", "billing_code",
           "verify_email", "verify_resend", "verify_code_send", "verify_code_check",
           "adhub.export_page", "adhub.export_file", "share.create", "share.revoke",
           "admin_view_exit", "onboarding.intent", "onboarding.profile", "ui_v2.set_look"}
OPEN_PREFIXES = ("plangate.", "wsdelete.")


def _wants_json() -> bool:
    return (request.is_json or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/"))


def _refuse():
    if _wants_json():
        return jsonify({"error": plan_gate.MESSAGE, "plan_required": True, "url": url_for("plangate.page")}), 402
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
    return _refuse()


@bp.route("/app/plan")
@login_required
def page():
    import stripe_setup
    stripe_setup.ready()        # right after a restart the plan prices settle in a start-up step: wait for it
    with db.connect() as conn:
        if not plan_gate.needs_plan(conn, g.ws, g.user):
            return redirect(url_for("studio"))
        trials = {p["code"]: plan_gate.trial_days(conn, g.ws, g.user, p["code"]) for p in plans.PAID}
        made = conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is not None
    terms = {code: plan_gate.terms(code, days) for code, days in trials.items()}
    return render_template("plan_pick.html", trials=trials, terms=terms, new=not made, owner=is_owner(),
                           any_trial=any(trials.values()), days=plan_gate.TRIAL_DAYS,
                           canceled=request.args.get("canceled") == "1", checkout_open=billing.configured())
