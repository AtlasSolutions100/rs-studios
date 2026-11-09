"""Creator Pools pages (creator_pools.py): set up and pay for a pool, approve
or reject creators' posts, pause or end it; the admin's refunds; and the
signed partner endpoints Crowdcut calls (asset links, new-post events)."""
import os

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import billing
import creator_pools as cp
import db
import storage
from core import UPLOADS, PHOTO_TYPES, VIDEO_TYPES, MAX_PHOTO_MB, MAX_VIDEO_MB, admin_required, is_owner, \
    login_required, save_upload

bp = Blueprint("cpools", __name__)
MAX_FILES = 10
NEED_PRODUCT = "Add a product first. A Creator Pool pays creators to post about a product you sell."


def _guard():
    with db.connect() as conn:
        if not cp.visible(conn, g.ws, g.user):
            abort(404)


def _brand_default() -> str:
    b = g.get("brand_row")
    return ((b["brand_name"] if b is not None else "") or g.ws["brand_name"] or "")


def _pool(pid):
    with db.connect() as conn:
        p = cp.get(conn, g.ws["id"], pid)
    if p is None:
        abort(404)
    return p


@bp.route("/app/creator-pools", methods=["GET", "POST"])
@login_required
def index():
    _guard()
    if request.method == "POST":
        if not is_owner():
            abort(403, "Only the workspace owner can pay for a Creator Pool.")
        with db.connect() as conn:
            if cp.needs_product(conn, g.ws, g.user):
                flash(NEED_PRODUCT)
                return redirect(url_for("cpools.index"))
        brand_default = _brand_default()
        try:
            data = cp.clean(request.form, brand_default)
        except cp.Refused as e:
            flash(str(e))
            return render_index(form=request.form), 400
        saved = []
        files = [f for f in request.files.getlist("assets") if f and f.filename][:MAX_FILES]
        folder = os.path.join(UPLOADS, "creator_pools", str(g.ws["id"]))
        try:
            for f in files:
                is_vid = (f.mimetype or "").startswith("video/")
                path, ctype = save_upload(f, folder, VIDEO_TYPES if is_vid else PHOTO_TYPES,
                                          MAX_VIDEO_MB if is_vid else MAX_PHOTO_MB, ws=g.ws)
                saved.append((path, f.filename, ctype))
            with db.connect() as conn:
                pid = cp.create_draft(conn, g.ws["id"], g.user["id"], g.ws["brand_id"], data, saved)
        except (ValueError, cp.Refused, storage.StorageFull) as e:
            for path, _, _ in saved:
                try:
                    os.remove(path)
                except OSError:
                    pass
            flash(str(e))
            return render_index(form=request.form), (507 if isinstance(e, storage.StorageFull) else 400)
        return _start_checkout(pid)
    return render_index()


def render_index(form=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM creator_pools WHERE workspace_id=? ORDER BY id DESC LIMIT 50",
                            (g.ws["id"],)).fetchall()
        pools = [cp.view(conn, r) for r in rows]
        need_product = cp.needs_product(conn, g.ws, g.user)
    brand_default = _brand_default()
    return render_template("creator_pools.html", pools=pools, form=form or {}, brand_default=brand_default,
                           fee=cp.money(cp.fee_cents()), min_pool=cp.money(cp.min_pool_cents()),
                           max_pool=cp.money(cp.max_pool_cents()), modes=cp.MODE_LABELS, platforms=cp.PLATFORMS,
                           rate_range=cp.RATE_RANGE, days_range=cp.DAYS_RANGE, refund_days=cp.REFUND_DAYS,
                           connected=cp.connected(), billing_on=billing.configured(), public=cp.public(),
                           owner=is_owner(), need_product=need_product)


