"""Settings > Conversion tracking (roadmap M3.4, conversions.py): where the page and form events of a workspace's
Campaignify pages go.

    GET  /app/settings/conversions                      the destinations, last 7 days sent / failed, the setup steps
    POST /app/settings/conversions/meta/<pixel id>      save or remove a Meta pixel's Conversions API token and its
                                                        Test Events code (the pixel comes from the Meta connection)
    POST /app/settings/conversions/tiktok               add (or update) a TikTok pixel: pixel code, Events API token,
                                                        Test Events code
    POST /app/settings/conversions/tiktok/<id>/remove   remove a TikTok pixel and its token
    POST /app/settings/conversions/google/<conn id>/sync  make (or re-read) the Google Ads conversion actions
                                                        (conversion_actions.sync, M3.5; also runs on Google connect)

Behind CONVERSIONS_API and AD_LAUNCH (conversions.ws_enabled): off = 404. Owners and admins change things;
members see the page. Tokens go straight into a token box (secretsbox.py, sealed to "pixels:<workspace>") and are
never shown again, logged or flashed. Another workspace's pixel id answers 404.
"""
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

import conversions
import db
import secretsbox
from adplatforms import google
from core import is_owner, login_required

bp = Blueprint("conversions", __name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.|-]{20,600}$")
TEST_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
TIKTOK_CODE_RE = re.compile(r"^[A-Z0-9]{10,30}$")


def _on():
    if not conversions.ws_enabled(g.get("ws"), g.get("user")):
        abort(404)


def _owner():
    if not is_owner():
        abort(403, "Only the workspace owner or an admin can change conversion tracking.")
    if g.get("viewing"):
        abort(403, "You're viewing this workspace read-only.")


def _seal(token: str) -> str:
    return secretsbox.seal(token, secretsbox.aad_for("pixels", g.ws["id"]))


def error_words(platform: str, err) -> str | None:
    """A failed send in plain words for the owner. The stored error starts with its kind (conversions._kind) and is
    already scrubbed of tokens."""
    if not err:
        return None
    kind = str(err).split(":", 1)[0]
    if kind == "token":
        return {"meta": "Meta refused the token. Paste a new one from Events Manager.",
                "tiktok": "TikTok refused the token. Make a new Events API token for this pixel.",
                "google": "Google access ended. Reconnect Google Ads."}.get(platform, "The token was refused.")
    if kind == "permission":
        return {"meta": "Meta refused the event for this pixel. Check the token belongs to this pixel's dataset.",
                "google": "Google refused the lead. Reconnect Google Ads and allow sending conversions."}.get(
                    platform, "The platform refused the event.")
    if kind in ("network", "rate"):
        return "We couldn't reach the platform. We'll try again."
    return "The platform refused the last event. We'll try again, then stop after 4 tries."


@bp.route("/app/settings/conversions")
@login_required
def settings():
    _on()
    with db.connect() as conn:
        dests = conversions.destinations(conn, g.ws["id"])
        meta_rows = conn.execute("SELECT p.*, c.account_name FROM pixels p JOIN ad_connections c ON c.id=p.connection_id "
                                 "WHERE p.workspace_id=? AND p.platform='meta' AND c.status IN ('active','expired','error') "
                                 "ORDER BY p.id", (g.ws["id"],)).fetchall()
        tiktok_rows = dests["tiktok"]
        stats = conversions.stats(conn, g.ws["id"])
        pages_on = conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=? AND status='published' AND "
                                "measurement!='off'", (g.ws["id"],)).fetchone()[0]
        pages_all = conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=? AND status='published'",
                                 (g.ws["id"],)).fetchone()[0]
        gconns = []
        if google.enabled(g.ws, g.get("user")):
            import conversion_actions
            for row in conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='google' AND "
                                    "status='active' ORDER BY id", (g.ws["id"],)).fetchall():
                gconns.append({"row": row, "setup": conversion_actions.setup(conn, row["id"]),
                               "actions": conversion_actions.actions(conn, row["id"]),
                               "checklist": conversion_actions.checklist(conn, row)})
    return render_template("conversions.html", meta_rows=meta_rows, tiktok_rows=tiktok_rows, google_rows=dests["google"],
                           stats=stats, words=error_words, owner=is_owner() and not g.get("viewing"),
                           box_ready=secretsbox.configured(), conn_token=conversions.meta_connection_token_ok(),
                           pages_on=pages_on, pages_all=pages_all, has_dm=conversions.has_datamanager,
                           google_on=google.enabled(g.ws, g.get("user")), gconns=gconns, dashed=google.dashed)


