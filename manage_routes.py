"""The Campaigns page for launched campaigns (roadmap M2.4; admanage.py does the work).

    GET  /app/ad-campaigns                       every launch on every platform with its status, budget, results and
                                                 pending changes (Meta's are managed here; the others link out)
    GET  /app/ad-campaigns/<lid>                 one campaign: its ads, turn on or pause, the daily budget, the card
    GET  /app/ad-campaigns/<lid>.json            the state of a change being made (the page polls while it waits)
    POST /app/ad-campaigns/refresh               "Update now": pull every platform's numbers soon (adinsights.refresh)
    POST /app/ad-campaigns/<lid>/change          draft a change card: action=on|off|budget, target=campaign|<ad id>
    POST /app/ad-cards/<card_id>/approve         the owner approves the card shown (hash, confirm_big, confirm_ai)
    POST /app/ad-cards/<card_id>/discard         drop an undecided card (the owner, or who drafted it)

Behind AD_LAUNCH (adplatforms.enabled: off = 404, beta = AD_LAUNCH_WORKSPACES and admins). Not behind a plan
feature: pausing must stay possible after a downgrade; turning on and raising the budget check the paid plan on
the card (admanage.rules, D2). Members draft; only the owner (or an admin) approves, and an admin viewing another
workspace is read-only. Another workspace's launch or card is a 404. Approve sends only the card id and the hash
of what was shown; what is applied is the stored card, re-checked against Meta and the rules on the server.
"""
import logging

from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import adfatigue
import adinsights
import adleads
import admanage
import adspec
import db
import launches
import plans
import weekly_report
from core import is_owner, login_required

log = logging.getLogger("campaignify.manage")
bp = Blueprint("manage", __name__)

DRAFTS_PER_HOUR = 120
APPROVALS_PER_HOUR = 30


def _on():
    if not admanage.on(g.get("ws"), g.get("user")):
        abort(404)


def _operator() -> bool:
    return bool(g.get("user")) and bool(g.user["is_admin"])


def _owner_ok() -> bool:
    return is_owner() and not g.get("viewing")


def _launch(conn, lid):
    L = admanage.launch_of(conn, g.ws["id"], lid)
    if L is None:
        abort(404)
    return L


@bp.route("/app/ad-campaigns")
@login_required
def index():
    _on()
    key = request.args.get("range") if request.args.get("range") in adinsights.RANGES else "7"
    with db.connect() as conn:
        rows = admanage.listed(conn, g.ws["id"])
        for r in rows:
            tz = r["conn_row"]["timezone"] if r["conn_row"] else None
            r["fig"] = adinsights.launch_figures(conn, r["launch"], key, tz)
            r["tired"] = adfatigue.for_launch(conn, r["launch"], s=r)["tired"] if r["manageable"] else []  # M4.2
            r["leads"] = adleads.leads_for(conn, r["launch"], key, tz,             # our forms (M4.1)
                                          spend=r["fig"]["spend"] if r["fig"]["n"] else None)
        conn_row = launches.connection(conn, g.ws["id"])
        connected = conn_row is not None
        other_leads = adleads.unmatched(conn, g.ws["id"], key, conn_row["timezone"] if conn_row else None)
        totals = adinsights.workspace_totals(conn, g.ws["id"], key)
        ins = adinsights.status(conn, g.ws["id"])
    weekly_on = weekly_report.enabled() and not g.user["weekly_report_off"]
    return render_template("ad_campaigns.html", rows=rows, connected=connected, owner=_owner_ok(), totals=totals, ins=ins,
                           weekly_on=weekly_on, weekly_enabled=weekly_report.enabled(),
                           key=key, ranges=adinsights.RANGES, money=launches.money, other_leads=other_leads,
                           platform_words=adleads.PLATFORM_WORDS)


