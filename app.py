"""Campaignify: make ads for your products from templates that already work.

Flask app: a public landing page, self-serve accounts (invites still join an
existing workspace), one workspace per team, a brand kit (product
name, approved claims, product photos), templates and tools, a library of
everything made, and RS credits (free starter credits, then packs bought
through Stripe Checkout, billing.py). Generation runs in worker.py.
"""
import functools
import json
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import uuid

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import billing
import catalog
import owners
import compliance
import db
import hf
import worker

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(db.DATA, "uploads")
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
MAX_PHOTO_MB = 25
MAX_VIDEO_MB = 150
# Signup abuse limits (per client IP): new accounts per hour and per day.
SIGNUPS_PER_HOUR, SIGNUPS_PER_DAY = 5, 10
# Failed logins allowed per IP+email and per IP in 15 minutes.
LOGIN_FAILS_PER_EMAIL, LOGIN_FAILS_PER_IP = 8, 30
log = logging.getLogger("campaignify")

app = Flask(__name__, template_folder=os.path.join(HERE, "web"), static_folder=os.path.join(HERE, "static"))
app.secret_key = os.environ.get("RS_SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=(MAX_VIDEO_MB + 5) * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("RS_COOKIE_SECURE", "0") == "1")
# Behind Crowdcut's Caddy: trust its one hop for the client IP and https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init()
worker.start()


# ---------------------------------------------------------------- helpers

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


app.jinja_env.globals.update(csrf_token=csrf_token, tools=catalog.TOOLS, packs=catalog.PACKS,
                             starter_credits=catalog.starter_credits, rates=catalog.rates,
                             billing_configured=billing.configured, starter_image_ads=catalog.STARTER_IMAGE_ADS)

CSRF_EXEMPT = {"static", "stripe_webhook"}


@app.before_request
def load_user():
    g.user = g.ws = None
    uid = session.get("uid")
    if uid:
        with db.connect() as conn:
            u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if u:
                g.user = u
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        sent = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "This form expired. Go back, refresh and try again.")


def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrap


def admin_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrap


def brand():
    w = g.ws
    return {"name": w["brand_name"] or w["name"], "product": w["product_name"] or "",
            "claims": w["claims"] or "", "audience": w["audience"] or ""}


def _save_upload(fs, folder, allowed, max_mb):
    ctype = fs.mimetype or mimetypes.guess_type(fs.filename or "")[0] or ""
    if ctype not in allowed:
        raise ValueError("That file type isn't supported.")
    os.makedirs(folder, exist_ok=True)
    ext = mimetypes.guess_extension(ctype) or os.path.splitext(fs.filename or "")[1] or ""
    path = os.path.join(folder, f"{uuid.uuid4().hex}{ext}")
    fs.save(path)
    if os.path.getsize(path) > max_mb * 1024 * 1024:
        os.remove(path)
        raise ValueError(f"Files up to {max_mb} MB.")
    return path, ctype