@bp.route("/app/settings/conversions/meta/<int:pid>", methods=["POST"])
@login_required
def meta_pixel(pid):
    _on()
    _owner()
    f = request.form
    with db.connect() as conn:
        px = conn.execute("SELECT * FROM pixels WHERE id=? AND workspace_id=? AND platform='meta'", (pid, g.ws["id"])).fetchone()
        if px is None:
            abort(404)
        test = (f.get("test_code") or "").strip()
        if test and not TEST_CODE_RE.match(test):
            flash("That test code doesn't look right. Copy it from the Test events tab in Meta Events Manager.")
            return redirect(url_for("conversions.settings"))
        if f.get("action") == "remove_token":
            conn.execute("UPDATE pixels SET capi_token_enc=NULL WHERE id=?", (pid,))
            conn.commit()
            flash(f"Removed the Conversions API token for {px['name'] or 'the pixel'}. Server events to it stop.")
            return redirect(url_for("conversions.settings"))
        token = re.sub(r"\s+", "", f.get("token") or "")
        if token and not TOKEN_RE.match(token):
            flash("That token doesn't look right. Copy the whole access token from Events Manager.")
            return redirect(url_for("conversions.settings"))
        if token and not secretsbox.configured():
            flash("Saving tokens isn't switched on yet. Try again later.")
            return redirect(url_for("conversions.settings"))
        if token:
            conn.execute("UPDATE pixels SET capi_token_enc=?, test_code=? WHERE id=?", (_seal(token), test or None, pid))
        else:
            conn.execute("UPDATE pixels SET test_code=? WHERE id=?", (test or None, pid))
        conn.commit()
    flash("Saved." + (" Server events to this pixel start with the next visit." if token else ""), "ok")
    return redirect(url_for("conversions.settings"))


@bp.route("/app/settings/conversions/tiktok", methods=["POST"])
@login_required
def tiktok_add():
    _on()
    _owner()
    f = request.form
    code = (f.get("pixel_code") or "").strip().upper()
    token = re.sub(r"\s+", "", f.get("token") or "")
    test = (f.get("test_code") or "").strip()
    name = " ".join((f.get("name") or "").split())[:80] or f"TikTok pixel {code}"
    if not TIKTOK_CODE_RE.match(code):
        flash("Add the pixel code from TikTok Events Manager. It looks like C4AB12CD34EF56GH78IJ.")
        return redirect(url_for("conversions.settings"))
    if not TOKEN_RE.match(token):
        flash("Add the Events API access token from the pixel's Settings in TikTok Events Manager.")
        return redirect(url_for("conversions.settings"))
    if test and not TEST_CODE_RE.match(test):
        flash("That test code doesn't look right. Copy it from the Test events tab in TikTok Events Manager.")
        return redirect(url_for("conversions.settings"))
    if not secretsbox.configured():
        flash("Saving tokens isn't switched on yet. Try again later.")
        return redirect(url_for("conversions.settings"))
    with db.connect() as conn:
        conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, capi_token_enc, "
                     "test_code, created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id, platform, external_pixel_id) "
                     "DO UPDATE SET name=excluded.name, capi_token_enc=excluded.capi_token_enc, test_code=excluded.test_code",
                     (g.ws["id"], "tiktok", None, code, name, _seal(token), test or None, db.now()))
        conn.commit()
    flash(f"Added {name}. Page views and leads go to it from the next visit.", "ok")
    return redirect(url_for("conversions.settings"))


@bp.route("/app/settings/conversions/tiktok/<int:pid>/remove", methods=["POST"])
@login_required
def tiktok_remove(pid):
    _on()
    _owner()
    with db.connect() as conn:
        px = conn.execute("SELECT * FROM pixels WHERE id=? AND workspace_id=? AND platform='tiktok'", (pid, g.ws["id"])).fetchone()
        if px is None:
            abort(404)
        conn.execute("DELETE FROM pixels WHERE id=?", (pid,))
        conn.execute("UPDATE ws_conversion_queue SET status='dropped', error='removed' WHERE workspace_id=? AND "
                     "platform='tiktok' AND dest_id=? AND status IN ('pending','failed')", (g.ws["id"], pid))
        conn.commit()
    flash(f"Removed {px['name'] or 'the TikTok pixel'} and its token.")
    return redirect(url_for("conversions.settings"))


@bp.route("/app/settings/conversions/google/<int:cid>/sync", methods=["POST"])
@login_required
def google_sync(cid):
    _on()
    _owner()
    if not google.enabled(g.ws, g.get("user")):
        abort(404)
    import adplatforms
    import conversion_actions
    with db.connect() as conn:
        row = adplatforms.get(conn, g.ws["id"], cid)
        if row is None or row["platform"] != "google":
            abort(404)
        res = conversion_actions.sync(conn, row)
    if res["status"] == "ready":
        flash(res["words"], "ok")
    else:
        flash(res["words"])
    return redirect(url_for("conversions.settings") + "#google")