@bp.route("/app/ad-campaigns/refresh", methods=["POST"])
@login_required
def refresh():
    _on()
    if g.get("viewing"):
        abort(403, "Viewing as admin is read-only.")
    with db.connect() as conn:
        moved = adinsights.refresh(conn, g.ws["id"])
    flash("Getting the latest numbers from your ad accounts. They show here within a few minutes." if moved else
          "The numbers were updated in the last 15 minutes. The ad platforms update them every few hours.")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/app/ad-campaigns")
                    else url_for("manage.index"))


@bp.route("/app/ad-campaigns/weekly-report", methods=["POST"])
@login_required
def weekly_toggle():
    """The owner's weekly report email on or off (weekly_report.py, M4.4)."""
    _on()
    if g.get("viewing"):
        abort(403, "Viewing as admin is read-only.")
    off = request.form.get("weekly") != "on"
    with db.connect() as conn:
        weekly_report.set_off(conn, g.user["id"], off)
    flash("Weekly report emails are off." if off else "Weekly report emails are on. The next one comes on Monday "
          "when your campaigns have something to report.")
    return redirect(url_for("manage.index") + "#weekly")


@bp.route("/u/weekly/<token>", methods=["GET", "POST"])
def weekly_unsub(token):
    """One-click unsubscribe from the weekly report: the List-Unsubscribe-Post header or the button. No login."""
    with db.connect() as conn:
        u = weekly_report.user_of_token(conn, current_app.secret_key, token)
        if u is None:
            abort(404)
        done = bool(u["weekly_report_off"])
        if request.method == "POST":
            weekly_report.set_off(conn, u["id"], True)
            if not request.form.get("confirm"):      # mail apps' one-click POST
                return Response("Unsubscribed.", mimetype="text/plain")
            done = True
    return render_template("weekly_unsub.html", token=token, done=done)


@bp.route("/app/ad-campaigns/<int:lid>")
@login_required
def show(lid):
    _on()
    with db.connect() as conn:
        L = _launch(conn, lid)
        if L["status"] != "paused":
            return redirect(url_for("launch.status", lid=lid))
        sync_error = admanage.sync(conn, L) if not g.get("viewing") else None
        s = admanage.summary(conn, L)
        ads = [{"n": a["n"], "row": a["row"], "word": admanage.status_word(a["row"]),
                "name": admanage.ad_name(a["row"], a["n"], s["name"])} for a in s["objects"]["ads"]]
        kinds = admanage.ai_ads(conn, L)
        hist = admanage.history(conn, L)
        cap = launches.ceiling(g.ws, s["currency"], _operator())
        code = launches.launch_plan_code(g.ws, _operator())
        key = request.args.get("range") if request.args.get("range") in adinsights.RANGES else "7"
        tz = s["conn_row"]["timezone"] if s["conn_row"] else None
        fig = adinsights.launch_figures(conn, L, key, tz)
        ad_fig = adinsights.ad_figures(conn, L, key, tz)
        days = adinsights.daily(conn, L, 14, tz)
        ins = adinsights.status(conn, g.ws["id"])
        # leads on the member's own forms next to Meta's numbers, with cost per lead (adleads.py, M4.1)
        leads = adleads.leads_for(conn, L, key, tz, {a: f["spend"] for a, f in ad_fig.items()},
                                  fig["spend"] if fig["n"] else None)
        recent = adleads.recent(conn, L)
        # ads wearing out and the angle that's working (adfatigue.py, M4.2): a suggestion, never a change on Meta
        fat = adfatigue.for_launch(conn, L, s=s)
        if fat["tired"] and fat["winner"] is None:
            fat["winner"] = adfatigue._winner([a for _, x in adfatigue.workspace(conn, g.ws["id"]) for a in x["ads"]])
    return render_template("ad_campaign.html", s=s, L=L, ads=ads, ai_kinds=kinds, hist=hist, owner=_owner_ok(),
                           sync_error=sync_error, cap_text=launches.money(cap, s["currency"]) if cap else "",
                           plan_name=plans.PLANS.get(code, {}).get("name", ""), minor_factor=adspec.minor_factor(
                               s["currency"]), viewing=bool(g.get("viewing")), fig=fig, ad_fig=ad_fig, days=days, ins=ins,
                           key=key, ranges=adinsights.RANGES, money=launches.money, leads=leads, recent=recent, fat=fat,
                           tired={a["id"]: a for a in fat["tired"]}, fat_words=adfatigue.words)


