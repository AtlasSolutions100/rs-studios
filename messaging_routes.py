"""Public message endpoints (tracked links, opens, one-click unsubscribe, the
provider's webhooks) and the Sending settings, contact import and Messaging
Policy pages."""
import csv
import io
import json

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for)

import db
import flows_engine
import forms
import mailer
import messaging
import plans
from core import login_required, owner_required

bp = Blueprint("messaging", __name__)
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00"
         b"\x01\x00\x00\x02\x02D\x01\x00;")


@bp.route("/t/<token>")
def click(token):
    """A tracked link from an email: count it, tell the flow, then go where it pointed."""
    with db.connect() as conn:
        ln = conn.execute("SELECT l.*, m.workspace_id, m.contact_id, m.id mid FROM message_links l JOIN messages m ON "
                          "m.id=l.message_id WHERE l.token=?", (token,)).fetchone()
        if not ln:
            abort(404)
        now = db.now()
        conn.execute("UPDATE message_links SET clicks=clicks+1 WHERE id=?", (ln["id"],))
        conn.execute("UPDATE messages SET clicked_at=COALESCE(clicked_at, ?), opened_at=COALESCE(opened_at, ?) WHERE id=?",
                     (now, now, ln["mid"]))
        if ln["contact_id"]:
            conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                         (ln["workspace_id"], ln["contact_id"], "msg_click", str(ln["mid"]), ln["url"][:200], now))
        conn.commit()
    url = ln["url"]
    if url.startswith(flows_engine.PUBLIC_URL + "/p/"):
        url += ("&" if "?" in url else "?") + "cfy_c=" + token   # our own pages can log the visit
    return redirect(url, code=302)


@bp.route("/t/o/<token>.gif")
def open_pixel(token):
    """Opens are approximate (some mail apps load images for everyone)."""
    with db.connect() as conn:
        conn.execute("UPDATE messages SET opened_at=COALESCE(opened_at, ?) WHERE token=?", (db.now(), token))
        conn.commit()
    return Response(PIXEL, mimetype="image/gif", headers={"Cache-Control": "no-store"})


@bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """One click, no login. POST from the List-Unsubscribe-Post header or the button here."""
    with db.connect() as conn:
        msg = conn.execute("SELECT * FROM messages WHERE token=?", (token,)).fetchone()
        if not msg:
            abort(404)
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (msg["workspace_id"],)).fetchone()
        brand = flows_engine._brand(conn, ws["id"])
        sender = ws["sender_name"] or brand["name"] or "this sender"
        done = False
        if request.method == "POST":
            messaging.unsubscribe(conn, msg, "unsubscribe", (request.form.get("why") or "one-click")[:200])
            conn.commit()
            done = True
            if not request.form:   # mail apps' one-click POST
                return Response("Unsubscribed.", mimetype="text/plain")
        c = conn.execute("SELECT unsubscribed_at FROM contacts WHERE id=?", (msg["contact_id"],)).fetchone()
    return render_template("unsubscribe.html", token=token, sender=sender, done=done or bool(c and c["unsubscribed_at"]))


@bp.route("/webhooks/resend", methods=["POST"])
def resend_webhook():
    raw = request.get_data()
    if not mailer.verify_webhook(request.headers, raw):
        return "bad signature", 400
    try:
        ev = json.loads(raw)
    except ValueError:
        return "bad body", 400
    eid = request.headers.get("svix-id") or ev.get("id") or ""
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM webhook_events WHERE provider='resend' AND event_id=?", (eid,)).fetchone():
            return "already seen", 200
        conn.execute("INSERT INTO webhook_events (provider, event_id, kind, at) VALUES ('resend', ?, ?, ?)",
                     (eid, ev.get("type"), db.now()))
        result = messaging.handle_event(conn, ev)
        conn.commit()
    return result, 200


# ---------------------------------------------------------------- settings