def _video_seconds(path):
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30).stdout
        return round(float(out.strip()), 1)
    except (ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- auth

def client_ip() -> str:
    return request.remote_addr or "unknown"


# Real ads from our own brand for the landing page. Only add work we actually
# made; never mock-ups presented as customer results.
GALLERY = [
    {"kind": "video", "src": "previews/swap.mp4", "poster": "previews/swap.jpg", "title": "The swap", "tool": "Video"},
    {"kind": "image", "src": "gallery/tmu-presenter-640.jpg", "title": "Presenter still", "tool": "Image",
     "alt": "A woman in a white shirt holds a coffee pouch at a marble kitchen counter."},
    {"kind": "video", "src": "previews/two-pm.mp4", "poster": "previews/two-pm.jpg", "title": "The 2 PM moment", "tool": "Video"},
    {"kind": "video", "src": "previews/label.mp4", "poster": "previews/label.jpg", "title": "Label read", "tool": "Video"},
    {"kind": "image", "src": "gallery/tmu-kitchen-640.jpg", "title": "Lifestyle still", "tool": "Image",
     "alt": "A woman in a knit sweater pours hot water into a mug beside a coffee pouch."},
    {"kind": "video", "src": "previews/iced.mp4", "poster": "previews/iced.jpg", "title": "Iced", "tool": "Video"},
    {"kind": "video", "src": "previews/gym.mp4", "poster": "previews/gym.jpg", "title": "Gym bag", "tool": "Video"},
    {"kind": "image", "src": "gallery/tmu-product-720.jpg", "title": "Product composition", "tool": "Image", "square": True,
     "alt": "A dark coffee pouch surrounded by coffee beans, a wooden scoop, coconut and green tea leaves."},
    {"kind": "video", "src": "previews/how.mp4", "poster": "previews/how.jpg", "title": "How to make it", "tool": "Video"},
    {"kind": "video", "src": "previews/cost.mp4", "poster": "previews/cost.jpg", "title": "Cost per cup", "tool": "Video"},
]


@app.route("/")
def index():
    """Logged in: the studio. Logged out: the public landing page."""
    if g.user:
        return redirect(url_for("studio"))
    return render_template("landing.html", gallery=GALLERY, hero=[GALLERY[0], GALLERY[2], GALLERY[5]])


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/health")
def health():
    out = {"ok": True, "generation_configured": hf.configured()}
    if request.args.get("deep"):
        out["generation"] = hf.probe()
    return jsonify(out)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        ip = client_ip()
        if db.throttled(f"login:{ip}:{email}", LOGIN_FAILS_PER_EMAIL, 900) or \
                db.throttled(f"login:{ip}", LOGIN_FAILS_PER_IP, 900):
            flash("Too many tries. Wait 15 minutes and try again.")
            return render_template("login.html"), 429
        with db.connect() as conn:
            u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and check_password_hash(u["pw_hash"], request.form.get("password") or ""):
            session.clear()
            session["uid"] = u["id"]
            csrf_token()
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("studio"))
        db.hit(f"login:{ip}:{email}")
        db.hit(f"login:{ip}")
        flash("That email and password don't match.")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Open to anyone: a new account gets its own workspace and the starter
    credits, then the guided first ad. An invite link joins an existing
    workspace instead; owner setup links (owners.py) make a Rector & Stull admin
    and work only with their own email."""
    code = (request.form.get("invite") if request.method == "POST" else request.args.get("invite")) or ""
    code = code.strip()
    owner = owners.owner_email(code)

    def again(msg, status=200):
        flash(msg)
        return render_template("signup.html", invite=code, owner=owner,
                               email=request.form.get("email", ""), name=request.form.get("name", "")), status

    if g.user:
        return redirect(url_for("studio"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        name = (request.form.get("name") or "").strip()[:80]
        ip = client_ip()
        if request.form.get("website"):            # honeypot: people never see this field
            return again("Something went wrong. Please try again.", 400)
        if "@" not in email or "." not in email.split("@")[-1] or len(email) > 200 or len(pw) < 8:
            return again("Use a real email and a password of at least 8 characters.")
        if not owner and (db.throttled(f"signup:{ip}", SIGNUPS_PER_HOUR, 3600) or
                          db.throttled(f"signup:{ip}", SIGNUPS_PER_DAY, 86400)):
            return again("Too many new accounts from your network. Try again later.", 429)
        dest = "start"
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                return again("That email already has an account. Log in instead.")
            inv = None
            is_admin = 0
            if owner:
                if owner != email:
                    return again("This setup link is for a different email.")
                ws = conn.execute("SELECT id FROM workspaces WHERE name=? ORDER BY id LIMIT 1",
                                  ("Rector & Stull",)).fetchone()
                ws_id = ws["id"] if ws else conn.execute(
                    "INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                    ("Rector & Stull", 0, db.now())).lastrowid
                is_admin, dest = 1, "brand_kit"
            else:
                if code:
                    inv = conn.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL", (code,)).fetchone()
                    if not inv:
                        return again("That invite link has expired or was already used. Clear the invite code "
                                     "to create your own free account instead.")
                if inv and inv["workspace_id"]:
                    ws_id, dest = inv["workspace_id"], "studio"
                else:
                    ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                                         ((name or email.split("@")[0]) + "'s studio", 0, db.now())).lastrowid
                    if catalog.starter_credits():
                        db.adjust_credits(conn, ws_id, catalog.starter_credits(), "starter credits")
            cur = conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at) "
                               "VALUES (?,?,?,?,?,?)", (email, name, generate_password_hash(pw), is_admin, ws_id, db.now()))
            if inv:
                conn.execute("UPDATE invites SET used_by=? WHERE code=?", (cur.lastrowid, code))
            conn.commit()
            session.clear()
            session["uid"] = cur.lastrowid
            csrf_token()
        if not owner:
            db.hit(f"signup:{ip}")
        return redirect(url_for(dest))
    return render_template("signup.html", invite=code, owner=owner, email="", name="")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- studio

@app.route("/app")
@login_required
def studio():
    with db.connect() as conn:
        recent = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 6",
                              (g.ws["id"],)).fetchall()
        photos = conn.execute("SELECT COUNT(*) c FROM photos WHERE workspace_id=?", (g.ws["id"],)).fetchone()["c"]
        outs = _outputs(conn, [j["id"] for j in recent])
    return render_template("studio.html", templates=catalog.TEMPLATES, templates_by_id=catalog.TEMPLATE_BY_ID,
                           recent=recent, outs=outs, photos=photos, brand=brand(), configured=hf.configured(),
                           first_run=not recent)


def _outputs(conn, job_ids):
    if not job_ids:
        return {}
    outs = {}
    for o in conn.execute(f"SELECT * FROM outputs WHERE job_id IN ({','.join('?' * len(job_ids))}) ORDER BY id",
                          job_ids):
        outs.setdefault(o["job_id"], []).append(o)
    return outs


@app.route("/app/new")
@login_required
def new():
    tpl = catalog.TEMPLATE_BY_ID.get(request.args.get("template", ""))
    tool = tpl["tool"] if tpl else request.args.get("tool", "ad_image")
    if tool not in catalog.TOOLS:
        abort(404)
    b = brand()
    with db.connect() as conn:
        photos = conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],)).fetchall()
        refs = conn.execute("SELECT * FROM refs WHERE active=1 ORDER BY id DESC").fetchall()
    prices = {
        "ad_image": {n: catalog.price_credits("ad_image", batch=n) for n in (1, 2, 4)},
        "photo_to_video": {d: catalog.price_credits("photo_to_video", duration=d) for d in (5, 10)},
        "motion_copy": {r["id"]: catalog.price_credits("motion_copy", ref_seconds=r["seconds"] or 15) for r in refs},
    }
    return render_template("new.html", tpl=tpl, tool=tool, t=catalog.TOOLS[tool], photos=photos, refs=refs,
                           brief=catalog.fill(tpl["brief"], b) if tpl else "", brand=b, prices=prices)


@app.route("/app/generate", methods=["POST"])
@login_required
def generate():
    f = request.form
    tool = f.get("tool")
    if tool not in catalog.TOOLS:
        abort(400)
    tpl_id = f.get("template") or None
    brief = (f.get("brief") or "").strip()[:2000]
    photo_ids = [int(x) for x in f.getlist("photo") if x.isdigit()][: catalog.TOOLS[tool]["max_photos"]]
    params = {"photo_ids": photo_ids}
    check = compliance.check(brief, f.get("audience") or "", f.get("niche") or "")
    if not check["ok"]:
        for m in check["blocks"]:
            flash(m)
        return redirect(url_for("new", template=tpl_id) if tpl_id else url_for("new", tool=tool))
    with db.connect() as conn:
        owned = {r["id"] for r in conn.execute("SELECT id FROM photos WHERE workspace_id=?", (g.ws["id"],))}
        params["photo_ids"] = [p for p in photo_ids if p in owned]
        if tool == "ad_image":
            batch = max(1, min(4, int(f.get("batch") or 1)))
            params.update(batch=batch, aspect=f.get("aspect") if f.get("aspect") in catalog.TOOLS[tool]["aspects"] else "9:16",
                          audience=(f.get("audience") or "")[:200], niche=(f.get("niche") or "")[:100])
            usd, credits = catalog.cost_usd(tool, batch=batch), catalog.price_credits(tool, batch=batch)
        elif tool == "photo_to_video":
            dur = 10 if f.get("duration") == "10" else 5
            if not params["photo_ids"]:
                flash("Pick one product photo to animate.")
                return redirect(url_for("new", template=tpl_id) if tpl_id else url_for("new", tool=tool))
            params.update(duration=dur, audio=f.get("audio") == "1")
            usd, credits = catalog.cost_usd(tool, duration=dur), catalog.price_credits(tool, duration=dur)
        else:
            ref = conn.execute("SELECT * FROM refs WHERE id=? AND active=1", (f.get("ref") or 0,)).fetchone()
            if not ref:
                flash("Pick a reference video.")
                return redirect(url_for("new", template=tpl_id) if tpl_id else url_for("new", tool=tool))
            params.update(ref_id=ref["id"])
            secs = ref["seconds"] or 15
            usd, credits = catalog.cost_usd(tool, ref_seconds=secs), catalog.price_credits(tool, ref_seconds=secs)
        jid = _queue_job(conn, tool, tpl_id, brief, params, usd, credits)
        if not jid:
            return redirect(url_for("new", template=tpl_id) if tpl_id else url_for("new", tool=tool))
    for w in check["warnings"]:
        flash("Heads up: " + w)
    flash("Started. It shows up in your library in a minute or two.")
    return redirect(url_for("library", highlight=jid))


def _queue_job(conn, tool, tpl_id, brief, params, usd, credits):
    """Insert a pending job and take its credits in one transaction. None (and a
    message) when the workspace can't afford it."""
    now = db.now()
    cur = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, "
                       "status, credits, cost_usd, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (g.ws["id"], g.user["id"], tool, tpl_id, brief, json.dumps(params), "pending",
                        credits, round(usd, 4), now, now))
    jid = cur.lastrowid
    if not db.adjust_credits(conn, g.ws["id"], -credits, "generation", jid):
        conn.rollback()
        flash(f"That needs {credits:,} credits and you have {g.ws['credits']:,}. Buy a credit pack on the Billing page.")
        return None
    conn.commit()
    return jid


