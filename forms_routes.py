"""Forms (builder, hosted form, page block submissions, webhooks) and the
contacts list (Growth+)."""
import csv
import io
import json
import os
import secrets

from flask import (Blueprint, Response, abort, current_app, flash, g, make_response, redirect, render_template, request,
                   url_for)

import compliance
import db
import forms
import pages
from core import feature_required

bp = Blueprint("forms", __name__)
SUBMITS_PER_IP_HOUR = 30
SUBMITS_PER_IP_FORM_10MIN = 6
DEFAULT_FIELDS = [{"key": "name", "label": "Name", "type": "text", "required": False, "options": [], "show_if": None},
                  {"key": "email", "label": "Email", "type": "email", "required": True, "options": [], "show_if": None}]


def _form(fid):
    with db.connect() as conn:
        f = conn.execute("SELECT * FROM forms WHERE id=? AND workspace_id=? AND archived=0", (fid, g.ws["id"])).fetchone()
    if not f:
        abort(404)
    return f


def _clean_tags(text):
    tags = []
    for t in (text or "").split(","):
        t = " ".join(t.strip().lower().split())[:30]
        if t and t not in tags:
            tags.append(t)
    return tags[:15]


# ---------------------------------------------------------------- builder

@bp.route("/app/forms", methods=["GET", "POST"])
@feature_required("forms")
def index():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:80] or "Sign-up form"
        with db.connect() as conn:
            fid = conn.execute("INSERT INTO forms (workspace_id, code, name, fields_json, button_text, success_message, "
                               "webhook_secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (g.ws["id"], secrets.token_urlsafe(9), name, json.dumps(DEFAULT_FIELDS), "Send",
                                "Thanks! We'll be in touch.", secrets.token_hex(16), db.now(), db.now())).lastrowid
            conn.commit()
        return redirect(url_for("forms.edit", fid=fid))
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM forms WHERE workspace_id=? AND archived=0 ORDER BY id DESC", (g.ws["id"],)).fetchall()
    return render_template("forms.html", rows=rows, base=request.host_url)


def _fields_from_form(f):
    raw, i = [], 0
    while f.get(f"f{i}_label") is not None and i < forms.MAX_FIELDS + 1:
        raw.append({"key": f.get(f"f{i}_key"), "label": f.get(f"f{i}_label"), "type": f.get(f"f{i}_type"),
                    "required": f.get(f"f{i}_required") == "1", "options": f.get(f"f{i}_options"),
                    "show_if_field": f.get(f"f{i}_show_if_field"), "show_if_value": f.get(f"f{i}_show_if_value")})
        i += 1
    return raw


@bp.route("/app/forms/<int:fid>", methods=["GET", "POST"])
@feature_required("forms")
def edit(fid):
    fm = _form(fid)
    if request.method == "GET":
        return render_template("form_edit.html", fm=fm, fields=json.loads(fm["fields_json"] or "[]"),
                               TYPES=forms.FIELD_TYPES, base=request.host_url)
    f = request.form
    action = f.get("action") or "save"
    raw = _fields_from_form(f)
    if action.startswith("add:") and action[4:] in forms.FIELD_TYPES and len(raw) < forms.MAX_FIELDS:
        raw.append({"label": forms.FIELD_TYPES[action[4:]], "type": action[4:]})
    elif action.startswith(("up:", "down:", "del:")):
        try:
            i = int(action.split(":")[1])
        except ValueError:
            i = -1
        if 0 <= i < len(raw):
            if action.startswith("del:"):
                raw.pop(i)
            elif action.startswith("up:") and i > 0:
                raw[i - 1], raw[i] = raw[i], raw[i - 1]
            elif action.startswith("down:") and i < len(raw) - 1:
                raw[i + 1], raw[i] = raw[i], raw[i + 1]
    fields = forms.clean_fields(raw)
    webhook = (f.get("webhook_url") or "").strip()[:500]
    if webhook:
        why = forms.webhook_url_ok(webhook)
        if why:
            flash(f"Webhook not saved: {why}")
            webhook = fm["webhook_url"] or ""
    redirect_url = pages.safe_url((f.get("redirect_url") or "").strip()[:500])
    chk = compliance.check(f.get("name") or "", f.get("success_message") or "", " ".join(x["label"] for x in fields))
    if not chk["ok"]:
        for m in chk["blocks"]:
            flash("Not saved: " + m)
        return redirect(url_for("forms.edit", fid=fid))
    secret = fm["webhook_secret"] or secrets.token_hex(16)
    if action == "rotate":
        secret = secrets.token_hex(16)
        flash("New signing secret made. Update it wherever you check signatures.")
    with db.connect() as conn:
        conn.execute("UPDATE forms SET name=?, fields_json=?, button_text=?, success_message=?, redirect_url=?, webhook_url=?, "
                     "webhook_secret=?, tags=?, updated_at=? WHERE id=?",
                     ((f.get("name") or fm["name"]).strip()[:80], json.dumps(fields), (f.get("button_text") or "Send").strip()[:40],
                      (f.get("success_message") or "").strip()[:300], redirect_url or None, webhook or None, secret,
                      ",".join(_clean_tags(f.get("tags"))), db.now(), fid))
        conn.commit()
    if action == "test" and webhook:
        st = forms.post_webhook(webhook, secret, {"event": "form.test", "form": {"id": fid, "name": fm["name"]},
                                                  "data": {"email": "test@example.com"}, "sent_at": db.now()})
        flash(f"Test webhook sent: {st}")
    elif action == "save":
        flash("Saved.")
    return redirect(url_for("forms.edit", fid=fid))


