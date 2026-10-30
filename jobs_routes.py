"""The results loop: try a failed ad again, approve or reject, delete, details,
and the live card updates the batch and library pages poll for.

Retrying makes a new job with the same settings at today's price (shown before
it runs, and refused if it changed); the old one stays, linked by
parent_job_id. Deleting is soft (jobs.deleted_at): the ledger keeps its rows.
"""
import json
import statistics

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template_string, request, url_for

import catalog
import db
from core import login_required, queue_job

bp = Blueprint("jobs", __name__)
VERDICTS = ("approved", "rejected")


def _job(conn, jid):
    j = conn.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=? AND deleted_at IS NULL", (jid, g.ws["id"])).fetchone()
    if not j:
        abort(404)
    return j


def price_now(conn, job) -> tuple[int, float]:
    """What the same job costs today (credits, vendor USD)."""
    p = json.loads(job["params_json"] or "{}")
    tool = job["tool"]
    if tool == "ad_image":
        kw = {"batch": int(p.get("batch") or 1)}
    elif tool in ("photo_to_video", "ugc_video"):
        kw = {"duration": int(p.get("duration") or (10 if tool == "ugc_video" else 5))}
        if p.get("resolution") in catalog.resolutions(tool):
            kw["resolution"] = p["resolution"]
    elif tool == "motion_copy":
        ref = conn.execute("SELECT seconds FROM refs WHERE id=?", (p.get("ref_id"),)).fetchone()
        kw = {"ref_seconds": (ref["seconds"] if ref else 0) or 15}
    else:
        return job["credits"], job["cost_usd"]
    return catalog.price_credits(tool, **kw), catalog.cost_usd(tool, **kw)


def hd_offer(job) -> dict | None:
    """For a finished 720p video: what re-rendering it in 1080p (and, when the
    flag is on, upscaling it) costs. None when it doesn't apply."""
    if job["status"] != "done" or job["tool"] not in ("ugc_video", "photo_to_video"):
        return None
    p = json.loads(job["params_json"] or "{}")
    if p.get("preview") or (p.get("resolution") or "720p") != "720p":
        return None
    dur = int(p.get("duration") or 5)
    out = {"rerender": catalog.price_credits(job["tool"], duration=dur, resolution="1080p"),
           "delta": catalog.hd_delta(job["tool"], dur), "upscale": None}
    if catalog.upscale_on():
        out["upscale"] = catalog.price_credits("video_upscale", duration=dur)
    return out


def retryable(job) -> bool:
    p = json.loads(job["params_json"] or "{}")
    return job["status"] == "failed" and not p.get("preview")


def _back(default):
    nxt = request.form.get("back") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)


def _wants_json():
    return request.accept_mimetypes.best == "application/json" or request.is_json


