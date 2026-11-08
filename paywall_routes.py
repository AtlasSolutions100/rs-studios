"""The out-of-credits paywall pages (plan_paywall.py, RS_PLAN_PAYWALL): the free
campaign plan with competitor research (1 per 30 days), its part-locked plan
page, the lock banner on every app page and the lock page for anything else
that costs credits."""
import json
import logging
import os
import threading
import traceback
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import compliance
import competitors
import db
import llm
import plan_paywall
import planner
import platforms
from core import brand, hold_check, login_required

log = logging.getLogger("campaignify")
bp = Blueprint("paywall", __name__)
STALE_MINUTES = 40        # research plus planning; a run older than this died with its thread
GOALS = ("sales", "leads", "traffic", "awareness", "engagement", "installs")


def _wants_json() -> bool:
    return (request.is_json or request.accept_mimetypes.best == "application/json"
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/"))


def _plans_url() -> str:
    return url_for("billing_page") + "#plans"


@bp.app_errorhandler(plan_paywall.PaywallLocked)
def locked_error(e):
    if _wants_json():
        return jsonify({"error": plan_paywall.MESSAGE, "paywall": True, "plans": _plans_url()}), 402
    with db.connect() as conn:
        free_ok = g.get("ws") is not None and plan_paywall.available(conn, g.ws)
    return render_template("paywall_locked.html", free_ok=free_ok), 402


@bp.app_context_processor
def _banner():
    """The lock on every app page while the workspace is out of credits with no subscription."""
    if not plan_paywall.on() or g.get("ws") is None or g.get("user") is None or g.get("viewing") is not None:
        return {}
    if request.endpoint in (None, "static") or (request.endpoint or "").startswith(("paywall.", "card.")):
        return {}
    if not plan_paywall.applies(g.ws):
        return {}
    with db.connect() as conn:
        free_ok = plan_paywall.available(conn, g.ws)
    return {"paywall_banner": {"message": plan_paywall.MESSAGE, "plans": _plans_url(),
                               "free_plan": url_for("paywall.new") if free_ok else None}}


@bp.before_app_request
def _planner_door():
    """Out of credits and on Free: the planner's door opens on the free plan while it's available."""
    if request.method != "GET" or request.endpoint != "campaigns.index" or not plan_paywall.on():
        return None
    if g.get("ws") is None or g.get("viewing") is not None:
        return None
    with db.connect() as conn:
        if plan_paywall.available(conn, g.ws):
            return redirect(url_for("paywall.new"))
    return None


def _campaign(cid):
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=? AND free_plan=1",
                         (cid, g.ws["id"])).fetchone()
    if c is None:
        abort(404)
    if c["status"] == "planning":
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)).replace(microsecond=0).isoformat()
        if c["updated_at"] < cutoff:
            with db.connect() as conn:
                conn.execute("UPDATE campaigns SET status='failed', error='The planning run was interrupted.' "
                             "WHERE id=? AND status='planning'", (cid,))
                claim = conn.execute("SELECT id FROM free_plans WHERE campaign_id=?", (cid,)).fetchone()
                conn.commit()
                c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            if claim and not json.loads(c["plan_json"] or "{}").get("angles"):
                plan_paywall.give_back(claim["id"])
    return c