# ---------------------------------------------------------------- guided first ad

@app.route("/app/start", methods=["GET", "POST"])
@login_required
def start():
    """One flow for a new account: product name + one photo, pick a template,
    first ad. Fills the brand kit as a side effect."""
    tpls = catalog.first_run_templates()
    by_id = {t["id"]: t for t in tpls}
    with db.connect() as conn:
        photos = conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC LIMIT 12",
                              (g.ws["id"],)).fetchall()
    form = request.form if request.method == "POST" else {}
    price = catalog.price_credits("ad_image", batch=1)

    def page(status=200):
        return render_template("start.html", tpls=tpls, photos=photos, form=form, price=price,
                               brand=brand(), pick=form.get("template") or tpls[0]["id"]), status

    if request.method == "GET":
        return page()
    f = request.form
    product = (f.get("product_name") or "").strip()[:80]
    brand_name = (f.get("brand_name") or "").strip()[:80]
    audience = (f.get("audience") or "").strip()[:200]
    tpl = by_id.get(f.get("template") or "")
    if not product:
        flash("Add your product's name.")
        return page(400)
    if not tpl:
        flash("Pick a template.")
        return page(400)
    b = {"name": brand_name or g.ws["brand_name"] or "", "product": product}
    brief = (f.get("brief") or "").strip()[:2000] or catalog.fill(tpl["brief"], b)
    if "{product}" in brief or "{brand}" in brief:
        brief = catalog.fill(brief, b)
    check = compliance.check(brief, audience, product, brand_name)
    if not check["ok"]:
        for m in check["blocks"]:
            flash(m)
        return page(400)
    fs = request.files.get("photo")
    photo_id = None
    if fs and fs.filename:
        try:
            path, ctype = _save_upload(fs, os.path.join(UPLOADS, str(g.ws["id"])), PHOTO_TYPES, MAX_PHOTO_MB)
        except ValueError as e:
            flash(f"{fs.filename}: {e}")
            return page(400)
    else:
        path = None
        chosen = f.get("photo_id", type=int)
        if chosen and any(p["id"] == chosen for p in photos):
            photo_id = chosen
        else:
            flash("Add one photo of your product.")
            return page(400)
    with db.connect() as conn:
        if path:
            photo_id = conn.execute("INSERT INTO photos (workspace_id, path, filename, content_type, created_at) "
                                    "VALUES (?,?,?,?,?)", (g.ws["id"], path, (fs.filename or "")[:120], ctype,
                                                           db.now())).lastrowid
        conn.execute("UPDATE workspaces SET product_name=?, brand_name=COALESCE(NULLIF(?, ''), brand_name), "
                     "audience=COALESCE(NULLIF(audience, ''), NULLIF(?, '')) WHERE id=?",
                     (product, brand_name, audience, g.ws["id"]))
        conn.commit()
        params = {"photo_ids": [photo_id], "batch": 1, "aspect": tpl.get("aspect") or "9:16",
                  "audience": audience or tpl.get("audience") or "", "niche": ""}
        jid = _queue_job(conn, "ad_image", tpl["id"], brief, params, catalog.cost_usd("ad_image", batch=1), price)
    if not jid:
        return redirect(url_for("billing_page"))
    for w in check["warnings"]:
        flash("Heads up: " + w)
    flash("Your first ad is on its way. It shows up here in a minute or two, and your product is saved in the "
          "brand kit for next time.")
    return redirect(url_for("library", highlight=jid))


