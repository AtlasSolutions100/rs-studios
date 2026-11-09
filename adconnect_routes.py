"""Settings > Connections: a workspace connects its own Meta ad account (roadmap M2.1).

    GET  /app/settings/ad-accounts                 the connections, Connect Meta, the "no ad account yet" checklist
    POST /connect/meta                             start Facebook Login for Business (owner or admin; CSRF)
    GET  /connect/meta/callback                    Meta sends the person back here with ?code&state
    GET/POST /app/settings/ad-accounts/<id>/pick   pick the ad account and Page (Instagram comes with the Page)
    GET/POST /app/settings/ad-accounts/<id>/pixel  pick the pixel (skipped when there are 0 or 1)
    POST /app/settings/ad-accounts/<id>/disconnect remove the connection and its token; revoke at Meta
    POST /connect/meta/deauthorize                 Meta's Deauthorize Callback (signed_request)
    POST /connect/meta/data-deletion               Meta's Data Deletion Request Callback (signed_request)
    GET  /connect/meta/deletion?code=...           the status page that callback links to

Connecting sits behind AD_LAUNCH (adplatforms.enabled): off = 404. Removing never does (M-3): the
Settings list and Disconnect stay open for a workspace that still holds connections, and Meta's two
callbacks work whatever the flag says, so stored tokens can always be removed. Connecting is free
(no plan gate, design doc 4.1). Owners and admins connect and disconnect; members see the list.
Another workspace's connection id answers 404.

The two callback URLs to enter in the Meta app (App settings > Basic > Data deletion, and
Facebook Login for Business > Settings > Deauthorize callback URL):
    https://campaignify.ai/connect/meta/deauthorize
    https://campaignify.ai/connect/meta/data-deletion

The OAuth state is single use, tied to this session, this workspace and this person, and lives
10 minutes; PKCE (S256) guards the code. Tokens go straight into a token box (secretsbox.py)
and are never logged, flashed or rendered. The callback logs no query string.
"""
import base64
import hashlib
import json
import logging
import secrets
import time

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

import adhttp
import adplatforms
import db
import secretsbox
from adplatforms import meta
from core import is_owner, login_required

log = logging.getLogger("campaignify.adconnect")
bp = Blueprint("adconnect", __name__)

S_KEY = "ad_oauth"
STATE_TTL_S = 600
FAILED = "Connecting to Meta didn't work. Try again in a minute."


def _on():
    if not adplatforms.enabled(g.get("ws"), g.get("user")):
        abort(404)


def _on_or_stored():
    """Removing what's stored works even with AD_LAUNCH off (M-3); there's just nothing to see without it."""
    if adplatforms.enabled(g.get("ws"), g.get("user")):
        return True
    with db.connect() as conn:
        if g.get("ws") is None or not adplatforms.has_stored(conn, g.ws["id"]):
            abort(404)
    return False


def _owner():
    if not is_owner():
        abort(403, "Only the workspace owner or an admin can connect or remove ad accounts.")
    if g.get("viewing"):
        abort(403, "You're viewing this workspace read-only.")


def _pending(conn, cid):
    row = adplatforms.get(conn, g.ws["id"], cid)
    if row is None:
        abort(404)
    return row


def _data(row) -> dict:
    try:
        return json.loads(row["pending_json"] or "{}")
    except ValueError:
        return {}


@bp.route("/app/settings/ad-accounts")
@login_required
def settings():
    live = _on_or_stored()
    with db.connect() as conn:
        rows = adplatforms.listed(conn, g.ws["id"])
        px = {}
        for p in conn.execute("SELECT * FROM pixels WHERE workspace_id=? ORDER BY id", (g.ws["id"],)).fetchall():
            px.setdefault(p["connection_id"], []).append(p)
    return render_template("ad_connections.html", rows=rows, pixels=px, owner=is_owner() and not g.get("viewing"),
                           meta_ready=live and meta.ADAPTER.configured(), checklist=meta.checklist(), paused=not live)


@bp.route("/connect/meta", methods=["POST"])
@login_required
def meta_start():
    _on()
    _owner()
    if not meta.ADAPTER.configured():
        flash("Connecting Meta isn't switched on yet. Try again later.")
        return redirect(url_for("adconnect.settings"))
    reconnect = request.form.get("reconnect") or ""
    if reconnect:
        with db.connect() as conn:
            reconnect = str(_pending(conn, reconnect)["id"]) if reconnect.isdigit() else ""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    session[S_KEY] = {"state": state, "verifier": verifier, "ws": g.ws["id"], "uid": g.user["id"],
                      "reconnect": reconnect, "at": int(time.time())}
    return redirect(meta.ADAPTER.oauth_start(state, challenge))


@bp.route("/connect/meta/callback")
def meta_callback():
    pend = session.pop(S_KEY, None)
    if not g.get("user"):
        flash("Log in, then connect your ad account again.")
        return redirect(url_for("login"))          # never a `next` carrying the code
    _on()
    back = redirect(url_for("adconnect.settings"))
    if request.args.get("error"):
        log.info("meta connect refused by the person: %s", (request.args.get("error_reason") or "")[:40])
        flash("Meta didn't connect: the request was cancelled. Nothing changed.")
        return back
    sent, code = request.args.get("state") or "", request.args.get("code") or ""
    if not pend or not sent or not code or not secrets.compare_digest(sent, str(pend.get("state") or "")) \
            or pend.get("ws") != g.ws["id"] or pend.get("uid") != g.user["id"] \
            or time.time() - int(pend.get("at") or 0) > STATE_TTL_S:
        flash("That connect attempt expired. Start it again.")
        return back
    _owner()
    try:
        got = meta.ADAPTER.oauth_finish(code, pend["verifier"])
    except adhttp.AdHttpError as e:          # scrubbed: never carries a token or the code
        log.warning("meta connect failed: %s", e)
        flash(FAILED)
        return back
    except Exception as e:  # noqa: BLE001  a malformed answer; log the kind only (messages may echo values)
        log.error("meta connect failed: %s", type(e).__name__)
        flash(FAILED)
        return back
    now = db.now()
    keep = {"accounts": got["accounts"], "pages": got["pages"], "missing": got["missing"],
            "user_name": got["user_name"], "reconnect": pend.get("reconnect") or ""}
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, "
            "token_expires_at, scopes, status, pending_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.ws["id"], "meta", "pending:" + secrets.token_hex(8), got["user_id"],
             adplatforms.seal_token(g.ws["id"], got["token"]), got["expires_at"], " ".join(got["scopes"]),
             "pending", json.dumps(keep), g.user["id"], now, now)).lastrowid
        conn.commit()
    return redirect(url_for("adconnect.pick", cid=cid))


