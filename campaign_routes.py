"""AI campaign planner pages (Starter+): brief -> live plan -> confirm ads."""
import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import catalog
import compliance
import db
import llm
import planner
import platforms
from core import brand, can_afford, feature_required, picked_product, queue_job

bp = Blueprint("campaigns", __name__)
MAX_RUNNING = 2   # planning runs at once per workspace


def _get(cid):
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
    if not c:
        abort(404)
    return _unstale(c)


def _unstale(c):
    """A run whose thread died (deploy, restart) shows as failed instead of spinning forever."""
    if c["status"] != "planning":
        return c
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=planner.STALE_MINUTES)).replace(microsecond=0).isoformat()
    if c["updated_at"] < cutoff:
        with db.connect() as conn:
            conn.execute("UPDATE campaigns SET status=?, error='The planning run was interrupted. Revise to run it "
                         "again.' WHERE id=? AND status='planning'",
                         ("ready" if json.loads(c["plan_json"] or "{}").get("angles") else "failed", c["id"]))
            conn.commit()
            c = conn.execute("SELECT * FROM campaigns WHERE id=?", (c["id"],)).fetchone()
    return c


def _ready_to_plan():
    if not llm.anthropic_ready():
        flash("The campaign planner isn't switched on yet. Nothing was charged.")
        return False
    if not can_afford(planner.cap_credits()):
        flash(f"Planning can use up to {planner.cap_credits()} credits (you pay what it actually uses) and you have "
              f"{db.balance(g.ws):,}. Top up on the Billing page.")
        return False
    with db.connect() as conn:
        running = conn.execute("SELECT COUNT(*) FROM campaigns WHERE workspace_id=? AND status='planning' AND updated_at>?",
                               (g.ws["id"], (datetime.now(timezone.utc) - timedelta(minutes=planner.STALE_MINUTES))
                                .replace(microsecond=0).isoformat())).fetchone()[0]
    if running >= MAX_RUNNING:
        flash("Two plans are already being made. Give them a minute.")
        return False
    return True


@bp.route("/app/campaigns", methods=["GET", "POST"])
@feature_required("planner")
def index():
    pid = picked_product() or g.ws["product_id"]
    if request.method == "POST":
        f = request.form
        brief = {k: (f.get(k) or "").strip()[:lim] for k, lim in
                 (("offer", 300), ("audience", 200), ("goal", 20), ("budget", 80), ("dates", 120), ("tone", 40),
                  ("link", 300), ("notes", 1500))}
        brief["platforms"] = [p for p in f.getlist("platforms") if p in platforms.PLATFORMS][:6]
        if not brief["offer"]:
            flash("Say what you're promoting.")
            return redirect(url_for("campaigns.index"))
        chk = compliance.check(brief["offer"], brief["audience"], brief["notes"])
        if not chk["ok"]:
            for m in chk["blocks"]:
                flash(m)
            return redirect(url_for("campaigns.index"))
        if not _ready_to_plan():
            return redirect(url_for("campaigns.index"))
        with db.connect() as conn:
            prow = conn.execute("SELECT * FROM products WHERE id=? AND workspace_id=?", (pid or 0, g.ws["id"])).fetchone()
            cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, brand_id, name, brief_json, status, plan_json, "
                               "steps_json, created_at, updated_at, product_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (g.ws["id"], g.user["id"], prow["brand_id"] if prow else g.ws["brand_id"],
                                brief["offer"][:80], json.dumps(brief), "planning", "{}", "[]", db.now(), db.now(),
                                prow["id"] if prow else None)).lastrowid
            conn.commit()
        planner.start(cid)
        return redirect(url_for("campaigns.show", cid=cid))
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC LIMIT 50", (g.ws["id"],)).fetchall()
    return render_template("campaigns.html", campaigns=[_unstale(c) for c in rows], b=brand(pid), pid=pid,
                           cap=planner.cap_credits(), ai_on=llm.anthropic_ready())