@app.route("/app/library")
@login_required
def library():
    with db.connect() as conn:
        jobs = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 200",
                            (g.ws["id"],)).fetchall()
        outs = _outputs(conn, [j["id"] for j in jobs])
    return render_template("library.html", jobs=jobs, outs=outs, templates=catalog.TEMPLATE_BY_ID,
                           highlight=request.args.get("highlight", type=int))


@app.route("/api/jobs")
@login_required
def api_jobs():
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.isdigit()][:50]
    if not ids:
        return jsonify({"jobs": []})
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id, status, error FROM jobs WHERE workspace_id=? AND id IN ({','.join('?' * len(ids))})",
                            (g.ws["id"], *ids)).fetchall()
    return jsonify({"jobs": [dict(r) for r in rows]})


@app.route("/media/<int:oid>")
@login_required
def media(oid):
    with db.connect() as conn:
        o = conn.execute("SELECT o.* FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE o.id=? AND j.workspace_id=?",
                         (oid, g.ws["id"])).fetchone()
    if not o:
        abort(404)
    if request.args.get("poster") == "1":
        p = (o["path"] or "") + ".jpg"
        if o["path"] and os.path.exists(p):
            return send_file(p, conditional=True, max_age=86400)
        abort(404)
    if o["path"] and os.path.exists(o["path"]):
        return send_file(o["path"], as_attachment=request.args.get("download") == "1",
                         download_name=os.path.basename(o["path"]), conditional=True)
    return redirect(o["url"])