def _start_checkout(pid):
    if not billing.configured():
        flash("Checkout isn't switched on yet. Your pool is saved as a draft; nothing was charged.")
        return redirect(url_for("cpools.show", pid=pid))
    with db.connect() as conn:
        p = cp.get(conn, g.ws["id"], pid)
        try:
            url = cp.checkout(conn, p, request.host_url.rstrip("/"), g.user["email"], billing.create_line_items_session)
        except cp.Refused as e:
            flash(str(e))
            return redirect(url_for("cpools.show", pid=pid))
        except billing.BillingError:
            flash("Checkout couldn't start. Try again in a minute. Nothing was charged.")
            return redirect(url_for("cpools.show", pid=pid))
    return redirect(url, code=303)


@bp.route("/app/creator-pools/<int:pid>/pay", methods=["POST"])
@login_required
def pay(pid):
    _guard()
    if not is_owner():
        abort(403, "Only the workspace owner can pay for a Creator Pool.")
    _pool(pid)
    return _start_checkout(pid)


@bp.route("/app/creator-pools/<int:pid>")
@login_required
def show(pid):
    _guard()
    p = _pool(pid)
    subs, err = [], None
    with db.connect() as conn:
        if p["status"] == "paid":
            cp.push(conn, pid)          # paid but not on Crowdcut yet (a missed call): try again
            p = cp.get(conn, g.ws["id"], pid)
        if p["status"] in ("live", "paused", "ended"):
            try:
                subs = cp.submissions(conn, p)
            except cp.CrowdcutError as e:
                err = str(e)
            p = cp.get(conn, g.ws["id"], pid)
        v = cp.view(conn, p)
    if request.args.get("paid") and p["status"] in ("draft", "checkout"):
        flash("Thanks. Your payment is being confirmed; the pool goes live in a minute.", "ok")
    counts = {k: sum(1 for s in subs if s["brand_status"] == k) for k in ("pending", "approved", "rejected")}
    return render_template("creator_pool.html", p=v, subs=subs, err=err, counts=counts, money=cp.money,
                           platforms=cp.PLATFORMS, modes=cp.MODE_LABELS, refund_days=cp.REFUND_DAYS,
                           ends_on=cp.ends_on(p), owner=is_owner(), billing_on=billing.configured())


@bp.route("/app/creator-pools/<int:pid>/review", methods=["POST"])
@login_required
def review(pid):
    _guard()
    p = _pool(pid)
    sid = request.form.get("sid") or ""
    if not sid.isdigit():
        abort(400)
    with db.connect() as conn:
        try:
            cp.review(conn, p, int(sid), request.form.get("decision") or "", request.form.get("reason") or "")
            flash("Approved. Crowdcut checks the views next." if request.form.get("decision") == "approve"
                  else "Rejected. The creator sees your reason.", "ok")
        except cp.Refused as e:
            flash(str(e))
    return redirect(url_for("cpools.show", pid=pid) + "#posts")


@bp.route("/app/creator-pools/<int:pid>/status", methods=["POST"])
@login_required
def status(pid):
    _guard()
    if not is_owner():
        abort(403, "Only the workspace owner can pause or end a pool.")
    p = _pool(pid)
    want = request.form.get("status") or ""
    with db.connect() as conn:
        try:
            cp.set_status(conn, p, want)
            flash({"paused": "Paused. Creators can't send new posts until you resume.", "active": "Resumed.",
                   "ended": f"Ended. What's left of the pool is refunded within {cp.REFUND_DAYS} days."}[want], "ok")
        except (cp.Refused, KeyError) as e:
            flash(str(e))
    return redirect(url_for("cpools.show", pid=pid))


# ---------------------------------------------------------------- admin