@bp.route("/app/forms/<int:fid>/delete", methods=["POST"])
@feature_required("forms")
def delete(fid):
    _form(fid)
    with db.connect() as conn:
        conn.execute("UPDATE forms SET archived=1 WHERE id=? AND workspace_id=?", (fid, g.ws["id"]))
        conn.commit()
    flash("Form deleted. Its submissions and contacts are kept.")
    return redirect(url_for("forms.index"))


@bp.route("/app/forms/<int:fid>/submissions")
@feature_required("forms")
def submissions(fid):
    fm = _form(fid)
    fields = json.loads(fm["fields_json"] or "[]")
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM form_submissions WHERE form_id=? AND workspace_id=? ORDER BY id DESC LIMIT 500",
                            (fid, g.ws["id"])).fetchall()
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["submitted_at"] + [x["label"] for x in fields] + ["page", "smart_link", "utm_source", "utm_medium",
                                                                        "utm_campaign"])
        for r in rows:
            d = json.loads(r["data_json"])
            w.writerow([r["created_at"]] + [_csv_safe(d.get(x["key"], "")) for x in fields] +
                       [r["page_slug"] or "", r["link_code"] or "", r["utm_source"] or "", r["utm_medium"] or "",
                        r["utm_campaign"] or ""])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=form-{fid}-submissions.csv"})
    return render_template("form_subs.html", fm=fm, fields=fields, rows=rows, loads=json.loads)


def _csv_safe(v):
    """Stop spreadsheet formula injection in exported cells."""
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


# ---------------------------------------------------------------- public

def _public_form(code):
    with db.connect() as conn:
        fm = conn.execute("SELECT * FROM forms WHERE code=? AND archived=0", (code,)).fetchone()
        if not fm:
            abort(404)
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (fm["workspace_id"],)).fetchone()
    import plans
    if not plans.has(ws, "forms"):
        abort(404)
    return fm


def _hosted(fm, errors=None, values=None, sent=False, status=200):
    resp = make_response(render_template("form_public.html", fm=fm, fields=json.loads(fm["fields_json"] or "[]"),
                                         stamp=forms.stamp(current_app.secret_key, fm["code"]), errors=errors or [],
                                         values=values or {}, sent=sent, css=forms.logic_css, ctx={}), status)
    resp.headers["Content-Security-Policy"] = pages.CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/f/<code>", methods=["GET", "POST"])
def public(code):
    fm = _public_form(code)
    if request.method == "GET":
        return _hosted(fm, sent=request.args.get("sent") == "1")
    return submit(fm)