@bp.route("/app/jobs/<int:jid>/retry", methods=["POST"])
@login_required
def retry(jid):
    with db.connect() as conn:
        j = _job(conn, jid)
        if not retryable(j):
            abort(400)
        credits, usd = price_now(conn, j)
        if str(request.form.get("total") or (request.get_json(silent=True) or {}).get("total")) != str(credits):
            msg = f"The price is now {credits:,} credits. Check it and try again."
            if _wants_json():
                return jsonify({"error": msg, "total": credits}), 409
            flash(msg)
            return _back(url_for("library"))
        params = json.loads(j["params_json"] or "{}")
        # The failed card makes way, and claiming it first (in the same transaction
        # as the charge) means a double click or a second tab can't retry it twice.
        if conn.execute("UPDATE jobs SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (db.now(), j["id"])).rowcount != 1:
            conn.rollback()
            if _wants_json():
                return jsonify({"error": "It's already being tried again."}), 409
            return _back(url_for("library"))
        new = queue_job(conn, j["tool"], j["template_id"], j["brief"], params, usd, credits, quiet=_wants_json(),
                        product_id=j["product_id"], batch_id=j["batch_id"])
        if not new:      # queue_job rolled back, so the failed card is back too
            if _wants_json() and g.get("verify_blocked"):
                return jsonify({"error": g.verify_blocked, "verify": True}), 403
            if _wants_json():
                return jsonify({"error": f"That needs {credits:,} credits and you have {db.balance(g.ws):,}.",
                                "billing": url_for("billing_page")}), 402
            return _back(url_for("library"))
        conn.execute("UPDATE jobs SET parent_job_id=? WHERE id=?", (j["id"], new))
        conn.commit()
    if _wants_json():
        return jsonify({"ok": True, "job": new})
    flash(f"Trying again for {credits:,} credits.", "ok")
    return _back(url_for("create.batch", bid=j["batch_id"]) if j["batch_id"] else url_for("library", highlight=new))


@bp.route("/app/jobs/<int:jid>/hd", methods=["POST"])
@login_required
def hd(jid):
    """Re-render a finished 720p video in 1080p (same settings, a new linked
    job), or upscale it when that's switched on. Price shown and checked."""
    how = request.form.get("how") or (request.get_json(silent=True) or {}).get("how") or "rerender"
    with db.connect() as conn:
        j = _job(conn, jid)
        offer = hd_offer(j)
        if not offer or (how == "upscale" and not offer["upscale"]):
            abort(400)
        params = json.loads(j["params_json"] or "{}")
        dur = int(params.get("duration") or 5)
        if how == "upscale":
            tool, credits, usd = "video_upscale", offer["upscale"], catalog.cost_usd("video_upscale", duration=dur)
            params = {"source_job": j["id"], "duration": dur, "label": (params.get("label") or "") + " · upscaled 1080p",
                      "hook": params.get("hook") or "", "resolution": "1080p"}
        else:
            tool, credits = j["tool"], offer["rerender"]
            usd = catalog.cost_usd(tool, duration=dur, resolution="1080p")
            label = (params.get("label") or "").replace(" · 1080p", "")
            params = dict(params, resolution="1080p", label=(label + " · 1080p").lstrip(" ·"))
        if str(request.form.get("total") or (request.get_json(silent=True) or {}).get("total")) != str(credits):
            msg = f"The price is now {credits:,} credits. Check it and try again."
            if _wants_json():
                return jsonify({"error": msg, "total": credits}), 409
            flash(msg)
            return _back(url_for("library"))
        new = queue_job(conn, tool, j["template_id"], j["brief"], params, usd, credits, quiet=_wants_json(),
                        product_id=j["product_id"], batch_id=j["batch_id"])
        if not new:
            if _wants_json() and g.get("verify_blocked"):
                return jsonify({"error": g.verify_blocked, "verify": True}), 403
            if _wants_json():
                return jsonify({"error": f"That needs {credits:,} credits and you have {db.balance(g.ws):,}.",
                                "billing": url_for("billing_page")}), 402
            return _back(url_for("library"))
        conn.execute("UPDATE jobs SET parent_job_id=? WHERE id=?", (j["id"], new))
        conn.commit()
    if _wants_json():
        return jsonify({"ok": True, "job": new})
    flash(f"Making it in 1080p for {credits:,} credits. Both versions stay in your library.", "ok")
    return _back(url_for("create.batch", bid=j["batch_id"]) if j["batch_id"] else url_for("library", highlight=new))


@bp.route("/app/jobs/<int:jid>/review", methods=["POST"])
@login_required
def review(jid):
    data = request.get_json(silent=True) or request.form
    verdict = data.get("verdict") or ""
    with db.connect() as conn:
        _job(conn, jid)
        if verdict in VERDICTS:
            conn.execute("INSERT INTO job_reviews (job_id, user_id, workspace_id, verdict, note, at) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(job_id, user_id) DO UPDATE SET verdict=excluded.verdict, note=excluded.note, at=excluded.at",
                         (jid, g.user["id"], g.ws["id"], verdict, str(data.get("note") or "")[:500], db.now()))
        else:
            conn.execute("DELETE FROM job_reviews WHERE job_id=? AND user_id=?", (jid, g.user["id"]))
        conn.commit()
        v = verdicts(conn, [jid]).get(jid)
    if _wants_json():
        return jsonify({"ok": True, "verdict": v})
    return _back(url_for("library"))


@bp.route("/app/jobs/<int:jid>/delete", methods=["POST"])
@login_required
def delete(jid):
    with db.connect() as conn:
        j = _job(conn, jid)
        if j["status"] in ("pending", "running"):
            if _wants_json():
                return jsonify({"error": "It's still being made. Delete it once it's finished."}), 400
            flash("It's still being made. Delete it once it's finished.")
            return _back(url_for("library"))
        conn.execute("UPDATE jobs SET deleted_at=? WHERE id=?", (db.now(), jid))
        conn.commit()
    if _wants_json():
        return jsonify({"ok": True})
    flash("Deleted. Credits already spent stay in your history.", "ok")
    return _back(url_for("library"))


def verdicts(conn, ids) -> dict:
    """job id -> 'approved' | 'rejected' | 'mixed' (team members disagree)."""
    if not ids:
        return {}
    out = {}
    for r in conn.execute(f"SELECT job_id, verdict FROM job_reviews WHERE workspace_id=? AND job_id IN "
                          f"({','.join('?' * len(ids))})", (g.ws["id"], *ids)):
        out[r["job_id"]] = r["verdict"] if out.get(r["job_id"]) in (None, r["verdict"]) else "mixed"
    return out


def eta_minutes(conn) -> dict:
    """tool -> typical minutes from start to finished, from the last 20 that finished."""
    out = {}
    for tool in catalog.TOOLS:
        rows = conn.execute("SELECT created_at, finished_at FROM jobs WHERE tool=? AND status='done' AND finished_at "
                            "IS NOT NULL ORDER BY id DESC LIMIT 20", (tool,)).fetchall()
        mins = []
        for r in rows:
            try:
                from datetime import datetime
                mins.append((datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["created_at"])
                             ).total_seconds() / 60)
            except (TypeError, ValueError):
                pass
        if len(mins) >= 3:
            out[tool] = max(1, round(statistics.median(mins)))
    return out


CARD = """{% from '_macros.html' import card with context %}{{ card(j, outs, templates, tools, ctx=ctx, verdict=verdict,
          price=price, names=names, eta=eta) }}"""


def card_html(conn, j, ctx):
    outs = {}
    for o in conn.execute("SELECT * FROM outputs WHERE job_id=? ORDER BY id", (j["id"],)):
        outs.setdefault(o["job_id"], []).append(o)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM products WHERE workspace_id=?", (g.ws["id"],))}
    return render_template_string(CARD, j=j, outs=outs, templates=catalog.TEMPLATE_BY_ID, tools=catalog.DISPLAY_TOOLS, ctx=ctx,
                                  verdict=verdicts(conn, [j["id"]]).get(j["id"]), price=price_now(conn, j)[0],
                                  names=names, eta=eta_minutes(conn))


@bp.route("/app/jobs/cards")
@login_required
def cards():
    """Fresh card HTML for jobs whose status changed (the pages patch them in place)."""
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.isdigit()][:50]
    ctx = "batch" if request.args.get("ctx") == "batch" else "library"
    out = {}
    with db.connect() as conn:
        for jid in ids:
            j = conn.execute("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (jid, g.ws["id"])).fetchone()
            if j:
                out[str(jid)] = {"status": j["status"], "html": "" if j["deleted_at"] else card_html(conn, j, ctx)}
    return jsonify({"cards": out})