@bp.route("/admin/creator-pools", methods=["GET", "POST"])
@admin_required
def admin_page():
    if request.method == "POST":
        act, pid = request.form.get("action") or "", request.form.get("pool") or ""
        with db.connect() as conn:
            p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (int(pid),)).fetchone() if pid.isdigit() else None
            try:
                if not p:
                    raise cp.Refused("No such pool.")
                if act == "sync":
                    cp.refresh(conn, p)
                    flash("Synced from Crowdcut.", "ok")
                elif act == "push":
                    flash("Pushed to Crowdcut." if cp.push(conn, p["id"]) else "Crowdcut didn't take it; see the error.")
                elif act == "end":
                    cp.set_status(conn, p, "ended")
                    flash("Ended.", "ok")
                elif act in ("refund_stripe", "refund_hand"):
                    r = cp.settle_refund(conn, p["id"], g.user["id"], "stripe" if act == "refund_stripe" else "hand",
                                         request.form.get("reference") or "", stripe_refund=billing.create_refund)
                    flash(f"Refund of {cp.money(r['amount_cents'])} recorded.", "ok")
                else:
                    raise cp.Refused("Unknown action.")
            except (cp.Refused, cp.CrowdcutError, billing.BillingError) as e:
                flash(str(e))
        return redirect(url_for("cpools.admin_page"))
    with db.connect() as conn:
        rows = conn.execute("SELECT p.*, w.name ws_name FROM creator_pools p LEFT JOIN workspaces w ON w.id=p.workspace_id "
                            "ORDER BY p.id DESC LIMIT 200").fetchall()
        pools = []
        for r in rows:
            v = cp.view(conn, r)
            v["ws_name"], v["pending"] = r["ws_name"], cp.pending_posts(r)
            pools.append(v)
    return render_template("admin_creator_pools.html", pools=pools, money=cp.money, connected=cp.connected(),
                           public=cp.public(), fee=cp.money(cp.fee_cents()), refund_days=cp.REFUND_DAYS)


# ---------------------------------------------------------------- partner endpoints (Crowdcut -> Campaignify)

def _partner_pool(ext):
    if not cp.connected():
        abort(404)
    if not cp.verify_request(request):
        return None, (jsonify({"error": "bad signature"}), 401)
    pid = cp.pool_id_of(ext)
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (pid,)).fetchone() if pid else None
    if not p or p["status"] not in ("live", "paused", "ended", "paid"):
        return None, (jsonify({"error": "not found"}), 404)
    return p, None


@bp.route("/api/partner/v1/creator-pools/<ext>/assets/<int:aid>/url", methods=["POST"])
def partner_asset_url(ext, aid):
    p, bad = _partner_pool(ext)
    if bad:
        return bad
    with db.connect() as conn:
        a = conn.execute("SELECT * FROM creator_pool_assets WHERE id=? AND pool_id=?", (aid, p["id"])).fetchone()
    if not a:
        return jsonify({"error": "not found"}), 404
    base = (os.environ.get("RS_PUBLIC_URL") or request.host_url).rstrip("/")
    return jsonify({"url": f"{base}/pools/assets/{cp.asset_token(a['id'])}", "expires_in": cp.ASSET_URL_TTL})


@bp.route("/api/partner/v1/creator-pools/<ext>/events", methods=["POST"])
def partner_event(ext):
    p, bad = _partner_pool(ext)
    if bad:
        return bad
    d = request.get_json(silent=True) or {}
    if d.get("type") == "submission.created":
        with db.connect() as conn:
            conn.execute("UPDATE creator_pools SET new_posts=new_posts+1 WHERE id=?", (p["id"],))
            conn.commit()
    return jsonify({"ok": True})


@bp.route("/pools/assets/<tok>")
def asset_file(tok):
    """A brand's uploaded file for Crowdcut's worker, by a signed link that
    expires in 24 hours."""
    aid = cp.asset_from_token(tok)
    if not aid:
        abort(404)
    with db.connect() as conn:
        a = conn.execute("SELECT * FROM creator_pool_assets WHERE id=?", (aid,)).fetchone()
    if not a or not os.path.exists(a["path"]):
        abort(404)
    resp = send_file(a["path"], mimetype=a["content_type"], conditional=True,
                     download_name=os.path.basename(a["filename"] or a["path"]))
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp
