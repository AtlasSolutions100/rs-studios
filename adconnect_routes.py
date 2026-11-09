"""Settings > Connections: a workspace connects its own Meta ad account (roadmap M2.1), Google Ads
account (M3.1), Snapchat ad account (M6.1) and TikTok advertiser account (M6.2).

    GET  /app/settings/ad-accounts                 the connections, Connect Meta, the "no ad account yet" checklist
    POST /connect/meta                             start Facebook Login for Business (owner or admin; CSRF)
    GET  /connect/meta/callback                    Meta sends the person back here with ?code&state
    GET/POST /app/settings/ad-accounts/<id>/pick   pick the ad account and Page (Instagram comes with the Page)
    GET/POST /app/settings/ad-accounts/<id>/pixel  pick the pixel (skipped when there are 0 or 1)
    POST /app/settings/ad-accounts/<id>/disconnect remove the connection and its token; revoke at Meta
    POST /connect/meta/deauthorize                 Meta's Deauthorize Callback (signed_request)
    POST /connect/meta/data-deletion               Meta's Data Deletion Request Callback (signed_request)
    GET  /connect/meta/deletion?code=...           the status page that callback links to
    POST /connect/google                           start Google's OAuth (adwords scope, offline; owner or admin)
    GET  /connect/google/callback                  Google sends the person back here with ?code&state
                                                   (then .../<id>/pick lists the Google Ads customers, managers'
                                                   client accounts included)
    POST /connect/snapchat                         start Snap's OAuth (snapchat-marketing-api; owner or admin)
    GET  /connect/snapchat/callback                Snap sends the person back here with ?code&state
                                                   (then .../<id>/pick: the ad account and its public profile,
                                                   then .../<id>/pixel when there are several Snap Pixels)
    POST /connect/tiktok                           start TikTok's advertiser authorization (owner or admin)
    GET  /connect/tiktok/callback                  TikTok sends the person back here with ?auth_code&state
    GET/POST /app/settings/ad-accounts/<id>/identity  TikTok: pick the custom identity the ads show, or make one

Google sits behind AD_LAUNCH and GOOGLE_ADS (adplatforms/google.py). Its refresh token goes into the same
token box; Disconnect revokes it at Google. Snapchat sits behind AD_LAUNCH and SNAPCHAT_ADS (adplatforms/snapchat.py);
its refresh token goes into the token box too. Snap has no revoke endpoint, so Disconnect deletes the token here and
says where to remove the app on Snap. TikTok sits behind AD_LAUNCH and TIKTOK_ADS (adplatforms/tiktok.py); its
access token goes into the token box and Disconnect revokes it at TikTok.

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
import conversions
import db
import secretsbox
from adplatforms import google, meta, snapchat, tiktok
from core import is_owner, login_required

log = logging.getLogger("campaignify.adconnect")
bp = Blueprint("adconnect", __name__)

S_KEY = "ad_oauth"
G_KEY = "ad_oauth_google"
SNAP_KEY = "ad_oauth_snapchat"
TT_KEY = "ad_oauth_tiktok"
STATE_TTL_S = 600
FAILED = "Connecting to Meta didn't work. Try again in a minute."
G_FAILED = "Connecting to Google didn't work. Try again in a minute."
SNAP_FAILED = "Connecting to Snapchat didn't work. Try again in a minute."
TT_FAILED = "Connecting to TikTok didn't work. Try again in a minute."
WORDS = {"meta": "Meta", "google": "Google", "snapchat": "Snapchat", "tiktok": "TikTok"}


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


def _google_on():
    if not google.enabled(g.get("ws"), g.get("user")):
        abort(404)


def _snap_on():
    if not snapchat.enabled(g.get("ws"), g.get("user")):
        abort(404)


def _tt_on():
    if not tiktok.enabled(g.get("ws"), g.get("user")):
        abort(404)


def _pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


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
    g_on = live and google.enabled(g.ws, g.get("user"))
    s_on = live and snapchat.enabled(g.ws, g.get("user"))
    t_on = live and tiktok.enabled(g.ws, g.get("user"))
    return render_template("ad_connections.html", rows=rows, pixels=px, owner=is_owner() and not g.get("viewing"),
                           meta_ready=live and meta.ADAPTER.configured(), checklist=meta.checklist(), paused=not live,
                           google_on=g_on, google_ready=g_on and google.ADAPTER.configured(),
                           google_checklist=google.checklist(), dashed=google.dashed,
                           snap_on=s_on, snap_ready=s_on and snapchat.ADAPTER.configured(),
                           snap_checklist=snapchat.checklist(),
                           tt_on=t_on, tt_ready=t_on and tiktok.ADAPTER.configured(), tt_checklist=tiktok.checklist(),
                           conv_on=live and conversions.ws_enabled(g.ws, g.get("user")))


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
        if row["platform"] == "google":
            return _pick_google(conn, row)
        if row["platform"] == "snapchat":
            return _pick_snapchat(conn, row)
        if row["platform"] == "tiktok":
            return _pick_tiktok(conn, row)
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
    ad = adplatforms.adapter(old["platform"])
    if held or not ad.configured():
        return False
    try:
        ok = ad.revoke(adplatforms.open_token(old))
        log.info("%s reconnect by another person: the previous grant was revoked (%s)", old["platform"], "ok" if ok else "no")
        return ok
    except (adhttp.AdHttpError, secretsbox.BoxError) as e:
        log.warning("%s revoke of the replaced grant: %s", old["platform"], e)
        return False


def _save_pixel(conn, row, px):
    # TikTok: a pixel the member added by hand in Conversion tracking (with its Events API token) stays theirs, not
    # the connection's, so Disconnect never deletes it; launches read both (launch_video.pixels_for).
    keep = "DO NOTHING" if row["platform"] == "tiktok" else "DO UPDATE SET connection_id=excluded.connection_id, name=excluded.name"
    conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                 f"VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id, platform, external_pixel_id) {keep}",
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
        ad = adplatforms.adapter(row["platform"])
        if not shared and row["token_enc"] and ad.configured():
            try:
                revoked = ad.revoke(adplatforms.open_token(row))
            except (adhttp.AdHttpError, secretsbox.BoxError) as e:
                log.warning("%s revoke: %s", row["platform"], e)
        conn.execute("UPDATE ad_connections SET status='revoked', token_enc=NULL, refresh_token_enc=NULL, pending_json=NULL, "
                     "last_error=NULL, updated_at=? WHERE id=?", (db.now(), cid))
        conn.execute("DELETE FROM pixels WHERE connection_id=? AND workspace_id=?", (cid, g.ws["id"]))
        import conversion_actions
        conversion_actions.forget(conn, cid, g.ws["id"])          # Google's conversion actions as we read them (M3.5)
        conn.commit()
    name = row["account_name"] or "the ad account"
    word = WORDS.get(row["platform"], "the platform")
    if shared:
        flash(f"Disconnected {name}. Your {word} login stays connected for your other Campaignify connections.")
    elif revoked:
        flash(f"Disconnected {name} and removed Campaignify's access on {word}.")
    elif row["platform"] == "google":
        flash(f"Disconnected {name}. To be sure, remove Campaignify under Third-party apps in your Google Account.")
    elif row["platform"] == "snapchat":
        flash(f"Disconnected {name} and deleted Campaignify's access here. Snapchat has no way for apps to end their own "
              "access, so to be sure, remove Campaignify from your Snapchat account's connected apps.")
    else:
        flash(f"Disconnected {name}. To be sure, remove Campaignify under Business integrations in your Facebook settings.")
    return redirect(url_for("adconnect.settings"))


# ---------------------------------------------------------------- Google Ads (M3.1)

@bp.route("/connect/google", methods=["POST"])
@login_required
def google_start():
    _on()
    _google_on()
    _owner()
    if not google.ADAPTER.configured():
        flash("Connecting Google isn't switched on yet. Try again later.")
        return redirect(url_for("adconnect.settings"))
    reconnect = request.form.get("reconnect") or ""
    if reconnect:
        with db.connect() as conn:
            reconnect = str(_pending(conn, reconnect)["id"]) if reconnect.isdigit() else ""
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    session[G_KEY] = {"state": state, "verifier": verifier, "ws": g.ws["id"], "uid": g.user["id"],
                      "reconnect": reconnect, "at": int(time.time())}
    return redirect(google.ADAPTER.oauth_start(state, challenge))


@bp.route("/connect/google/callback")
def google_callback():
    pend = session.pop(G_KEY, None)
    if not g.get("user"):
        flash("Log in, then connect your Google Ads account again.")
        return redirect(url_for("login"))          # never a `next` carrying the code
    _on()
    _google_on()
    back = redirect(url_for("adconnect.settings"))
    if request.args.get("error"):
        log.info("google connect refused by the person: %s", (request.args.get("error") or "")[:40])
        flash("Google didn't connect: the request was cancelled. Nothing changed.")
        return back
    sent, code = request.args.get("state") or "", request.args.get("code") or ""
    if not pend or not sent or not code or not secrets.compare_digest(sent, str(pend.get("state") or "")) \
            or pend.get("ws") != g.ws["id"] or pend.get("uid") != g.user["id"] \
            or time.time() - int(pend.get("at") or 0) > STATE_TTL_S:
        flash("That connect attempt expired. Start it again.")
        return back
    _owner()
    try:
        got = google.ADAPTER.oauth_finish(code, pend["verifier"])
    except adhttp.AdHttpError as e:          # scrubbed: never carries a token or the code
        log.warning("google connect failed: %s", e)
        own = e.code in ("oauth.scope_missing", "oauth.no_refresh") and e.user_message
        flash(e.user_message if own else google.ERRORS.get(e.code or "") or G_FAILED)
        return back
    except Exception as e:  # noqa: BLE001  a malformed answer; log the kind only
        log.error("google connect failed: %s", type(e).__name__)
        flash(G_FAILED)
        return back
    now = db.now()
    keep = {"accounts": got["accounts"], "user_name": got["user_name"], "reconnect": pend.get("reconnect") or ""}
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, "
            "token_expires_at, scopes, status, pending_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.ws["id"], "google", "pending:" + secrets.token_hex(8), got["user_id"],
             adplatforms.seal_token(g.ws["id"], got["token"]), None, " ".join(got["scopes"]),
             "pending", json.dumps(keep), g.user["id"], now, now)).lastrowid
        conn.commit()
    return redirect(url_for("adconnect.pick", cid=cid))


def _pick_google(conn, row):
    """Pick the Google Ads customer (a client account; managers are shown as where it was reached from)."""
    _google_on()
    d = _data(row)
    accounts = {a["id"]: a for a in d.get("accounts") or []}
    pre = None
    if d.get("reconnect"):
        old = adplatforms.get(conn, g.ws["id"], d["reconnect"])
        pre = old["external_account_id"] if old else None
    if request.method == "GET":
        return render_template("ad_connect_google.html", row=row, d=d, pre=pre, checklist=google.checklist(),
                               dashed=google.dashed)
    a = accounts.get(request.form.get("account") or "")
    if not a or not a["usable"]:
        flash("Pick one of the Google Ads accounts listed.")
        return redirect(url_for("adconnect.pick", cid=row["id"]))
    now = db.now()
    same = conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='google' AND "
                        "external_account_id=? AND id!=?", (g.ws["id"], a["id"], row["id"])).fetchone()
    target = same["id"] if same else row["id"]
    if same:
        _revoke_replaced(conn, same, row)
        conn.execute("DELETE FROM ad_connections WHERE id=?", (row["id"],))
    conn.execute("UPDATE ad_connections SET external_account_id=?, account_name=?, currency=?, timezone=?, business_id=?, "
                 "page_id=NULL, page_name=NULL, instagram_actor_id=NULL, instagram_username=NULL, external_user_id=?, "
                 "token_enc=?, token_expires_at=NULL, scopes=?, updated_at=?, status='active', last_error=NULL, "
                 "pending_json=NULL WHERE id=?",
                 (a["id"], a["name"], a["currency"], a["timezone"], a.get("login"), row["external_user_id"],
                  row["token_enc"], row["scopes"], now, target))
    conn.commit()
    conv = ""
    if conversions.ws_enabled(g.ws, g.get("user")):   # M3.5: lead and purchase conversion actions, once connected
        import conversion_actions
        res = conversion_actions.sync(conn, adplatforms.get(conn, g.ws["id"], target))
        conv = " " + (res["words"] if res["status"] != "error" else
                      "Conversion actions weren't made: " + res["words"].rstrip(".") + ". Try again in Conversion tracking.")
    flash(f"Connected {a['name']} ({google.dashed(a['id'])})." + (" It's a Google Ads test account: campaigns there "
                                                                    "never run." if a.get("test") else "") + conv)
    return redirect(url_for("adconnect.settings"))


# ---------------------------------------------------------------- Snapchat (M6.1)

@bp.route("/connect/snapchat", methods=["POST"])
@login_required
def snapchat_start():
    _on()
    _snap_on()
    _owner()
    if not snapchat.ADAPTER.configured():
        flash("Connecting Snapchat isn't switched on yet. Try again later.")
        return redirect(url_for("adconnect.settings"))
    reconnect = request.form.get("reconnect") or ""
    if reconnect:
        with db.connect() as conn:
            reconnect = str(_pending(conn, reconnect)["id"]) if reconnect.isdigit() else ""
    state = secrets.token_urlsafe(24)
    session[SNAP_KEY] = {"state": state, "ws": g.ws["id"], "uid": g.user["id"], "reconnect": reconnect,
                         "at": int(time.time())}
    return redirect(snapchat.ADAPTER.oauth_start(state))


@bp.route("/connect/snapchat/callback")
def snapchat_callback():
    pend = session.pop(SNAP_KEY, None)
    if not g.get("user"):
        flash("Log in, then connect your Snapchat ad account again.")
        return redirect(url_for("login"))          # never a `next` carrying the code
    _on()
    _snap_on()
    back = redirect(url_for("adconnect.settings"))
    if request.args.get("error"):
        log.info("snapchat connect refused by the person: %s", (request.args.get("error") or "")[:40])
        flash("Snapchat didn't connect: the request was cancelled. Nothing changed.")
        return back
    sent, code = request.args.get("state") or "", request.args.get("code") or ""
    if not pend or not sent or not code or not secrets.compare_digest(sent, str(pend.get("state") or "")) \
            or pend.get("ws") != g.ws["id"] or pend.get("uid") != g.user["id"] \
            or time.time() - int(pend.get("at") or 0) > STATE_TTL_S:
        flash("That connect attempt expired. Start it again.")
        return back
    _owner()
    try:
        got = snapchat.ADAPTER.oauth_finish(code)
    except adhttp.AdHttpError as e:          # scrubbed: never carries a token or the code
        log.warning("snapchat connect failed: %s", e)
        flash(e.user_message if e.code == "oauth.no_refresh" and e.user_message else SNAP_FAILED)
        return back
    except Exception as e:  # noqa: BLE001  a malformed answer; log the kind only
        log.error("snapchat connect failed: %s", type(e).__name__)
        flash(SNAP_FAILED)
        return back
    now = db.now()
    keep = {"accounts": got["accounts"], "pages": got["pages"], "user_name": got["user_name"],
            "reconnect": pend.get("reconnect") or ""}
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, "
            "token_expires_at, scopes, status, pending_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.ws["id"], "snapchat", "pending:" + secrets.token_hex(8), got["user_id"],
             adplatforms.seal_token(g.ws["id"], got["token"]), None, " ".join(got["scopes"]),
             "pending", json.dumps(keep), g.user["id"], now, now)).lastrowid
        conn.commit()
    return redirect(url_for("adconnect.pick", cid=cid))


def _pick_snapchat(conn, row):
    """Pick the Snapchat ad account and the public profile its ads run from (one of the account's organization's),
    then the Snap Pixel (skipped when there are 0 or 1)."""
    _snap_on()
    d = _data(row)
    accounts = {a["id"]: a for a in d.get("accounts") or []}
    profiles = {p["id"]: p for p in d.get("pages") or []}
    pre = None
    if d.get("reconnect"):
        old = adplatforms.get(conn, g.ws["id"], d["reconnect"])
        pre = old["external_account_id"] if old else None
    if request.method == "GET":
        return render_template("ad_connect_snapchat.html", row=row, d=d, pre=pre, checklist=snapchat.checklist())
    a = accounts.get(request.form.get("account") or "")
    if not a or not a["usable"]:
        flash("Pick one of the ad accounts listed.")
        return redirect(url_for("adconnect.pick", cid=row["id"]))
    p = profiles.get(request.form.get("profile") or "")
    if request.form.get("profile") and (p is None or p.get("org_id") != a.get("org_id")):
        flash("Pick one of the public profiles listed for that ad account.")
        return redirect(url_for("adconnect.pick", cid=row["id"]))
    p = p or {}
    now = db.now()
    same = conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='snapchat' AND "
                        "external_account_id=? AND id!=?", (g.ws["id"], a["id"], row["id"])).fetchone()
    target = same["id"] if same else row["id"]
    if same:
        _revoke_replaced(conn, same, row)
        conn.execute("DELETE FROM ad_connections WHERE id=?", (row["id"],))
    conn.execute("UPDATE ad_connections SET external_account_id=?, account_name=?, currency=?, timezone=?, business_id=?, "
                 "page_id=?, page_name=?, instagram_actor_id=NULL, instagram_username=NULL, external_user_id=?, "
                 "token_enc=?, token_expires_at=NULL, scopes=?, updated_at=?, status='active', last_error=NULL, "
                 "pending_json=NULL WHERE id=?",
                 (a["id"], a["name"], a["currency"], a["timezone"], a.get("org_id"), p.get("id"), p.get("name"),
                  row["external_user_id"], row["token_enc"], row["scopes"], now, target))
    conn.commit()
    row = adplatforms.get(conn, g.ws["id"], target)
    try:
        pxs = snapchat.ADAPTER.list_pixels(adplatforms.open_token(row), a["id"])
    except adhttp.TokenInvalid as e:
        adplatforms.mark(conn, target, "expired", "Snapchat ended this access. Reconnect.")
        log.warning("snapchat pixels: %s", e)
        pxs = None
    except (adhttp.AdHttpError, secretsbox.BoxError) as e:
        log.warning("snapchat pixels: %s", e)
        pxs = None
    note = "" if p else " Pick a public profile before launching: reconnect once you've made one in Snapchat Ads Manager."
    if pxs is None:
        flash(f"Connected {a['name']}. We couldn't read its pixels; you can pick one later." + note)
        return redirect(url_for("adconnect.settings"))
    if len(pxs) == 1:
        _save_pixel(conn, row, pxs[0])
    if len(pxs) > 1:
        conn.execute("UPDATE ad_connections SET pending_json=? WHERE id=?", (json.dumps({"pixels": pxs}), target))
        conn.commit()
        return redirect(url_for("adconnect.pixel", cid=target))
    flash(f"Connected {a['name']}." + ("" if pxs else " It has no Snap Pixel yet: traffic campaigns run without one.") + note)
    return redirect(url_for("adconnect.settings"))


# ---------------------------------------------------------------- TikTok (M6.2)

@bp.route("/connect/tiktok", methods=["POST"])
@login_required
def tiktok_start():
    _on()
    _tt_on()
    _owner()
    if not tiktok.ADAPTER.configured():
        flash("Connecting TikTok isn't switched on yet. Try again later.")
        return redirect(url_for("adconnect.settings"))
    reconnect = request.form.get("reconnect") or ""
    if reconnect:
        with db.connect() as conn:
            reconnect = str(_pending(conn, reconnect)["id"]) if reconnect.isdigit() else ""
    state = secrets.token_urlsafe(24)
    session[TT_KEY] = {"state": state, "ws": g.ws["id"], "uid": g.user["id"], "reconnect": reconnect,
                       "at": int(time.time())}
    return redirect(tiktok.ADAPTER.oauth_start(state))


@bp.route("/connect/tiktok/callback")
def tiktok_callback():
    pend = session.pop(TT_KEY, None)
    if not g.get("user"):
        flash("Log in, then connect your TikTok ad account again.")
        return redirect(url_for("login"))          # never a `next` carrying the code
    _on()
    _tt_on()
    back = redirect(url_for("adconnect.settings"))
    sent, code = request.args.get("state") or "", request.args.get("auth_code") or request.args.get("code") or ""
    if request.args.get("error") or (sent and not code):
        log.info("tiktok connect refused by the person")
        flash("TikTok didn't connect: the request was cancelled. Nothing changed.")
        return back
    if not pend or not sent or not code or not secrets.compare_digest(sent, str(pend.get("state") or "")) \
            or pend.get("ws") != g.ws["id"] or pend.get("uid") != g.user["id"] \
            or time.time() - int(pend.get("at") or 0) > STATE_TTL_S:
        flash("That connect attempt expired. Start it again.")
        return back
    _owner()
    try:
        got = tiktok.ADAPTER.oauth_finish(code)
    except adhttp.AdHttpError as e:          # scrubbed: never carries a token or the code
        log.warning("tiktok connect failed: %s", e)
        flash(TT_FAILED)
        return back
    except Exception as e:  # noqa: BLE001  a malformed answer; log the kind only
        log.error("tiktok connect failed: %s", type(e).__name__)
        flash(TT_FAILED)
        return back
    now = db.now()
    keep = {"accounts": got["accounts"], "user_name": got["user_name"], "reconnect": pend.get("reconnect") or ""}
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, "
            "token_expires_at, scopes, status, pending_json, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.ws["id"], "tiktok", "pending:" + secrets.token_hex(8), got["user_id"],
             adplatforms.seal_token(g.ws["id"], got["token"]), None, " ".join(got["scopes"]),
             "pending", json.dumps(keep), g.user["id"], now, now)).lastrowid
        conn.commit()
    return redirect(url_for("adconnect.pick", cid=cid))


def _pick_tiktok(conn, row):
    """Pick the TikTok advertiser account, then (.../identity) the identity its ads show, then the pixel."""
    _tt_on()
    d = _data(row)
    accounts = {a["id"]: a for a in d.get("accounts") or []}
    pre = None
    if d.get("reconnect"):
        old = adplatforms.get(conn, g.ws["id"], d["reconnect"])
        pre = old["external_account_id"] if old else None
    if request.method == "GET":
        return render_template("ad_connect_tiktok.html", step="account", row=row, d=d, pre=pre,
                               checklist=tiktok.checklist())
    a = accounts.get(request.form.get("account") or "")
    if not a or not a["usable"]:
        flash("Pick one of the ad accounts listed.")
        return redirect(url_for("adconnect.pick", cid=row["id"]))
    now = db.now()
    same = conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='tiktok' AND "
                        "external_account_id=? AND id!=?", (g.ws["id"], a["id"], row["id"])).fetchone()
    target = same["id"] if same else row["id"]
    if same:
        _revoke_replaced(conn, same, row)
        conn.execute("DELETE FROM ad_connections WHERE id=?", (row["id"],))
    conn.execute("UPDATE ad_connections SET external_account_id=?, account_name=?, currency=?, timezone=?, business_id=NULL, "
                 "page_id=?, page_name=?, instagram_actor_id=NULL, instagram_username=NULL, external_user_id=?, "
                 "token_enc=?, token_expires_at=NULL, scopes=?, updated_at=?, status='active', last_error=NULL, "
                 "pending_json=NULL WHERE id=?",
                 (a["id"], a["name"], a["currency"], a["timezone"], same["page_id"] if same else None,
                  same["page_name"] if same else None, row["external_user_id"], row["token_enc"], row["scopes"], now,
                  target))
    conn.commit()
    row = adplatforms.get(conn, g.ws["id"], target)
    token = adplatforms.open_token(row)
    try:
        ids = tiktok.ADAPTER.list_identities(token, a["id"])
        pxs = tiktok.ADAPTER.list_pixels(token, a["id"])
    except adhttp.TokenInvalid as e:
        adplatforms.mark(conn, target, "expired", "TikTok ended this access. Reconnect.")
        log.warning("tiktok identities: %s", e)
        flash(f"Connected {a['name']}, but TikTok ended the access right away. Reconnect it.")
        return redirect(url_for("adconnect.settings"))
    except adhttp.AdHttpError as e:
        log.warning("tiktok identities: %s", e)
        ids, pxs = [], None
    conn.execute("UPDATE ad_connections SET pending_json=? WHERE id=?",
                 (json.dumps({"identities": ids, "pixels": pxs, "brand": g.ws["brand_name"] or g.ws["name"] or ""}), target))
    conn.commit()
    return redirect(url_for("adconnect.identity", cid=target))


@bp.route("/app/settings/ad-accounts/<int:cid>/identity", methods=["GET", "POST"])
@login_required
def identity(cid):
    """TikTok: the custom identity (name and avatar) the ads show. Pick one, or Campaignify makes one with the name
    typed here (TikTok's /identity/create/; the avatar can be added in TikTok Ads Manager)."""
    _on()
    _tt_on()
    _owner()
    with db.connect() as conn:
        row = _pending(conn, cid)
        d = _data(row)
        if row["platform"] != "tiktok" or row["status"] != "active" or "identities" not in d:
            return redirect(url_for("adconnect.settings"))
        ids = {i["id"]: i for i in d.get("identities") or []}
        if request.method == "GET":
            return render_template("ad_connect_tiktok.html", step="identity", row=row, d=d)
        choice = request.form.get("identity") or ""
        if choice == "new":
            name = " ".join((request.form.get("display_name") or "").split())[:40]
            if not name:
                flash("Type the name the ads should show.")
                return redirect(url_for("adconnect.identity", cid=cid))
            try:
                ident = tiktok.ADAPTER.create_identity(adplatforms.open_token(row), row["external_account_id"], name)
            except adhttp.AdHttpError as e:
                log.warning("tiktok identity create: %s", e)
                flash("TikTok didn't make the identity" + (f": {e.user_message[:160]}" if e.user_message else ".")
                      + " Try another name.")
                return redirect(url_for("adconnect.identity", cid=cid))
        elif choice in ids:
            ident = ids[choice]
        else:
            flash("Pick one of the identities listed, or make one.")
            return redirect(url_for("adconnect.identity", cid=cid))
        conn.execute("UPDATE ad_connections SET page_id=?, page_name=?, pending_json=NULL, updated_at=? WHERE id=?",
                     (ident["id"], ident["name"], db.now(), cid))
        conn.commit()
        row = adplatforms.get(conn, g.ws["id"], cid)
        pxs = d.get("pixels")
        if pxs is None:
            flash(f"Connected {row['account_name']}. We couldn't read its pixels; you can pick one later.")
            return redirect(url_for("adconnect.settings"))
        if len(pxs) == 1:
            _save_pixel(conn, row, pxs[0])
        if len(pxs) > 1:
            conn.execute("UPDATE ad_connections SET pending_json=? WHERE id=?", (json.dumps({"pixels": pxs}), cid))
            conn.commit()
            return redirect(url_for("adconnect.pixel", cid=cid))
    flash(f"Connected {row['account_name']}." + ("" if pxs else " It has no TikTok Pixel yet: traffic campaigns run "
                                                          "without one."))
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