@bp.route("/app/free-plan", methods=["GET", "POST"])
@login_required
def new():
    if not plan_paywall.on():
        abort(404)
    if plan_paywall.subscribed(g.ws):
        return redirect(url_for("campaigns.index"))
    with db.connect() as conn:
        next_on = plan_paywall.next_free_on(conn, g.ws["id"])
        last = plan_paywall.last_free(conn, g.ws["id"])
    if request.method == "POST":
        hold_check()
        if not plan_paywall.applies(g.ws):
            flash("The free campaign plan is for accounts that are out of credits. Use your credits first, "
                  "or pick a plan to use the planner any time.")
            return redirect(url_for("paywall.new"))
        if not llm.anthropic_ready():
            flash("The campaign planner isn't switched on yet. Nothing was charged.")
            return redirect(url_for("paywall.new"))
        f = request.form
        brief = {k: (f.get(k) or "").strip()[:lim] for k, lim in
                 (("offer", 300), ("audience", 200), ("goal", 20), ("budget", 80), ("dates", 120), ("tone", 40),
                  ("link", 300), ("notes", 1500))}
        if brief["goal"] not in GOALS:
            brief["goal"] = "sales"
        brief["platforms"] = [p for p in f.getlist("platforms") if p in platforms.PLATFORMS][:6]
        if not brief["offer"]:
            flash("Say what you're promoting.")
            return redirect(url_for("paywall.new"))
        chk = compliance.check(brief["offer"], brief["audience"], brief["notes"])
        if not chk["ok"]:
            for m in chk["blocks"]:
                flash(m)
            return redirect(url_for("paywall.new"))
        with db.connect() as conn:
            claim = plan_paywall.claim(conn, g.ws["id"])
            if claim is None:
                conn.rollback()
                flash(f"You've made your free plan for this month. The next one opens on "
                      f"{plan_paywall.next_free_on(conn, g.ws['id'])}.")
                return redirect(url_for("paywall.new"))
            bid = g.ws["brand_id"]
            pid = g.ws["product_id"]
            cur = competitors.current(conn, g.ws["id"], bid)
            fresh = cur is not None and cur["created_at"] > (datetime.now(timezone.utc) - timedelta(
                days=plan_paywall.FREE_PLAN_DAYS)).replace(microsecond=0).isoformat()
            rid = None
            if not fresh:
                quote = llm.credits_for(competitors.quote_usd(plan_paywall.FREE_COMPETITORS, True))
                rid = conn.execute("INSERT INTO competitor_reports (workspace_id, brand_id, user_id, status, inputs_json, "
                                   "steps_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                   (g.ws["id"], bid, g.user["id"], "running",
                                    json.dumps({"competitors": [], "quote": quote, "free": True}), "[]", db.now(),
                                    db.now())).lastrowid
            cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, brand_id, name, brief_json, status, "
                               "plan_json, steps_json, created_at, updated_at, product_id, free_plan) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                               (g.ws["id"], g.user["id"], bid, brief["offer"][:80], json.dumps(brief), "planning",
                                "{}", "[]", db.now(), db.now(), pid)).lastrowid
            conn.execute("UPDATE free_plans SET campaign_id=?, report_id=? WHERE id=?",
                         (cid, rid or (cur["id"] if cur is not None else None), claim))
            conn.commit()
        start(claim, cid, rid)
        return redirect(url_for("paywall.show", cid=cid))
    if last is not None and last["campaign_id"]:
        made = last["campaign_id"]
    else:
        made = None
    return render_template("free_plan_new.html", b=brand(), next_on=next_on, made=made,
                           out=plan_paywall.out_of_credits(g.ws), ai_on=llm.anthropic_ready())


def run(claim: int, cid: int, rid: int | None) -> str:
    """The free plan: competitor research (when the brand has none from the last 30 days), then the
    planner, neither charged. A plan that produced nothing gives the month's free plan back."""
    status = "failed"
    try:
        if rid:
            competitors.run(rid)
            with db.connect() as conn:
                conn.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (db.now(), cid))
                conn.commit()
        status = planner.run(cid, hold=None, free=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        with db.connect() as conn:
            conn.execute("UPDATE campaigns SET status='failed', error='planner crashed', updated_at=? WHERE id=?",
                         (db.now(), cid))
            conn.commit()
    if status != "ready":
        plan_paywall.give_back(claim)
    return status


def start(claim, cid, rid):
    if os.environ.get("RS_WORKER", "1") != "1":
        return run(claim, cid, rid)
    threading.Thread(target=run, args=(claim, cid, rid), name=f"free-plan-{cid}", daemon=True).start()
    return "planning"


@bp.route("/app/free-plan/<int:cid>")
@login_required
def show(cid):
    c = _campaign(cid)
    if not plan_paywall.locked(c, g.ws):
        return redirect(url_for("campaigns.show", cid=cid))   # a subscription unlocks the whole plan
    plan = json.loads(c["plan_json"] or "{}")
    with db.connect() as conn:
        report = plan_paywall.report_for(conn, c)
        next_on = plan_paywall.next_free_on(conn, g.ws["id"])
    part = plan_paywall.partial(plan, report)
    return render_template("free_plan.html", c=c, brief=json.loads(c["brief_json"] or "{}"), part=part,
                           steps=json.loads(c["steps_json"] or "[]"), next_on=next_on,
                           platform_name=lambda k: (platforms.PLATFORMS.get(k) or {}).get("name", k))


@bp.route("/app/free-plan/<int:cid>/status")
@login_required
def status(cid):
    c = _campaign(cid)
    return jsonify({"status": c["status"], "steps": len(json.loads(c["steps_json"] or "[]"))})