@bp.route("/app/settings/ad-accounts/<int:cid>/pick", methods=["GET", "POST"])
@login_required
def pick(cid):
    _on()
    _owner()
    with db.connect() as conn:
        row = _pending(conn, cid)
        if row["status"] != "pending":
            return redirect(url_for("adconnect.settings"))
        if adplatforms.pending_expired(row):      # an unfinished connect keeps its token an hour at most (L-13)
            adplatforms.sweep_pending(conn, g.ws["id"])
            flash("That connect attempt expired. Start it again.")
            return redirect(url_for("adconnect.settings"))
        d = _data(row)
        accounts = {a["id"]: a for a in d.get("accounts") or []}
        pages = {p["id"]: p for p in d.get("pages") or []}
        pre = None
        if d.get("reconnect"):
            old = adplatforms.get(conn, g.ws["id"], d["reconnect"])
            pre = old["external_account_id"] if old else None
        if request.method == "GET":
            return render_template("ad_connect_pick.html", step="account", row=row, d=d, pre=pre,
                                   checklist=meta.checklist())
        a = accounts.get(request.form.get("account") or "")
        if not a or not a["usable"]:
            flash("Pick one of the ad accounts listed.")
            return redirect(url_for("adconnect.pick", cid=cid))
        pid = request.form.get("page") or ""
        if pid and pid not in pages:
            flash("Pick one of the Pages listed.")
            return redirect(url_for("adconnect.pick", cid=cid))
        p = pages.get(pid) or {}
        now = db.now()
        fields = (a["id"], a["name"], a["currency"], a["timezone"], a.get("business_id"), p.get("id"), p.get("name"),
                  p.get("ig_id"), p.get("ig_username"), row["external_user_id"], row["token_enc"],
                  row["token_expires_at"], row["scopes"], now)
        sql = ("UPDATE ad_connections SET external_account_id=?, account_name=?, currency=?, timezone=?, business_id=?, "
               "page_id=?, page_name=?, instagram_actor_id=?, instagram_username=?, external_user_id=?, token_enc=?, "
               "token_expires_at=?, scopes=?, updated_at=?, status='active', last_error=NULL, pending_json=? WHERE id=?")
        same = conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='meta' AND "
                            "external_account_id=? AND id!=?", (g.ws["id"], a["id"], cid)).fetchone()
        target = same["id"] if same else cid
        if same:
            _revoke_replaced(conn, same, row)
            conn.execute("DELETE FROM ad_connections WHERE id=?", (cid,))
        conn.execute(sql, fields + (None, target))
        conn.commit()
        row = adplatforms.get(conn, g.ws["id"], target)
        try:
            pxs = meta.ADAPTER.list_pixels(adplatforms.open_token(row), a["id"])
        except adhttp.TokenInvalid as e:
            adplatforms.mark(conn, target, "expired", "Meta ended this access. Reconnect.")
            log.warning("meta pixels: %s", e)
            pxs = None
        except (adhttp.AdHttpError, secretsbox.BoxError) as e:
            log.warning("meta pixels: %s", e)
            pxs = None
        if pxs is None:
            flash(f"Connected {a['name']}. We couldn't read its pixels; you can pick one later.")
            return redirect(url_for("adconnect.settings"))
        if len(pxs) == 1:
            _save_pixel(conn, row, pxs[0])
        if len(pxs) > 1:
            conn.execute("UPDATE ad_connections SET pending_json=? WHERE id=?", (json.dumps({"pixels": pxs}), target))
            conn.commit()
            return redirect(url_for("adconnect.pixel", cid=target))
    flash(f"Connected {a['name']}." + ("" if pxs else " It has no pixel yet: create one in Meta Events Manager."))
    return redirect(url_for("adconnect.settings"))


def _revoke_replaced(conn, old, new):
    """Re-audit L-14: a different Meta user reconnected this ad account, so the old user's token is about to be
    overwritten. Revoke the old grant at Meta first, unless another connection still uses that user's grant.
    Best effort: the old token is replaced here either way."""
    if not old["token_enc"] or not old["external_user_id"] or old["external_user_id"] == new["external_user_id"]:
        return False
    held = conn.execute("SELECT 1 FROM ad_connections WHERE platform=? AND external_user_id=? AND id NOT IN (?,?) AND "
                        "token_enc IS NOT NULL AND status IN ('active','expired','error','pending') LIMIT 1",
                        (old["platform"], old["external_user_id"], old["id"], new["id"])).fetchone()
    if held or not meta.ADAPTER.configured():
        return False
    try:
        ok = meta.ADAPTER.revoke(adplatforms.open_token(old))
        log.info("meta reconnect by another person: the previous grant was revoked (%s)", "ok" if ok else "no")
        return ok
    except (adhttp.AdHttpError, secretsbox.BoxError) as e:
        log.warning("meta revoke of the replaced grant: %s", e)
        return False


def _save_pixel(conn, row, px):
    conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id, platform, external_pixel_id) DO UPDATE SET "
                 "connection_id=excluded.connection_id, name=excluded.name",
                 (row["workspace_id"], row["platform"], row["id"], px["id"], px["name"], db.now()))
    conn.execute("UPDATE ad_connections SET pending_json=NULL WHERE id=?", (row["id"],))
    conn.commit()


@bp.route("/app/settings/ad-accounts/<int:cid>/pixel", methods=["GET", "POST"])
@login_required
def pixel(cid):
    _on()
    _owner()
    with db.connect() as conn:
        row = _pending(conn, cid)
        pxs = {p["id"]: p for p in _data(row).get("pixels") or []}
        if not pxs or row["status"] != "active":
            return redirect(url_for("adconnect.settings"))
        if request.method == "GET":
            return render_template("ad_connect_pick.html", step="pixel", row=row, pixels=list(pxs.values()))
        choice = request.form.get("pixel") or ""
        if choice == "skip":
            conn.execute("UPDATE ad_connections SET pending_json=NULL WHERE id=?", (cid,))
            conn.commit()
        elif choice in pxs:
            _save_pixel(conn, row, pxs[choice])
        else:
            flash("Pick one of the pixels listed.")
            return redirect(url_for("adconnect.pixel", cid=cid))
    flash(f"Connected {row['account_name']}.")
    return redirect(url_for("adconnect.settings"))