@bp.route("/app/ad-campaigns/<int:lid>.json")
@login_required
def state_json(lid):
    _on()
    with db.connect() as conn:
        L = _launch(conn, lid)
        live = admanage.busy(conn, L)
    return jsonify({"busy": live is not None, "state": live["apply_state"] if live else None})


@bp.route("/app/ad-campaigns/<int:lid>/change", methods=["POST"])
@login_required
def change(lid):
    _on()
    if g.get("viewing"):
        abort(403, "Viewing as admin is read-only.")
    back = url_for("manage.show", lid=lid)
    with db.connect() as conn:
        L = _launch(conn, lid)
        if db.throttled(f"manage-draft:{g.ws['id']}", DRAFTS_PER_HOUR, 3600):
            flash("That's a lot of changes in an hour. Give it a few minutes.")
            return redirect(back)
        db.hit(f"manage-draft:{g.ws['id']}")
        action = request.form.get("action") or ""
        target = request.form.get("target") or "campaign"
        amount = None
        if action == "budget":
            conn_row = admanage.connection_of(conn, L)
            cur = (conn_row["currency"] if conn_row is not None else None) or "USD"
            try:
                amount = int(round(float((request.form.get("budget") or "").replace(",", "").strip())
                                   * adspec.minor_factor(cur)))
            except ValueError:
                flash("Type the new daily budget as a number.")
                return redirect(back + "#budget")
        try:
            admanage.draft(conn, g.ws, g.user, L, action, target, amount, _operator())
        except admanage.Refused as e:
            flash(str(e))
            return redirect(back)
    return redirect(back + "#card")


def _card(conn, card_id):
    row = conn.execute("SELECT * FROM approval_cards WHERE id=? AND workspace_id=? AND kind IN "
                       "('status_change','budget_change')", (card_id, g.ws["id"])).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/app/ad-cards/<int:card_id>/approve", methods=["POST"])
@login_required
def approve(card_id):
    _on()
    with db.connect() as conn:
        row = _card(conn, card_id)
        if not _owner_ok():
            abort(403, "Only the workspace owner can approve a change.")
        back = url_for("manage.show", lid=row["launch_id"])
        if db.throttled(f"manage-approve:{g.ws['id']}", APPROVALS_PER_HOUR, 3600):
            flash("Too many changes approved in an hour. Try again later.")
            return redirect(back + "#card")
        db.hit(f"manage-approve:{g.ws['id']}")
        try:
            state = admanage.approve(conn, g.ws, g.user, row, request.form.get("hash"),
                                     confirm_big=request.form.get("confirm_big") == "1",
                                     confirm_ai=request.form.get("confirm_ai") == "1", operator=_operator())
        except admanage.NotApproved as e:
            flash(str(e))
            return redirect(back + "#card")
    flash({"done": "Done. Meta has the change.", "waiting": "Approved. Meta allows one change per item every 30 "
           "seconds, so Campaignify finishes it in a moment.", "failed": "Meta refused the change. See the card below."
           }.get(state, "Approved. Campaignify is making the change on Meta."))
    return redirect(back + "#history")


@bp.route("/app/ad-cards/<int:card_id>/discard", methods=["POST"])
@login_required
def discard(card_id):
    _on()
    if g.get("viewing"):
        abort(403, "Viewing as admin is read-only.")
    with db.connect() as conn:
        row = _card(conn, card_id)
        if not (_owner_ok() or row["created_by"] == g.user["id"]):
            abort(403, "Only the owner or who asked for the change can drop it.")
        admanage.discard(conn, g.ws, g.user, row)
    flash("Dropped. Nothing was changed on Meta.")
    return redirect(url_for("manage.show", lid=row["launch_id"]))
