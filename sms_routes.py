"""Settings → Sending → Text messaging (the registration wizard) and Twilio's webhooks."""
import json

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import escape

import db
import flows_engine
import messaging
import plans
import sms
import smser
from core import login_required, owner_required

bp = Blueprint("sms", __name__)
FIELDS = ("legal_name", "business_type", "ein", "website", "street", "city", "state", "zip", "first_name", "last_name",
          "email", "phone", "job_title", "volume", "area_code", "use_case", "message_flow")


@bp.route("/app/settings/sms", methods=["GET", "POST"])
@login_required
def wizard():
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
        p = sms.profile(conn, ws["id"])
        if request.method == "POST":
            if g.user["role"] != "owner" and not g.user["is_admin"]:
                abort(403)
            if not plans.has(ws, "flows_sms"):
                return render_template("upgrade.html", feature=plans.FEATURES["flows_sms"], need=plans.required("flows_sms")), 402
            f = request.form
            vals = {k: " ".join(str(f.get(k) or "").split())[:600 if k in ("use_case", "message_flow") else 120] for k in FIELDS}
            path = "10dlc" if f.get("path") == "10dlc" else "tollfree"
            if p and p["status"] not in ("none", "collecting", "rejected"):
                flash("Your registration is already in. You can't change it while it's being reviewed.")
                return redirect(url_for("sms.wizard"))
            now = db.now()
            if p:
                conn.execute("UPDATE sms_profiles SET path=?, fields_json=?, status=CASE WHEN status='rejected' THEN 'collecting' "
                             "ELSE 'collecting' END, updated_at=? WHERE workspace_id=?", (path, json.dumps(vals), now, ws["id"]))
            else:
                conn.execute("INSERT INTO sms_profiles (workspace_id, path, status, fields_json, created_at, updated_at) VALUES "
                             "(?,?,?,?,?,?)", (ws["id"], path, "collecting", json.dumps(vals), now, now))
            conn.commit()
            if f.get("action") == "register":
                missing = [k for k in ("legal_name", "website", "street", "city", "state", "zip", "first_name", "last_name",
                                       "email", "phone", "use_case", "message_flow") if not vals[k]]
                if path == "10dlc" and not vals["ein"] and vals["business_type"] != "sole_proprietor":
                    missing.append("ein")
                why = sms.sms_ready(ws)
                why = [w for w in why if "Finish registering" not in w]
                if missing:
                    flash("Fill in: " + ", ".join(m.replace("_", " ") for m in missing) + ".")
                elif why:
                    flash(" ".join(why))
                else:
                    try:
                        num = sms.register(conn, ws, path, g.user["email"])
                        conn.commit()
                        flash(f"Submitted. Your number is {num}; texts start once it's approved.", "ok")
                    except smser.SmsError as e:
                        conn.rollback()
                        flash(f"Couldn't register: {e}")
            else:
                flash("Saved.", "ok")
            return redirect(url_for("sms.wizard"))
        fields = json.loads(p["fields_json"] or "{}") if p else {}
        brand = flows_engine._brand(conn, ws["id"])
        form = conn.execute("SELECT * FROM forms WHERE workspace_id=? AND consent_sms=1 ORDER BY id DESC LIMIT 1", (ws["id"],)).fetchone()
        samples = sms.sample_messages(conn, ws)
    path = (p["path"] if p else "") or ("10dlc" if int(fields.get("volume") or 0) >= sms.LOW_VOLUME else "tollfree")
    defaults = {"use_case": f"Marketing texts to people who opted in on {(brand['name'] + "'s") if brand['name'] else 'our'} web form: launch news, "
                            "reminders and offers they asked for.",
                "message_flow": ("People opt in by ticking an unchecked box on our web form" +
                                 (f" at {flows_engine.PUBLIC_URL}/f/{form['code']}" if form else "") +
                                 ", next to the disclosure: " + (messaging.DEFAULT_SMS_CONSENT.replace("{brand}", brand["name"] or "us"))),
                "business_type": "llc", "email": ws["reply_to"] or g.user["email"]}
    return render_template("sms.html", ws=ws, p=p, f=dict(defaults, **{k: v for k, v in fields.items() if v}), path=path,
                           fees={k: sms.fees(k, int(fields.get("volume") or 0)) for k in ("tollfree", "10dlc")},
                           seg=sms.credits_per_segment(), samples=samples, connected=smser.configured(),
                           has=plans.has(ws, "flows_sms"), form=form, low=sms.LOW_VOLUME)


def _twiml(text=None):
    body = f"<Response><Message>{escape(text)}</Message></Response>" if text else "<Response></Response>"
    return Response('<?xml version="1.0" encoding="UTF-8"?>' + body, mimetype="text/xml")


@bp.route("/webhooks/twilio/sms", methods=["POST"])
def inbound():
    params = request.form.to_dict()
    url = flows_engine.PUBLIC_URL + request.path
    if not smser.validate_signature(url, params, request.headers.get("X-Twilio-Signature", "")):
        return "bad signature", 403
    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    with db.connect() as conn:
        if sid and conn.execute("SELECT 1 FROM webhook_events WHERE provider='twilio' AND event_id=?", (sid,)).fetchone():
            return _twiml()
        if sid:
            conn.execute("INSERT INTO webhook_events (provider, event_id, kind, at) VALUES ('twilio', ?, 'inbound', ?)", (sid, db.now()))
        reply = sms.inbound(conn, params.get("To", ""), params.get("From", ""), params.get("Body", ""))
        conn.commit()
    return _twiml(reply)


@bp.route("/webhooks/twilio/status", methods=["POST"])
def status():
    params = request.form.to_dict()
    if not smser.validate_signature(flows_engine.PUBLIC_URL + request.path, params, request.headers.get("X-Twilio-Signature", "")):
        return "bad signature", 403
    st = (params.get("MessageStatus") or "").lower()
    new = {"delivered": "delivered", "undelivered": "failed", "failed": "failed"}.get(st)
    if new:
        with db.connect() as conn:
            conn.execute("UPDATE messages SET status=?, delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END, "
                         "error=CASE WHEN ?='failed' THEN ? ELSE error END WHERE provider_id=?",
                         (new, new, db.now(), new, f"Carrier: {params.get('ErrorCode') or st}", params.get("MessageSid", "")))
            conn.commit()
    return "", 204