@bp.route("/app/settings/ad-accounts/<int:cid>/disconnect", methods=["POST"])
@login_required
def disconnect(cid):
    _on_or_stored()          # works with AD_LAUNCH off too: tokens can always be removed (M-3)
    _owner()
    with db.connect() as conn:
        row = _pending(conn, cid)
        if row["status"] == "revoked":
            return redirect(url_for("adconnect.settings"))
        shared = row["external_user_id"] and conn.execute(
            "SELECT 1 FROM ad_connections WHERE platform=? AND external_user_id=? AND id!=? AND token_enc IS NOT NULL "
            "AND status IN ('active','expired','error','pending')", (row["platform"], row["external_user_id"], cid)).fetchone()
        revoked = False
        if not shared and row["token_enc"] and meta.ADAPTER.configured():
            try:
                revoked = meta.ADAPTER.revoke(adplatforms.open_token(row))
            except (adhttp.AdHttpError, secretsbox.BoxError) as e:
                log.warning("meta revoke: %s", e)
        conn.execute("UPDATE ad_connections SET status='revoked', token_enc=NULL, refresh_token_enc=NULL, pending_json=NULL, "
                     "last_error=NULL, updated_at=? WHERE id=?", (db.now(), cid))
        conn.execute("DELETE FROM pixels WHERE connection_id=? AND workspace_id=?", (cid, g.ws["id"]))
        conn.commit()
    name = row["account_name"] or "the ad account"
    if shared:
        flash(f"Disconnected {name}. Your Meta login stays connected for your other Campaignify connections.")
    elif revoked:
        flash(f"Disconnected {name} and removed Campaignify's access on Meta.")
    else:
        flash(f"Disconnected {name}. To be sure, remove Campaignify under Business integrations in your Facebook settings.")
    return redirect(url_for("adconnect.settings"))


# ---------------------------------------------------------------- Meta's callbacks (M-3), whatever AD_LAUNCH says

def _signed(kind: str):
    """Verify the POSTed signed_request; on success drop the person's tokens and record the request."""
    try:
        payload = meta.parse_signed_request(request.form.get("signed_request") or "")
    except meta.BadSignedRequest as e:
        log.warning("meta %s callback refused: %s", kind, e)
        return None, None
    uid = str(payload["user_id"])
    with db.connect() as conn:
        n = adplatforms.forget_platform_user(conn, "meta", uid, delete=(kind == "deletion"))
        code = secrets.token_hex(8)
        conn.execute("INSERT INTO meta_data_requests (code, kind, user_hash, connections, status, created_at, done_at) "
                     "VALUES (?,?,?,?,?,?,?)", (code, kind, hashlib.sha256(("meta:" + uid).encode()).hexdigest(), n,
                                                "done", db.now(), db.now()))
        conn.commit()
    log.info("meta %s callback: %s connection(s) cleared", kind, n)
    return code, n


@bp.route("/connect/meta/deauthorize", methods=["POST"])
def meta_deauthorize():
    """Deauthorize Callback: the person removed the app on Facebook. Their tokens go now (rows kept as revoked)."""
    code, _ = _signed("deauthorize")
    if code is None:
        return {"error": "invalid signed_request"}, 400
    return {"ok": True}


@bp.route("/connect/meta/data-deletion", methods=["POST"])
def meta_data_deletion():
    """Data Deletion Request Callback: delete what we hold for this Meta user (connections, their tokens, the
    account, Page and pixel names) and answer with a status URL and a confirmation code, as Meta specifies."""
    code, _ = _signed("deletion")
    if code is None:
        return {"error": "invalid signed_request"}, 400
    import verify
    return {"url": f"{verify.public_url()}/connect/meta/deletion?code={code}", "confirmation_code": code}


@bp.route("/connect/meta/deletion")
def meta_deletion_status():
    code = (request.args.get("code") or "").strip()[:32]
    row = None
    if code:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM meta_data_requests WHERE code=?", (code,)).fetchone()
    return render_template("meta_deletion.html", row=row, code=code), (200 if row else 404)
