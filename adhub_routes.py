"""Ad-account setup hub and platform exports (Starter+)."""
import os

from flask import Blueprint, abort, g, redirect, render_template, request, send_file, url_for, flash

import adhub
import db
import exporter
from core import feature_required

bp = Blueprint("adhub", __name__)


def _done():
    with db.connect() as conn:
        rows = conn.execute("SELECT platform, step FROM adhub_progress WHERE workspace_id=?", (g.ws["id"],)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["platform"], set()).add(r["step"])
    return out


@bp.route("/app/ad-accounts")
@feature_required("adhub")
def index():
    return render_template("adhub.html", hub=adhub, done=_done())


@bp.route("/app/ad-accounts/<platform>")
@feature_required("adhub")
def platform(platform):
    p = adhub.PLATFORMS.get(platform)
    if not p:
        abort(404)
    with db.connect() as conn:
        outs = conn.execute("SELECT o.*, j.template_id, j.tool FROM outputs o JOIN jobs j ON j.id=o.job_id "
                            "WHERE j.workspace_id=? AND j.status='done' ORDER BY o.id DESC LIMIT 12",
                            (g.ws["id"],)).fetchall()
    return render_template("adhub_platform.html", hub=adhub, key=platform, p=p, done=_done().get(platform, set()),
                           outs=outs)


@bp.route("/app/ad-accounts/<platform>/steps/<int:step>", methods=["POST"])
@feature_required("adhub")
def toggle(platform, step):
    p = adhub.PLATFORMS.get(platform)
    if not p or not 0 <= step < len(p["steps"]):
        abort(404)
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM adhub_progress WHERE workspace_id=? AND platform=? AND step=?",
                           (g.ws["id"], platform, step))
        if not cur.rowcount:
            conn.execute("INSERT INTO adhub_progress (workspace_id, platform, step, done_by, done_at) VALUES (?,?,?,?,?)",
                         (g.ws["id"], platform, step, g.user["id"], db.now()))
        conn.commit()
    return redirect(url_for("adhub.platform", platform=platform) + f"#s{step}")


@bp.route("/app/export/<int:oid>")
@feature_required("adhub")
def export_page(oid):
    o = _output(oid)
    return render_template("export.html", o=o, hub=adhub)


@bp.route("/app/export/<int:oid>/<platform>/<fmt>")
@feature_required("adhub")
def export_file(oid, platform, fmt):
    o = _output(oid)
    spec = adhub.export_by(platform, fmt)
    if not spec:
        abort(404)
    try:
        path = exporter.export(o["path"], o["kind"], platform, fmt, spec[2], spec[3])
    except exporter.ExportError as e:
        flash(str(e))
        return redirect(url_for("adhub.export_page", oid=oid))
    name = f"campaignify-{o['job_id']}-{platform}-{fmt}-{spec[2]}x{spec[3]}{os.path.splitext(path)[1]}"
    return send_file(path, as_attachment=True, download_name=name, conditional=True)


def _output(oid):
    with db.connect() as conn:
        o = conn.execute("SELECT o.* FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                         (oid, g.ws["id"])).fetchone()
    if not o:
        abort(404)
    return o