@app.route("/photo/<int:pid>")
@login_required
def photo(pid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, g.ws["id"])).fetchone()
    if not p or not os.path.exists(p["path"]):
        abort(404)
    return send_file(p["path"], conditional=True)


# ---------------------------------------------------------------- brand kit

@app.route("/app/brand", methods=["GET", "POST"])
@login_required
def brand_kit():
    if request.method == "POST":
        f = request.form
        check = compliance.check(f.get("brand_name") or "", f.get("product_name") or "", f.get("claims") or "",
                                 f.get("audience") or "")
        if not check["ok"]:
            for m in check["blocks"]:
                flash("Not saved: " + m)
            return redirect(url_for("brand_kit"))
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET brand_name=?, product_name=?, claims=?, audience=? WHERE id=?",
                         ((f.get("brand_name") or "")[:80], (f.get("product_name") or "")[:80],
                          (f.get("claims") or "")[:2000], (f.get("audience") or "")[:300], g.ws["id"]))
            conn.commit()
        flash("Brand kit saved.")
        return redirect(url_for("brand_kit"))
    with db.connect() as conn:
        photos = conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],)).fetchall()
    return render_template("brand.html", ws=g.ws, photos=photos)


@app.route("/app/photos", methods=["POST"])
@login_required
def upload_photos():
    n = 0
    for fs in request.files.getlist("photos")[:20]:
        if not fs or not fs.filename:
            continue
        try:
            path, ctype = _save_upload(fs, os.path.join(UPLOADS, str(g.ws["id"])), PHOTO_TYPES, MAX_PHOTO_MB)
        except ValueError as e:
            flash(f"{fs.filename}: {e}")
            continue
        with db.connect() as conn:
            conn.execute("INSERT INTO photos (workspace_id, path, filename, content_type, created_at) VALUES (?,?,?,?,?)",
                         (g.ws["id"], path, (fs.filename or "")[:120], ctype, db.now()))
            conn.commit()
        n += 1
    if n:
        flash(f"Added {n} photo{'s' if n != 1 else ''}.")
    return redirect(request.form.get("back") or url_for("brand_kit"))


@app.route("/app/photos/<int:pid>/delete", methods=["POST"])
@login_required
def delete_photo(pid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, g.ws["id"])).fetchone()
        if p:
            conn.execute("DELETE FROM photos WHERE id=?", (pid,))
            conn.commit()
            try:
                os.remove(p["path"])
            except OSError:
                pass
    return redirect(url_for("brand_kit"))


# ---------------------------------------------------------------- billing

