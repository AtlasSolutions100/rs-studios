"""Private share links for a finished ad: /s/<token>.

Each output (one image or video file) can get an unguessable token. Anyone
with the link sees a bare page with just that file and the product's name, and
a small "Made with Campaignify" footer: no price, no buy button, nothing else
from the workspace. The file is served through the same token, so it works
logged out. The workspace that made the ad creates and revokes the link from
the ad's details; revoking (or deleting the ad) makes the old link 404 at once,
and a new link gets a new token. Share pages are never indexed.
"""
import os
import re
import secrets

from flask import (Blueprint, abort, flash, g, jsonify, make_response, redirect, render_template, request, send_file,
                   url_for)

import db
from core import login_required

bp = Blueprint("share", __name__)
TOKEN = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
NOINDEX = "noindex, nofollow, noarchive, noimageindex"


def _own_output(conn, oid):
    """An output of a finished, not-deleted ad in the member's workspace (404 otherwise)."""
    o = conn.execute("SELECT o.*, j.status AS job_status FROM outputs o JOIN jobs j ON j.id=o.job_id "
                     "WHERE o.id=? AND j.workspace_id=? AND j.deleted_at IS NULL", (oid, g.ws["id"])).fetchone()
    if not o:
        abort(404)
    return o


def _shared(conn, token):
    """The output behind a share token, or 404 (bad, revoked, deleted or unfinished)."""
    if not token or not TOKEN.match(token):
        abort(404)
    o = conn.execute("SELECT o.*, j.product_id, j.workspace_id, j.params_json FROM outputs o JOIN jobs j ON j.id=o.job_id "
                     "WHERE o.share_token=? AND j.deleted_at IS NULL AND j.status='done'", (token,)).fetchone()
    if not o:
        abort(404)
    return o


def _back(default):
    nxt = request.form.get("back") or ""
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else default)


def _wants_json():
    return request.accept_mimetypes.best == "application/json" or request.is_json


@bp.route("/app/outputs/<int:oid>/share", methods=["POST"])
@login_required
def create(oid):
    """Make (or reuse) the share link for one finished output."""
    with db.connect() as conn:
        o = _own_output(conn, oid)
        if o["job_status"] != "done":
            abort(400)
        token = o["share_token"]
        if not token:
            token = secrets.token_urlsafe(24)
            conn.execute("UPDATE outputs SET share_token=?, shared_at=? WHERE id=?", (token, db.now(), oid))
            conn.commit()
    link = url_for("share.view", token=token, _external=True)
    if _wants_json():
        return jsonify({"ok": True, "url": link})
    flash(f"Share link ready: {link} Anyone with the link can see this ad. Stop sharing any time from its details.", "ok")
    return _back(url_for("library"))


@bp.route("/app/outputs/<int:oid>/unshare", methods=["POST"])
@login_required
def revoke(oid):
    with db.connect() as conn:
        _own_output(conn, oid)
        conn.execute("UPDATE outputs SET share_token=NULL, shared_at=NULL WHERE id=?", (oid,))
        conn.commit()
    if _wants_json():
        return jsonify({"ok": True})
    flash("Stopped sharing. The old link no longer works.", "ok")
    return _back(url_for("library"))


def _noindex(resp, cache="no-store"):
    resp.headers["X-Robots-Tag"] = NOINDEX
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = cache
    return resp


@bp.route("/s/<token>")
def view(token):
    with db.connect() as conn:
        o = _shared(conn, token)
        p = conn.execute("SELECT name FROM products WHERE id=? AND workspace_id=?",
                         (o["product_id"], o["workspace_id"])).fetchone() if o["product_id"] else None
    name = (p["name"] if p else "") or ""
    resp = make_response(render_template("share.html", o=o, name=name, token=token))
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' https:; media-src 'self' https:; "
                                               "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
                                               "frame-ancestors 'none'")
    return _noindex(resp)


@bp.route("/s/<token>/media")
def media(token):
    with db.connect() as conn:
        o = _shared(conn, token)
    if request.args.get("poster") == "1":
        p = (o["path"] or "") + ".jpg"
        if not (o["path"] and os.path.exists(p)):
            abort(404)
        return _noindex(send_file(p, conditional=True), "private, max-age=300")
    if o["path"] and os.path.exists(o["path"]):
        return _noindex(send_file(o["path"], conditional=True), "private, max-age=300")
    if not o["url"]:
        abort(404)
    return _noindex(redirect(o["url"]))