def submit(fm):
    f = request.form
    ip = request.remote_addr or "unknown"
    fields = json.loads(fm["fields_json"] or "[]")
    page_slug = (f.get("_page") or "").strip().lower()[:60]
    back_to_page = bool(page_slug)

    def fail(msg, status=400, values=None):
        if back_to_page:
            r = render_errors_page([msg])
            r.status_code = status
            return r
        return _hosted(fm, errors=[msg] if isinstance(msg, str) else msg, values=values, status=status)

    if f.get("website"):                                  # honeypot
        return fail("Something went wrong. Please try again.")
    if not forms.stamp_ok(current_app.secret_key, fm["code"], f.get("_t") or ""):
        return fail("This form expired or was sent too fast. Reload the page and try again.")
    if db.throttled(f"fs:{ip}", SUBMITS_PER_IP_HOUR, 3600) or \
            db.throttled(f"fs:{ip}:{fm['id']}", SUBMITS_PER_IP_FORM_10MIN, 600):
        return fail("Too many submissions. Try again later.", 429)
    db.hit(f"fs:{ip}")
    db.hit(f"fs:{ip}:{fm['id']}")
    values, errors = forms.validate(fields, f)
    if errors:
        if back_to_page:
            return render_errors_page(errors)
        return _hosted(fm, errors=errors, values={k: f.get(f"f_{k}", "") for k in (x["key"] for x in fields)})
    link_code = (f.get("_l") or "").strip()[:40]
    utm = {k: (f.get(k) or "").strip()[:80] for k in ("utm_source", "utm_medium", "utm_campaign")}
    referrer = (request.referrer or "")[:300]
    email = next((values[x["key"]] for x in fields if x["type"] == "email" and values.get(x["key"])), "").lower()
    name = next((values[x["key"]] for x in fields if x["type"] == "text" and "name" in x["key"] and values.get(x["key"])), "")
    phone = next((values[x["key"]] for x in fields if x["type"] == "phone" and values.get(x["key"])), "")
    now = db.now()
    with db.connect() as conn:
        page = conn.execute("SELECT slug FROM pages WHERE slug=? AND workspace_id=? AND status='published'",
                            (page_slug, fm["workspace_id"])).fetchone() if page_slug else None
        link = conn.execute("SELECT code, title FROM links WHERE code=? AND workspace_id=?",
                            (link_code, fm["workspace_id"])).fetchone() if link_code else None
        contact_id = None
        if email:
            c = conn.execute("SELECT * FROM contacts WHERE workspace_id=? AND email=?", (fm["workspace_id"], email)).fetchone()
            tags = _clean_tags(",".join(filter(None, [c["tags"] if c else "", fm["tags"] or ""])))
            if c:
                contact_id = c["id"]
                conn.execute("UPDATE contacts SET name=COALESCE(NULLIF(?, ''), name), phone=COALESCE(NULLIF(?, ''), phone), "
                             "tags=?, submissions=submissions+1, last_seen=? WHERE id=?",
                             (name, phone, ",".join(tags), now, contact_id))
            else:
                contact_id = conn.execute("INSERT INTO contacts (workspace_id, email, name, phone, tags, source, submissions, "
                                          "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                                          (fm["workspace_id"], email, name or None, phone or None, ",".join(tags),
                                           f"form: {fm['name']}", 1, now, now)).lastrowid
            if link:
                conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                             (fm["workspace_id"], contact_id, "link", link["code"], link["title"] or link["code"], now))
            if page:
                conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                             (fm["workspace_id"], contact_id, "page", page["slug"], f"/p/{page['slug']}", now))
            conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                         (fm["workspace_id"], contact_id, "form", str(fm["id"]), fm["name"], now))
        sid = conn.execute("INSERT INTO form_submissions (form_id, workspace_id, contact_id, data_json, page_slug, link_code, "
                           "referrer, utm_source, utm_medium, utm_campaign, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (fm["id"], fm["workspace_id"], contact_id, json.dumps(values), page["slug"] if page else None,
                            link["code"] if link else None, referrer, utm["utm_source"] or None, utm["utm_medium"] or None,
                            utm["utm_campaign"] or None, now)).lastrowid
        conn.execute("UPDATE forms SET submissions=submissions+1 WHERE id=?", (fm["id"],))
        conn.commit()
    if fm["webhook_url"]:
        payload = {"event": "form.submitted", "form": {"id": fm["id"], "name": fm["name"]}, "submission_id": sid,
                   "data": values, "page": page["slug"] if page else None, "smart_link": link["code"] if link else None,
                   "utm": {k: v for k, v in utm.items() if v}, "submitted_at": now}

        def done(status, sid=sid):
            with db.connect() as c2:
                c2.execute("UPDATE form_submissions SET webhook_status=? WHERE id=?", (status[:120], sid))
                c2.commit()
        forms.send_async(fm["webhook_url"], fm["webhook_secret"] or "", payload, done,
                         sync=os.environ.get("RS_WORKER", "1") != "1")
    if fm["redirect_url"]:
        return redirect(fm["redirect_url"], code=303)
    if page:
        return redirect(url_for("pages.public", slug=page["slug"], sent=fm["code"]) + f"#form-{fm['code']}", code=303)
    return redirect(url_for("forms.public", code=fm["code"], sent=1), code=303)