@bp.route("/app/settings/sending", methods=["GET", "POST"])
@login_required
def sending():
    if request.method == "POST":
        if g.user["role"] != "owner" and not g.user["is_admin"]:
            abort(403)
        f = request.form
        vals = {"sender_name": " ".join((f.get("sender_name") or "").split())[:60] or None,
                "reply_to": (f.get("reply_to") or "").strip().lower()[:200] or None,
                "postal_address": " ".join((f.get("postal_address") or "").split())[:300] or None,
                "consent_manual": 1 if f.get("consent_manual") else 0}
        if vals["reply_to"] and not forms.EMAIL_RX.match(vals["reply_to"]):
            flash("Reply-to must be an email address.")
            return redirect(url_for("messaging.sending"))
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET sender_name=?, reply_to=?, postal_address=?, consent_manual=? WHERE id=?",
                         (*vals.values(), g.ws["id"]))
            if f.get("accept") and not g.ws["messaging_accepted_at"]:
                conn.execute("UPDATE workspaces SET messaging_accepted_at=?, messaging_accepted_by=? WHERE id=?",
                             (f"{db.now()} v{messaging.POLICY_VERSION}", g.user["id"], g.ws["id"]))
            conn.commit()
        flash("Saved.", "ok")
        return redirect(url_for("messaging.sending"))
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        stats = conn.execute("SELECT COUNT(*) n, SUM(status IN ('delivered','sent')) ok, SUM(status='bounced') b, "
                             "SUM(status='complained') c FROM messages WHERE workspace_id=? AND channel='email' AND sent_at "
                             ">= datetime('now','-30 day')", (g.ws["id"],)).fetchone()
        n_sup = conn.execute("SELECT COUNT(*) FROM suppressions WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0]
        cap = messaging.daily_cap(conn, ws, flows_engine.utcnow()) if plans.has(ws, "flows_email") else 0
        accepted_by = conn.execute("SELECT email FROM users WHERE id=?", (ws["messaging_accepted_by"] or 0,)).fetchone()
        domains = [dict(d, records=json.loads(d["dns_json"] or "[]")) for d in conn.execute(
            "SELECT * FROM sender_domains WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],)).fetchall()]
    return render_template("sending.html", ws=ws, ready=messaging.email_ready(ws), connected=mailer.configured(),
                           mail_domain=mailer.domain() or "mail.campaignify.ai", stats=stats, n_sup=n_sup, cap=cap,
                           credits=messaging.EMAIL_CREDITS, accepted_by=accepted_by["email"] if accepted_by else None,
                           has_email=plans.has(ws, "flows_email"), domains=domains, extra=_extra())


def _upgrade(feature):
    return render_template("upgrade.html", feature=plans.FEATURES[feature], need=plans.required(feature)), 402


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


_EXTRA = []   # (template name, context fn): later phases add sections (custom domains, text messaging)


def _extra():
    return [(tpl, fn()) for tpl, fn in _EXTRA]


DOMAIN_RX = __import__("re").compile(r"^(?=.{4,190}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


@bp.route("/app/settings/sending/domain", methods=["POST"])
@owner_required
def domain():
    """Your own sending domain (Growth+, D6): add it at the email service, show the DNS records, check them.
    Until it's verified, mail keeps going from the shared domain."""
    if not plans.has(g.ws, "flows_email"):
        return _upgrade("flows_email")
    action = request.form.get("action")
    with db.connect() as conn:
        if action == "add":
            name = (request.form.get("domain") or "").strip().lower().removeprefix("www.")
            local = __import__("re").sub(r"[^a-z0-9._-]", "", (request.form.get("local_part") or "hello").lower())[:40] or "hello"
            if not DOMAIN_RX.match(name) or name.endswith("campaignify.ai"):
                flash("Use a domain you own, like yourbrand.com or mail.yourbrand.com.")
                return redirect(url_for("messaging.sending") + "#domain")
            if not mailer.configured():
                flash("Email sending isn't connected yet, so domains can't be added.")
                return redirect(url_for("messaging.sending") + "#domain")
            try:
                d = mailer.add_domain(name)
            except mailer.MailError as e:
                flash(f"The email service refused it: {e}")
                return redirect(url_for("messaging.sending") + "#domain")
            conn.execute("DELETE FROM sender_domains WHERE workspace_id=?", (g.ws["id"],))
            conn.execute("INSERT INTO sender_domains (workspace_id, domain, local_part, resend_domain_id, status, dns_json, "
                         "created_at) VALUES (?,?,?,?,?,?,?)", (g.ws["id"], name, local, d.get("id"), "pending",
                                                                json.dumps(d.get("records") or []), db.now()))
            flash("Added. Put these records in your domain's DNS, then press Check.", "ok")
        else:
            d = conn.execute("SELECT * FROM sender_domains WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (g.ws["id"],)).fetchone()
            if not d:
                abort(404)
            if action == "remove":
                conn.execute("DELETE FROM sender_domains WHERE id=?", (d["id"],))
                flash("Removed. Mail goes from the shared domain again.", "ok")
            elif action == "check":
                try:
                    info = mailer.verify_domain(d["resend_domain_id"])
                except mailer.MailError as e:
                    flash(f"Couldn't check: {e}")
                    return redirect(url_for("messaging.sending") + "#domain")
                st = "verified" if info.get("status") == "verified" else "failed" if info.get("status") == "failed" else "pending"
                conn.execute("UPDATE sender_domains SET status=?, dns_json=?, checked_at=?, verified_at=CASE WHEN ?='verified' "
                             "THEN COALESCE(verified_at, ?) ELSE NULL END WHERE id=?",
                             (st, json.dumps(info.get("records") or json.loads(d["dns_json"] or "[]")), db.now(), st, db.now(),
                              d["id"]))
                flash({"verified": f"Verified. Mail now goes from {d['local_part']}@{d['domain']}.",
                       "failed": "The records don't match yet. Check each one below; DNS can take up to a day.",
                       "pending": "Not verified yet. DNS changes can take a while; check again later."}[st],
                      "ok" if st == "verified" else "")
        conn.commit()
    return redirect(url_for("messaging.sending") + "#domain")


@bp.route("/app/contacts/import", methods=["POST"])
@login_required
def import_contacts():
    """CSV with columns email, name, phone, tags, email_consent (yes/no). Consent is only recorded when the
    member says it was given, with where it came from."""
    if not plans.has(g.ws, "forms"):
        return _upgrade("forms")
    fs = request.files.get("file")
    if not fs:
        flash("Pick a CSV file.")
        return redirect(url_for("forms.contacts"))
    text = fs.read(2_000_000).decode("utf-8-sig", errors="replace")
    source = " ".join((request.form.get("source") or "").split())[:120]
    rows = list(csv.DictReader(io.StringIO(text)))[:5000]
    added = updated = consented = 0
    now = db.now()
    with db.connect() as conn:
        for r in rows:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            email = r.get("email", "").lower()[:200]
            if not forms.EMAIL_RX.match(email):
                continue
            yes = r.get("email_consent", r.get("consent", "")).lower() in ("yes", "y", "true", "1", "opted in")
            tags = ",".join(t for t in (x.strip().lower()[:30] for x in r.get("tags", "").split(",")) if t)
            c = conn.execute("SELECT * FROM contacts WHERE workspace_id=? AND email=?", (g.ws["id"], email)).fetchone()
            if c:
                merged = ",".join(sorted(set(filter(None, (c["tags"] or "").split(","))) | set(filter(None, tags.split(",")))))
                conn.execute("UPDATE contacts SET name=COALESCE(name, ?), phone=COALESCE(phone, ?), tags=? WHERE id=?",
                             (r.get("name") or None, r.get("phone") or None, merged, c["id"]))
                cid, updated = c["id"], updated + 1
                before = set(filter(None, (c["tags"] or "").split(",")))
            else:
                cid = conn.execute("INSERT INTO contacts (workspace_id, email, name, phone, tags, source, first_seen, last_seen) "
                                   "VALUES (?,?,?,?,?,?,?,?)", (g.ws["id"], email, r.get("name") or None, r.get("phone") or None,
                                                                tags, "import", now, now)).lastrowid
                added, before = added + 1, set()
                flows_engine.enrol(conn, g.ws["id"], "contact_added", cid)
            if yes and not (c and c["unsubscribed_at"]):
                conn.execute("UPDATE contacts SET email_consent='import', email_consent_at=?, email_consent_detail=? WHERE id=? "
                             "AND email_consent='none'", (now, json.dumps({"import": source or fs.filename, "by": g.user["email"]}),
                                                          cid))
                consented += 1
            flows_engine.enrol_tags(conn, g.ws["id"], cid, before, set(filter(None, tags.split(","))) | before)
        conn.commit()
    flash(f"Imported: {added} new, {updated} updated, {consented} with email consent.", "ok")
    return redirect(url_for("forms.contacts"))


@bp.route("/app/contacts/<int:cid>/consent", methods=["POST"])
@owner_required
def set_consent(cid):
    """Manual consent change, with an audit note."""
    ch = "sms" if request.form.get("channel") == "sms" else "email"
    val = "manual" if request.form.get("value") == "yes" else "none"
    why = " ".join((request.form.get("why") or "").split())[:200]
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
        if not c:
            abort(404)
        if val == "manual" and ch == "email" and c["unsubscribed_at"]:
            flash("They unsubscribed. Only they can opt back in, from a form.")
            return redirect(url_for("forms.contact", cid=cid))
        conn.execute(f"UPDATE contacts SET {ch}_consent=?, {ch}_consent_at=?, {ch}_consent_detail=? WHERE id=?",
                     (val, db.now(), json.dumps({"by": g.user["email"], "why": why}), cid))
        conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                     (g.ws["id"], cid, "consent", ch, f"{ch} consent set to {val} by {g.user['email']}: {why}", db.now()))
        conn.commit()
    flash("Consent updated.", "ok")
    return redirect(url_for("forms.contact", cid=cid))


@bp.route("/messaging-policy")
def policy():
    return render_template("messaging_policy.html", version=messaging.POLICY_VERSION)
