"""Copy studio (Starter+) and content optimizer (Growth+)."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import copywriter
import db
import llm
import platforms
from core import brand, can_afford, charge_ai, feature_required, not_enough, picked_product

bp = Blueprint("copy", __name__)


def _runs(kinds, limit=20):
    with db.connect() as conn:
        q = ",".join("?" * len(kinds))
        return conn.execute(f"SELECT * FROM copy_runs WHERE workspace_id=? AND kind IN ({q}) ORDER BY id DESC LIMIT ?",
                            (g.ws["id"], *kinds, limit)).fetchall()


def save_run(conn, kind, platform, inp, out, usage, product_id=None):
    cost = usage.usd if usage else 0.0
    credits = charge_ai(conn, usage.credits, f"AI {platforms.KINDS.get(kind, {}).get('name', kind).lower()}") \
        if usage and usage.calls else 0
    pid = product_id or g.ws["product_id"]
    prow = conn.execute("SELECT brand_id FROM products WHERE id=? AND workspace_id=?", (pid or 0, g.ws["id"])).fetchone()
    rid = conn.execute("INSERT INTO copy_runs (workspace_id, user_id, brand_id, kind, platform, input_json, output_json, "
                       "credits, cost_usd, created_at, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (g.ws["id"], g.user["id"], prow["brand_id"] if prow else g.ws["brand_id"], kind, platform,
                        json.dumps(inp), json.dumps(out), credits, round(cost, 5), db.now(),
                        pid if prow else None)).lastrowid
    conn.commit()
    return rid


@bp.route("/app/copy", methods=["GET", "POST"])
@feature_required("copy")
def studio():
    pid = picked_product() or g.ws["product_id"]
    b = brand(pid)
    form = request.form if request.method == "POST" else {}
    kind = form.get("kind") or request.args.get("kind") or "ad"
    platform = form.get("platform") or request.args.get("platform") or "meta"
    n = 5 if form.get("n") == "5" else 3
    if request.method == "POST":
        if kind not in platforms.KINDS or platform not in platforms.PLATFORMS:
            abort(400)
        if not llm.configured():
            flash("AI writing isn't switched on yet. Nothing was charged.")
            return redirect(url_for("copy.studio"))
        quote = copywriter.estimate(n, kind)
        if not can_afford(quote):
            not_enough(quote)
            return redirect(url_for("copy.studio", kind=kind, platform=platform))
        brief = {k: (form.get(k) or "").strip()[:lim] for k, lim in
                 (("offer", 300), ("audience", 200), ("goal", 20), ("tone", 40), ("link", 300), ("notes", 1000))}
        if kind == "email":
            brief["email_type"] = copywriter.email_type({"email_type": form.get("email_type")})
        check = copywriter.compliance.check(brief["offer"], brief["audience"], brief["notes"])
        if not check["ok"]:
            for m in check["blocks"]:
                flash(m)
            return redirect(url_for("copy.studio", kind=kind, platform=platform))
        usage = llm.Usage()
        try:
            variants = copywriter.write(b, kind, platform, brief, n, usage)
        except llm.LLMError as e:
            with db.connect() as conn:
                charge_ai(conn, usage.credits, "AI copy (failed run)") if usage.calls else None
                conn.commit()
            flash(f"The writer didn't finish ({e}). Try again in a minute.")
            return redirect(url_for("copy.studio", kind=kind, platform=platform))
        with db.connect() as conn:
            rid = save_run(conn, kind, platform, dict(brief, n=n), {"variants": variants}, usage, product_id=pid)
        return redirect(url_for("copy.run", rid=rid))
    etype = copywriter.email_type({"email_type": request.args.get("type")})
    return render_template("copy.html", b=b, pid=pid, kind=kind, platform=platform, runs=_runs(list(platforms.KINDS)),
                           ai_on=llm.configured(), email_types=platforms.EMAIL_TYPES, etype=etype,
                           quotes={k: [copywriter.estimate(3, k), copywriter.estimate(5, k)] for k in platforms.KINDS})


@bp.route("/app/copy/<int:rid>")
@feature_required("copy")
def run(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM copy_runs WHERE id=? AND workspace_id=?", (rid, g.ws["id"])).fetchone()
    if not r or r["kind"] == "optimize":
        abort(404)
    return render_template("copy_run.html", r=r, inp=json.loads(r["input_json"] or "{}"),
                           out=json.loads(r["output_json"] or "{}"))


@bp.route("/app/optimize", methods=["GET", "POST"])
@feature_required("optimizer")
def optimize():
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()[:8000]
        platform = request.form.get("platform") or "meta"
        goal = request.form.get("goal") or "sales"
        if platform not in platforms.PLATFORMS:
            abort(400)
        if len(text) < 10:
            flash("Paste the copy or post you want to improve.")
            return redirect(url_for("copy.optimize"))
        use_ai = request.form.get("ai") == "1" and llm.configured()
        quote = copywriter.optimize_estimate() if use_ai else 0
        if use_ai and not can_afford(quote):
            not_enough(quote)
            return redirect(url_for("copy.optimize"))
        usage = llm.Usage()
        try:
            res = copywriter.optimize(text, platform, goal, brand(), use_ai=use_ai, usage=usage)
        except llm.LLMError as e:
            res = copywriter.optimize(text, platform, goal, brand(), use_ai=False)
            flash(f"The AI review didn't finish ({e}); here are the quick checks.")
        with db.connect() as conn:
            rid = save_run(conn, "optimize", platform, {"text": text, "goal": goal}, res, usage)
        return redirect(url_for("copy.optimized", rid=rid))
    return render_template("optimize.html", runs=_runs(["optimize"]), ai_on=llm.configured(),
                           quote=copywriter.optimize_estimate(), text=request.args.get("text", "")[:8000],
                           platform=request.args.get("platform") or "meta")


@bp.route("/app/optimize/<int:rid>")
@feature_required("optimizer")
def optimized(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM copy_runs WHERE id=? AND workspace_id=? AND kind='optimize'",
                         (rid, g.ws["id"])).fetchone()
    if not r:
        abort(404)
    return render_template("optimize_run.html", r=r, inp=json.loads(r["input_json"] or "{}"),
                           res=json.loads(r["output_json"] or "{}"))