@app.route("/app/billing")
@login_required
def billing_page():
    if request.args.get("checkout") == "success":
        flash("Payment received, thank you. Your credits land in a few seconds; refresh if the balance hasn't moved.")
    elif request.args.get("checkout") == "cancel":
        flash("Checkout canceled. Nothing was charged.")
    with db.connect() as conn:
        purchases = conn.execute("SELECT * FROM purchases WHERE workspace_id=? AND status IN ('paid','open') "
                                 "ORDER BY id DESC LIMIT 50", (g.ws["id"],)).fetchall()
        ledger = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT 30",
                              (g.ws["id"],)).fetchall()
    return render_template("billing.html", purchases=purchases, ledger=ledger, packs_by=catalog.PACK_BY_CODE)


@app.route("/app/billing/checkout", methods=["POST"])
@login_required
def checkout():
    pack = catalog.PACK_BY_CODE.get(request.form.get("pack") or "")
    if not pack:
        abort(400)
    if not billing.configured():
        flash("Checkout is coming soon. Your free credits work in the meantime.")
        return redirect(url_for("billing_page"))
    with db.connect() as conn:
        pid = conn.execute("INSERT INTO purchases (workspace_id, user_id, pack, credits, amount_cents, currency, "
                           "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (g.ws["id"], g.user["id"], pack["code"], pack["credits"], pack["usd"] * 100, "usd",
                            "open", db.now())).lastrowid
        conn.commit()
    base = request.host_url.rstrip("/")
    try:
        sess = billing.create_checkout_session(
            price_id=pack["price_id"],
            success_url=f"{base}{url_for('billing_page')}?checkout=success",
            cancel_url=f"{base}{url_for('billing_page')}?checkout=cancel",
            email=g.user["email"],
            client_reference_id=f"cfy-{pid}",
            description=f"Campaignify {pack['name']} pack ({pack['credits']:,} credits)",
            metadata={"app": billing.APP_TAG, "purchase_id": pid, "workspace_id": g.ws["id"],
                      "pack": pack["code"], "credits": pack["credits"]},
            idempotency_key=f"campaignify-purchase-{pid}")
    except billing.BillingError as e:
        log.warning("checkout failed for purchase %s: %s", pid, e)
        with db.connect() as conn:
            conn.execute("UPDATE purchases SET status='failed' WHERE id=?", (pid,))
            conn.commit()
        flash("Checkout couldn't start. Try again in a minute.")
        return redirect(url_for("billing_page"))
    with db.connect() as conn:
        conn.execute("UPDATE purchases SET stripe_session_id=? WHERE id=?", (sess["id"], pid))
        conn.commit()
    return redirect(sess["url"], code=303)


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """checkout.session.completed -> add the pack's credits, once. The Stripe
    account is shared, so anything that isn't one of our purchases is ignored."""
    payload = request.get_data()
    try:
        event = billing.verify_event(payload, request.headers.get("Stripe-Signature", ""))
    except billing.SignatureError as e:
        log.warning("stripe webhook rejected: %s", e)
        return jsonify({"error": "bad signature"}), 400
    if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return jsonify({"ignored": event.get("type")})
    sess = (event.get("data") or {}).get("object") or {}
    meta = sess.get("metadata") or {}
    if meta.get("app") != billing.APP_TAG or not str(meta.get("purchase_id", "")).isdigit():
        return jsonify({"ignored": "not a campaignify purchase"})
    if sess.get("payment_status") not in ("paid", "no_payment_required"):
        return jsonify({"pending": "payment not settled"})
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM purchases WHERE id=?", (int(meta["purchase_id"]),)).fetchone()
        if not p:
            log.error("stripe webhook: purchase %s not found (session %s)", meta["purchase_id"], sess.get("id"))
            return jsonify({"ignored": "unknown purchase"})
        if p["status"] == "paid":
            return jsonify({"ok": True, "duplicate": True})
        if (str(p["workspace_id"]) != str(meta.get("workspace_id")) or p["pack"] != meta.get("pack")
                or sess.get("amount_total") != p["amount_cents"] or (sess.get("currency") or "").lower() != p["currency"]):
            log.error("stripe webhook: session %s does not match purchase %s", sess.get("id"), p["id"])
            return jsonify({"error": "mismatch"}), 400
        cur = conn.execute("UPDATE purchases SET status='paid', paid_at=?, stripe_session_id=?, stripe_payment_intent=? "
                           "WHERE id=? AND status!='paid'",
                           (db.now(), sess.get("id"), sess.get("payment_intent"), p["id"]))
        if cur.rowcount != 1:
            conn.rollback()
            return jsonify({"ok": True, "duplicate": True})
        db.adjust_credits(conn, p["workspace_id"], p["credits"],
                          f"{catalog.PACK_BY_CODE.get(p['pack'], {}).get('name', p['pack'])} pack")
        conn.commit()
    return jsonify({"ok": True, "credited": p["credits"]})


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin():
    with db.connect() as conn:
        wss = conn.execute("SELECT w.*, (SELECT COUNT(*) FROM users u WHERE u.workspace_id=w.id) members, "
                           "(SELECT COUNT(*) FROM jobs j WHERE j.workspace_id=w.id) jobs, "
                           "(SELECT COALESCE(SUM(cost_usd),0) FROM jobs j WHERE j.workspace_id=w.id AND j.status='done') cost, "
                           "(SELECT COALESCE(SUM(credits),0) FROM jobs j WHERE j.workspace_id=w.id AND j.status='done') spent "
                           "FROM workspaces w ORDER BY w.id").fetchall()
        invites = conn.execute("SELECT i.*, u.email used_email FROM invites i LEFT JOIN users u ON u.id=i.used_by "
                               "ORDER BY i.created_at DESC LIMIT 50").fetchall()
        refs = conn.execute("SELECT * FROM refs ORDER BY id DESC").fetchall()
        jobs = conn.execute("SELECT j.*, w.name ws_name FROM jobs j JOIN workspaces w ON w.id=j.workspace_id "
                            "ORDER BY j.id DESC LIMIT 30").fetchall()
        purchases = conn.execute("SELECT p.*, w.name ws_name FROM purchases p JOIN workspaces w ON w.id=p.workspace_id "
                                 "WHERE p.status='paid' ORDER BY p.id DESC LIMIT 30").fetchall()
    return render_template("admin.html", wss=wss, invites=invites, refs=refs, jobs=jobs, purchases=purchases,
                           billing_on=billing.configured(),
                           markup=catalog.MARKUP, configured=hf.configured(), base=request.host_url)