def render_errors_page(errors):
    resp = make_response(render_template("form_errors.html", errors=errors), 400)
    resp.headers["Content-Security-Policy"] = pages.CSP
    return resp


# ---------------------------------------------------------------- contacts

@bp.route("/app/contacts", methods=["GET", "POST"])
@feature_required("forms")
def contacts():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:200]
        if not forms.EMAIL_RX.match(email):
            flash("Add a valid email.")
            return redirect(url_for("forms.contacts"))
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM contacts WHERE workspace_id=? AND email=?", (g.ws["id"], email)).fetchone():
                flash("That contact is already on your list.")
            else:
                conn.execute("INSERT INTO contacts (workspace_id, email, name, tags, source, first_seen, last_seen) "
                             "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], email, (request.form.get("name") or "").strip()[:120] or None,
                                                        ",".join(_clean_tags(request.form.get("tags"))), "added by hand",
                                                        db.now(), db.now()))
                conn.commit()
                flash("Contact added.")
        return redirect(url_for("forms.contacts"))
    q = (request.args.get("q") or "").strip()[:100]
    tag = (request.args.get("tag") or "").strip().lower()[:30]
    sql = "SELECT * FROM contacts WHERE workspace_id=?"
    args = [g.ws["id"]]
    if q:
        sql += " AND (email LIKE ? OR name LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if tag:
        sql += " AND (',' || tags || ',') LIKE ?"
        args.append(f"%,{tag},%")
    with db.connect() as conn:
        rows = conn.execute(sql + " ORDER BY last_seen DESC LIMIT 500", args).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM contacts WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0]
        alltags = {}
        for r in conn.execute("SELECT tags FROM contacts WHERE workspace_id=?", (g.ws["id"],)):
            for t in filter(None, (r["tags"] or "").split(",")):
                alltags[t] = alltags.get(t, 0) + 1
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["email", "name", "phone", "tags", "source", "submissions", "first_seen", "last_seen"])
        for r in rows:
            w.writerow([_csv_safe(r[k] or "") for k in ("email", "name", "phone", "tags", "source")] +
                       [r["submissions"], r["first_seen"], r["last_seen"]])
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=contacts.csv"})
    return render_template("contacts.html", rows=rows, total=total, q=q, tag=tag,
                           tags=sorted(alltags.items(), key=lambda x: -x[1])[:30])


@bp.route("/app/contacts/<int:cid>", methods=["GET", "POST"])
@feature_required("forms")
def contact(cid):
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=? AND workspace_id=?", (cid, g.ws["id"])).fetchone()
        if not c:
            abort(404)
        if request.method == "POST":
            if request.form.get("action") == "delete":
                conn.execute("DELETE FROM contact_events WHERE contact_id=? AND workspace_id=?", (cid, g.ws["id"]))
                conn.execute("UPDATE form_submissions SET contact_id=NULL, data_json='{}' WHERE contact_id=? AND workspace_id=?",
                             (cid, g.ws["id"]))
                conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
                conn.commit()
                flash("Contact and their form answers deleted.")
                return redirect(url_for("forms.contacts"))
            conn.execute("UPDATE contacts SET name=?, phone=?, tags=?, notes=? WHERE id=?",
                         ((request.form.get("name") or "").strip()[:120] or None,
                          (request.form.get("phone") or "").strip()[:30] or None,
                          ",".join(_clean_tags(request.form.get("tags"))), (request.form.get("notes") or "")[:4000], cid))
            conn.commit()
            flash("Saved.")
            return redirect(url_for("forms.contact", cid=cid))
        events = conn.execute("SELECT * FROM contact_events WHERE contact_id=? AND workspace_id=? ORDER BY id DESC LIMIT 200",
                              (cid, g.ws["id"])).fetchall()
        subs = conn.execute("SELECT s.*, f.name form_name, f.fields_json FROM form_submissions s JOIN forms f ON f.id=s.form_id "
                            "WHERE s.contact_id=? AND s.workspace_id=? ORDER BY s.id DESC LIMIT 50", (cid, g.ws["id"])).fetchall()
    return render_template("contact.html", c=c, events=events, subs=subs, loads=json.loads)
