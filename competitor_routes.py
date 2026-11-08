"""Competitor Snapshot pages: up to 5 competitors (or let it find them) ->
a live run -> the sourced report, as a page or Markdown."""
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

import competitors
import credits as credit_holds
import db
import llm
from core import can_afford, feature_required, reserve_ai

bp = Blueprint("competitors", __name__)


def _unstale(r):
    """A run whose thread died (deploy, restart) shows as failed; it was never charged
    and the credits held for it go back."""
    if r is None or r["status"] != "running":
        return r
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=competitors.STALE_MINUTES)).replace(microsecond=0).isoformat()
    if r["updated_at"] < cutoff:
        with db.connect() as conn:
            gone = conn.execute("UPDATE competitor_reports SET status='failed', error='The research was interrupted. "
                                "Nothing was charged. Run it again.' WHERE id=? AND status='running'", (r["id"],)).rowcount
            conn.commit()
            r = conn.execute("SELECT * FROM competitor_reports WHERE id=?", (r["id"],)).fetchone()
        if gone:
            credit_holds.release_ref(f"competitor:{r['id']}", "the research was interrupted")
    return r


def _get(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM competitor_reports WHERE id=? AND workspace_id=?", (rid, g.ws["id"])).fetchone()
    if r is None:
        abort(404)
    return _unstale(r)


def _free_lock(r):
    """Research made for a free campaign plan (plan_paywall.py) opens in full with a subscription."""
    import plan_paywall
    with db.connect() as conn:
        if plan_paywall.report_locked(conn, r, g.ws):
            raise plan_paywall.PaywallLocked()


@bp.route("/app/competitors", methods=["GET", "POST"])
@feature_required("competitors")
def index():
    bid = g.ws["brand_id"]
    if request.method == "POST":
        comps = competitors.parse_inputs(request.form.getlist("competitor"))
        need = competitors.quote(len(comps))
        if not llm.anthropic_ready():
            flash("Competitor research isn't switched on yet. Nothing was charged.")
            return redirect(url_for("competitors.index"))
        shown = request.form.get("quote") or ""
        if shown and (not shown.isdigit() or int(shown) < need):
            flash(f"The most this run can cost is now {need:,} credits. Check it and run it again.")
            return redirect(url_for("competitors.index"))
        if not can_afford(need):
            import plan_paywall   # out of credits with no subscription: the lock (RS_PLAN_PAYWALL)
            plan_paywall.check(g.ws)
            flash(f"This run can use up to {need:,} credits (you pay what it actually uses) and you have "
                  f"{db.balance(g.ws):,}. Top up on the Billing page.")
            return redirect(url_for("competitors.index"))
        with db.connect() as conn:
            busy = [_unstale(r) for r in conn.execute("SELECT * FROM competitor_reports WHERE workspace_id=? AND "
                                                      "status='running'", (g.ws["id"],)).fetchall()]
            if any(r["status"] == "running" for r in busy):
                flash("A competitor run is already going. Give it a few minutes.")
                return redirect(url_for("competitors.index"))
            # The most it can cost leaves the balance now (credits.py), so nothing else can spend it meanwhile.
            hold = reserve_ai(need, "Competitor research", conn=conn)
            if hold is None:
                flash(f"This run can use up to {need:,} credits (you pay what it actually uses) and you have "
                      f"{db.balance(conn.execute('SELECT * FROM workspaces WHERE id=?', (g.ws['id'],)).fetchone()):,}. "
                      "Top up on the Billing page.")
                return redirect(url_for("competitors.index"))
            rid = conn.execute("INSERT INTO competitor_reports (workspace_id, brand_id, user_id, status, inputs_json, "
                               "steps_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                               (g.ws["id"], bid, g.user["id"], "running",
                                json.dumps({"competitors": comps, "quote": need, "hold": hold.id}), "[]", db.now(),
                                db.now())).lastrowid
            conn.commit()
        credit_holds.link(hold, f"competitor:{rid}")
        competitors.start(rid)
        return redirect(url_for("competitors.show", rid=rid))
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM competitor_reports WHERE workspace_id=? AND brand_id IS ? ORDER BY id DESC "
                            "LIMIT 20", (g.ws["id"], bid)).fetchall()
        cur = competitors.current(conn, g.ws["id"], bid)
    return render_template("competitors.html", runs=[_unstale(r) for r in rows], current=cur, quotes=competitors.quotes(),
                           ai_on=llm.anthropic_ready(), max_n=competitors.MAX_COMPETITORS,
                           brand=g.get("brand_row"), caps={"searches": competitors.SEARCHES,
                                                           "fetches": competitors.FETCHES})


@bp.route("/app/competitors/<int:rid>")
@feature_required("competitors")
def show(rid):
    r = _get(rid)
    _free_lock(r)
    rep = json.loads(r["report_json"] or "{}") if r["status"] == "ready" else {}
    return render_template("competitor_report.html", r=r, rep=rep, inputs=json.loads(r["inputs_json"] or "{}"),
                           steps=json.loads(r["steps_json"] or "[]"), sources=json.loads(r["sources_json"] or "[]"),
                           sections=competitors.SECTIONS, synth_sections=competitors.SYNTH_SECTIONS,
                           names=competitors.SECTION_NAMES, markdown=competitors.to_markdown(r) if rep else "")


@bp.route("/app/competitors/<int:rid>/status")
@feature_required("competitors")
def status(rid):
    r = _get(rid)
    return jsonify({"status": r["status"], "steps": len(json.loads(r["steps_json"] or "[]"))})


@bp.route("/app/competitors/<int:rid>.md")
@feature_required("competitors")
def markdown(rid):
    r = _get(rid)
    _free_lock(r)
    if r["status"] != "ready":
        abort(404)
    return Response(competitors.to_markdown(r), mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="competitor-snapshot-{rid}.md"'})