@app.route("/admin/invites", methods=["POST"])
@admin_required
def admin_invite():
    code = secrets.token_urlsafe(9)
    ws = request.form.get("workspace_id")
    with db.connect() as conn:
        conn.execute("INSERT INTO invites (code, workspace_id, note, created_at) VALUES (?,?,?,?)",
                     (code, int(ws) if ws and ws.isdigit() else None, (request.form.get("note") or "")[:120], db.now()))
        conn.commit()
    flash(f"Invite link: {request.host_url}signup?invite={code}")
    return redirect(url_for("admin"))


@app.route("/admin/credits", methods=["POST"])
@admin_required
def admin_credits():
    try:
        ws_id, amount = int(request.form["workspace_id"]), int(request.form["amount"])
    except (KeyError, ValueError):
        abort(400)
    with db.connect() as conn:
        if not db.adjust_credits(conn, ws_id, amount, (request.form.get("reason") or "admin top-up")[:100]):
            flash("That would take the balance below zero.")
        conn.commit()
    return redirect(url_for("admin"))


@app.route("/admin/refs", methods=["POST"])
@admin_required
def admin_refs():
    fs = request.files.get("video")
    title = (request.form.get("title") or "").strip()[:100]
    if not fs or not title:
        flash("Add a title and a video.")
        return redirect(url_for("admin"))
    try:
        path, ctype = _save_upload(fs, os.path.join(UPLOADS, "refs"), VIDEO_TYPES, MAX_VIDEO_MB)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("admin"))
    with db.connect() as conn:
        conn.execute("INSERT INTO refs (title, path, content_type, seconds, created_at) VALUES (?,?,?,?,?)",
                     (title, path, ctype, _video_seconds(path), db.now()))
        conn.commit()
    flash("Reference added.")
    return redirect(url_for("admin"))


@app.route("/admin/refs/<int:rid>/toggle", methods=["POST"])
@admin_required
def admin_ref_toggle(rid):
    with db.connect() as conn:
        conn.execute("UPDATE refs SET active = 1 - active WHERE id=?", (rid,))
        conn.commit()
    return redirect(url_for("admin"))


@app.route("/ref/<int:rid>")
@login_required
def ref_video(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
    if not r or not os.path.exists(r["path"]):
        abort(404)
    return send_file(r["path"], conditional=True)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