def _ads(cid):
    with db.connect() as conn:
        ads = conn.execute("SELECT * FROM campaign_ads WHERE campaign_id=? AND workspace_id=? ORDER BY id",
                           (cid, g.ws["id"])).fetchall()
        jobs = {}
        ids = [a["job_id"] for a in ads if a["job_id"]]
        if ids:
            for j in conn.execute(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids):
                jobs[j["id"]] = j
            outs = {}
            for o in conn.execute(f"SELECT * FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
                outs.setdefault(o["job_id"], []).append(o)
        else:
            outs = {}
        photos = conn.execute("SELECT COUNT(*) FROM photos WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0]
    return ads, jobs, outs, photos


@bp.route("/app/campaigns/<int:cid>")
@feature_required("planner")
def show(cid):
    c = _get(cid)
    ads, jobs, outs, photos = _ads(cid)
    return render_template("campaign.html", c=c, brief=json.loads(c["brief_json"] or "{}"),
                           plan=json.loads(c["plan_json"] or "{}"), steps=json.loads(c["steps_json"] or "[]"),
                           ads=ads, jobs=jobs, outs=outs, photos=photos, templates=catalog.TEMPLATE_BY_ID,
                           ai_on=llm.anthropic_ready(), cap=planner.cap_credits())


@bp.route("/app/campaigns/<int:cid>/status")
@feature_required("planner")
def status(cid):
    c = _get(cid)
    return jsonify({"status": c["status"], "steps": len(json.loads(c["steps_json"] or "[]"))})


@bp.route("/app/campaigns/<int:cid>/revise", methods=["POST"])
@feature_required("planner")
def revise(cid):
    c = _get(cid)
    if c["status"] == "planning":
        flash("It's still planning. Revise once it's done.")
        return redirect(url_for("campaigns.show", cid=cid))
    notes = (request.form.get("notes") or "").strip()[:1500]
    chk = compliance.check(notes)
    if not chk["ok"]:
        for m in chk["blocks"]:
            flash(m)
        return redirect(url_for("campaigns.show", cid=cid))
    if not _ready_to_plan():
        return redirect(url_for("campaigns.show", cid=cid))
    with db.connect() as conn:
        conn.execute("DELETE FROM campaign_ads WHERE campaign_id=? AND status='proposed'", (cid,))
        conn.execute("UPDATE campaigns SET status='planning', error=NULL, updated_at=? WHERE id=?", (db.now(), cid))
        conn.commit()
    planner.start(cid, notes or "Make it better.")
    return redirect(url_for("campaigns.show", cid=cid))


@bp.route("/app/campaigns/<int:cid>/ads/<int:aid>", methods=["POST"])
@feature_required("planner")
def edit_ad(cid, aid):
    _get(cid)
    text = (request.form.get("brief") or "").strip()[:2000]
    chk = compliance.check(text)
    if not text or not chk["ok"]:
        flash("Not saved: " + (" ".join(chk["blocks"]) or "the brief is empty."))
        return redirect(url_for("campaigns.show", cid=cid) + f"#ad{aid}")
    with db.connect() as conn:
        conn.execute("UPDATE campaign_ads SET brief=? WHERE id=? AND campaign_id=? AND workspace_id=? AND status='proposed'",
                     (text, aid, cid, g.ws["id"]))
        conn.commit()
    flash("Brief updated.")
    return redirect(url_for("campaigns.show", cid=cid) + f"#ad{aid}")


@bp.route("/app/campaigns/<int:cid>/generate", methods=["POST"])
@feature_required("planner")
def generate(cid):
    """Queue the ads the member picked. The page showed the total; we recompute
    it and refuse if it changed, so nobody is charged more than they saw."""
    c = _get(cid)
    ids = [int(x) for x in request.form.getlist("ad") if x.isdigit()]
    with db.connect() as conn:
        ads = [a for a in conn.execute("SELECT * FROM campaign_ads WHERE campaign_id=? AND workspace_id=? AND status='proposed'",
                                       (cid, g.ws["id"])).fetchall() if a["id"] in ids]
        photos = {r["id"] for r in conn.execute("SELECT id FROM photos WHERE workspace_id=?", (g.ws["id"],))}
        own = [r["id"] for r in conn.execute("SELECT id FROM photos WHERE product_id=? ORDER BY id DESC", (c["product_id"] or 0,))]
        if not ads:
            flash("Pick at least one ad.")
            return redirect(url_for("campaigns.show", cid=cid) + "#ads")
        first_photo = own[0] if own else (max(photos) if photos else None)
        total = sum(a["credits"] for a in ads)
        if str(total) != (request.form.get("total") or ""):
            flash(f"The total changed to {total:,} credits. Check it and confirm again.")
            return redirect(url_for("campaigns.show", cid=cid) + "#ads")
        if db.balance(g.ws) < total:
            flash(f"Those ads need {total:,} credits and you have {db.balance(g.ws):,}. Pick fewer or top up on Billing.")
            return redirect(url_for("campaigns.show", cid=cid) + "#ads")
        queued = 0
        for a in ads:
            params = json.loads(a["params_json"] or "{}")
            params["photo_ids"] = [p for p in params.get("photo_ids") or [] if p in photos]
            if not params["photo_ids"] and first_photo and a["tool"] != "ad_image":
                params["photo_ids"] = [first_photo]
            if a["tool"] != "ad_image" and not params["photo_ids"]:
                continue
            chk = compliance.check(a["brief"] or "")
            if not chk["ok"]:
                continue
            jid = queue_job(conn, a["tool"], a["template_id"], a["brief"], params, a["cost_usd"], a["credits"], quiet=True,
                            product_id=c["product_id"])
            if not jid:
                break
            conn.execute("UPDATE campaign_ads SET status='queued', job_id=? WHERE id=?", (jid, a["id"]))
            conn.commit()
            queued += 1
    skipped = len(ads) - queued
    flash(f"Started {queued} ad{'s' if queued != 1 else ''}. They show up here and in your library in a few minutes."
          + (f" {skipped} couldn't start (they need a product photo, or the brief needs fixing)." if skipped else ""))
    return redirect(url_for("campaigns.show", cid=cid) + "#ads")


@bp.route("/app/campaigns/<int:cid>/delete", methods=["POST"])
@feature_required("planner")
def delete(cid):
    _get(cid)
    with db.connect() as conn:
        conn.execute("DELETE FROM campaign_ads WHERE campaign_id=? AND workspace_id=?", (cid, g.ws["id"]))
        conn.execute("DELETE FROM campaigns WHERE id=? AND workspace_id=?", (cid, g.ws["id"]))
        conn.commit()
    flash("Campaign deleted. Ads you generated stay in your library.")
    return redirect(url_for("campaigns.index"))
