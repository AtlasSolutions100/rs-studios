"""SEO / AEO / GEO optimizer (Growth+)."""
import json
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import db
import fetcher
import llm
import seo
from core import brand, can_afford, charge_ai, feature_required, not_enough, picked_product

bp = Blueprint("seo", __name__)
FETCHES_PER_HOUR = 30


@bp.route("/app/seo", methods=["GET", "POST"])
@feature_required("seo")
def index():
    if request.method == "POST":
        f = request.form
        mode = "text" if f.get("mode") == "text" else "url"
        url = (f.get("url") or "").strip()[:500] if mode == "url" else None
        text = (f.get("text") or "").strip()[:30000] if mode == "text" else None
        goal = (f.get("goal") or "").strip()[:200]
        if mode == "url" and not url or mode == "text" and len(text or "") < 50:
            flash("Add a page URL, or paste at least a paragraph.")
            return redirect(url_for("seo.index"))
        key = f"seo:{g.ws['id']}"
        if mode == "url":
            if db.throttled(key, FETCHES_PER_HOUR, 3600):
                flash("That's a lot of audits this hour. Try again in a bit.")
                return redirect(url_for("seo.index"))
            db.hit(key)
        use_ai = f.get("ai") == "1" and llm.configured()
        if use_ai and not can_afford(seo.suggest_estimate()):
            not_enough(seo.suggest_estimate())
            return redirect(url_for("seo.index"))
        try:
            page, report, extras = seo.run_audit(url, text)
        except fetcher.FetchError as e:
            flash(f"Couldn't read that page: {e}")
            return redirect(url_for("seo.index"))
        b = brand(picked_product())
        usage = llm.Usage()
        sug = None
        if use_ai:
            try:
                sug = seo.suggest(page, report, b, goal, usage)
            except llm.LLMError as e:
                flash(f"The AI suggestions didn't finish ({e}); here's the audit.")
        out = {"page": {k: page[k] for k in ("url", "title", "description", "canonical", "lang", "words", "images",
                                             "images_no_alt", "jsonld_types", "internal_links", "external_links")},
               "headings": page["headings"][:40], "report": report, "extras": extras, "suggest": sug,
               "jsonld": seo.jsonld(page, sug, b), "llms_txt": seo.llms_txt(page, sug, b)}
        with db.connect() as conn:
            credits = charge_ai(conn, usage.credits, "AI SEO suggestions") if usage.calls else 0
            rid = conn.execute("INSERT INTO copy_runs (workspace_id, user_id, brand_id, kind, platform, input_json, "
                               "output_json, credits, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (g.ws["id"], g.user["id"], g.ws["brand_id"], "seo", None,
                                json.dumps({"mode": mode, "url": url or "", "text": (text or "")[:4000], "goal": goal}),
                                json.dumps(out), credits, round(usage.usd, 5), db.now())).lastrowid
            conn.commit()
        return redirect(url_for("seo.result", rid=rid))
    with db.connect() as conn:
        runs = conn.execute("SELECT * FROM copy_runs WHERE workspace_id=? AND kind='seo' ORDER BY id DESC LIMIT 20",
                            (g.ws["id"],)).fetchall()
    return render_template("seo.html", runs=runs, ai_on=llm.configured(), quote=seo.suggest_estimate())


@bp.route("/app/seo/<int:rid>")
@feature_required("seo")
def result(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM copy_runs WHERE id=? AND workspace_id=? AND kind='seo'", (rid, g.ws["id"])).fetchone()
    if not r:
        abort(404)
    return render_template("seo_result.html", r=r, inp=json.loads(r["input_json"] or "{}"),
                           out=json.loads(r["output_json"] or "{}"))
