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
import plans
import worker
from core import admin_required, brand, feature_required, is_owner, login_required, owner_required  # noqa: F401
from core import queue_job as _queue_job  # noqa: E402

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


def _has(feature):
    return bool(g.get("ws")) and plans.has(g.ws, feature)


def _balance():
    return db.balance(g.ws) if g.get("ws") else 0


import llm  # noqa: E402
import platforms as platforms_mod  # noqa: E402

app.jinja_env.globals.update(csrf_token=csrf_token, tools=catalog.TOOLS, packs=catalog.PACKS, llm_on=llm.configured,
                             PLATFORMS=platforms_mod.PLATFORMS, PLATFORM_ORDER=platforms_mod.ORDER,
                             COPY_KINDS=platforms_mod.KINDS,
                             starter_credits=catalog.starter_credits, rates=catalog.rates,
                             billing_configured=billing.configured, starter_image_ads=catalog.STARTER_IMAGE_ADS,
                             plans=plans, has_feature=_has, balance=_balance)

# Sidebar: (endpoint, label, icon, feature or None, endpoints that light it up).
NAV = [
    ("studio", "Create", "create", None, ("studio", "new", "start")),
    ("campaigns.index", "Campaigns", "planner", "planner", ("campaigns.index", "campaigns.show")),
    ("copy.studio", "Copy", "copy", "copy", ("copy.studio", "copy.run", "copy.optimize", "copy.optimized")),
    ("seo.index", "SEO & AEO", "seo", "seo", ("seo.index", "seo.result")),
    ("pages.index", "Pages", "pages", "pages", ("pages.index", "pages.edit", "pages.links", "pages.link_detail",
                                                          "forms.index", "forms.edit", "forms.submissions")),
    ("forms.contacts", "Contacts", "contacts", "forms", ("forms.contacts", "forms.contact")),
    ("community.admin", "Community", "community", "community", ("community.admin", "community.course_edit",
                                                                 "community.lesson_edit")),
    ("library", "Library", "library", None, ("library",)),
    ("adhub.index", "Ad accounts", "adhub", "adhub", ("adhub.index", "adhub.platform", "adhub.export_page")),
    ("brand_kit", "Brand kit", "brand", None, ("brand_kit",)),
    ("team", "Team", "team", None, ("team",)),
    ("billing_page", "Billing", "billing", None, ("billing_page",)),
]


app.jinja_env.filters["fromjson"] = lambda s: json.loads(s or "{}")


# platform -> setup guide link (the planner's launch checklist links to these).
ADHUB_LINKS = {}


@app.before_request
def _adhub_links():
    if not ADHUB_LINKS:
        for k in ("meta", "tiktok", "youtube", "x", "linkedin", "pinterest", "snapchat", "reddit"):
            ADHUB_LINKS[k] = url_for("adhub.platform", platform=k)
        ADHUB_LINKS["google"] = url_for("adhub.platform", platform="youtube")


@app.context_processor
def _nav():
    return {"nav_items": NAV, "adhub_links": ADHUB_LINKS}


CSRF_EXEMPT = {"static", "stripe_webhook", "forms.public"}  # public forms carry their own signed token


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
                if g.ws and not g.ws["brand_id"]:
                    _ensure_brand(conn, g.ws["id"])
                    g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        sent = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "This form expired. Go back, refresh and try again.")


BRAND_COLS = ("brand_name", "product_name", "claims", "audience")


def _ensure_brand(conn, ws_id):
    """Every workspace has at least one brand row. The workspace's own brand
    columns always mirror the active brand, so the studio reads them as before."""
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if ws["brand_id"] and conn.execute("SELECT 1 FROM brands WHERE id=? AND workspace_id=?",
                                       (ws["brand_id"], ws_id)).fetchone():
        return ws["brand_id"]
    bid = conn.execute("INSERT INTO brands (workspace_id, brand_name, product_name, claims, audience, created_at) "
                       "VALUES (?,?,?,?,?,?)", (ws_id, *[ws[c] for c in BRAND_COLS], db.now())).lastrowid
    conn.execute("UPDATE workspaces SET brand_id=? WHERE id=?", (bid, ws_id))
    conn.commit()
    return bid


def _sync_brand_row(conn, ws_id):
    """Copy the workspace's (active) brand columns into its brand row."""
    bid = _ensure_brand(conn, ws_id)
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    conn.execute("UPDATE brands SET brand_name=?, product_name=?, claims=?, audience=? WHERE id=?",
                 (*[ws[c] for c in BRAND_COLS], bid))
    conn.commit()


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
    root = request.host_url
    site = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "@id": root + "#org", "name": "Campaignify", "url": root, "email": "hello@campaignify.ai",
         "logo": url_for("static", filename="img/campaignify-icon-512.png", _external=True)},
        {"@type": "WebSite", "name": "Campaignify", "url": root, "publisher": {"@id": root + "#org"}},
        {"@type": "SoftwareApplication", "name": "Campaignify", "applicationCategory": "BusinessApplication",
         "operatingSystem": "Web", "url": root,
         "offers": [{"@type": "Offer", "name": f"{p['name']} plan", "price": p["usd"], "priceCurrency": "USD"}
                    for p in plans.PAID]}]}
    return render_template("landing.html", gallery=GALLERY, hero=[GALLERY[0], GALLERY[2], GALLERY[5]],
                           site_jsonld=json.dumps(site).replace("</", "<\\/"))


# Extra lines for the public features page, by feature key (only shipped ones show).
FEATURE_DETAILS = {
    "studio": ["Templates for proven angles: product hero, daily routine, label read and more",
               "Image ads in four shapes, 5 or 10 second clips, motion copied from a reference video",
               "A library of everything you've made, with full-quality downloads",
               "The price in credits shown before anything runs; failures refunded automatically"],
    "compliance": ["Income, earnings and recruiting claims blocked on every account",
                   "Weight-loss, disease and before-and-after language blocked",
                   "Discount and free-shipping promises blocked; promise words flagged"],
    "brand_kit": ["Approved claims copied from your label", "Product photos reused across every ad",
                  "More than one brand on Growth and Scale"],
    "priority": ["Scale workspaces' generations start before the rest of the queue"],
    "planner": ["Give it the offer, audience, goal, budget and platforms",
                "It proposes angles, writes hooks and scripts, and lines up ads from Campaignify's own templates",
                "Writes the copy for each platform, a budget split, what to watch and a launch checklist",
                "You watch every step; ads are only generated when you confirm them, at the price shown",
                "Planning costs what the AI actually used (capped, shown before you start)"],
    "adhub": ["Facebook & Instagram, TikTok, YouTube & Google, X, LinkedIn, Pinterest, Snapchat and Reddit",
              "Step-by-step setup with links to each platform's own guides, ticked off as a team",
              "Pixel and tag basics for each platform",
              "Export any finished ad at each platform's sizes (4:5, 9:16, 1:1, 2:3, 16:9, 1.91:1)"],
    "pages": ["Blocks: hero, your ads from the library, text, benefits, FAQ, call to action, embeds",
              "Published at campaignify.ai/p/your-page, light or dark, your button color",
              "Optional AI draft of the page copy; every page checked by the compliance rules before it goes live",
              "Starter includes one published page; Growth and Scale, unlimited"],
    "links": ["campaignify.ai/l/… short links with your UTM tags added",
              "Passes through the UTM tags and click ids already on the incoming link",
              "Click counts, without storing visitors' IP addresses"],
    "link_analytics": ["Clicks per day for 30 days", "Referrers, countries, devices and UTM breakdowns"],
    "community": ["Spaces with posts, comments and pinned posts; members post and reply",
                  "Courses: modules and lessons with text and video (from your library, YouTube, Vimeo or Loom), "
                  "with progress saved per member",
                  "Members with their own logins (they don't use your team's seats), invite links, an optional join "
                  "link, and admin roles",
                  "Events with a time zone, a join link and an add-to-calendar file",
                  "Published at campaignify.ai/c/your-community"],
    "forms": ["Short text, email, phone, long text, dropdown and checkbox fields, with show-if logic",
              "On your pages as a block, or shared as a hosted form link",
              "A contacts list with tags, notes, search and CSV export",
              "Engagement per contact: which page, smart link and form they came through",
              "Signed webhooks to your own tools; spam guards built in"],
    "copy": ["Ad copy with every text field each platform takes, sized to its display lengths",
             "Hooks with on-screen text, captions with hashtags, headlines, emails and landing-page copy",
             "Facebook & Instagram, TikTok, YouTube, Google Search, X, LinkedIn, Pinterest, Snapchat and Reddit",
             "Every variant run through the compliance checks; you pay the AI's actual cost in credits"],
    "seo": ["Audit a live page or a pasted draft: title, description, headings, canonical, mobile, images, links",
            "Answer-engine checks: answer-first summary, question headings, FAQ and content-type structured data",
            "AI-assistant checks: who is behind the page, sources, specific facts, AI crawlers in robots.txt, llms.txt",
            "AI suggestions: better title and description, an FAQ block, keyword and topic ideas",
            "Ready-to-paste schema.org JSON-LD and an llms.txt draft"],
    "optimizer": ["Free quick checks: hook, clarity, call to action, platform fit, hashtags and compliance",
                  "An AI review that explains the biggest fix and writes three rewrites"],
}


@app.route("/features")
def features():
    return render_template("features.html", feature_details=FEATURE_DETAILS)


PUBLIC_PAGES = ["index", "features", "privacy", "terms"]


@app.route("/robots.txt")
def robots_txt():
    body = ("User-agent: *\nDisallow: /app\nDisallow: /admin\nDisallow: /api\nDisallow: /media\nDisallow: /photo\n"
            "Disallow: /ref\nDisallow: /webhooks\nAllow: /\n\n"
            f"Sitemap: {request.host_url}sitemap.xml\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    urls = "".join(f"<url><loc>{request.host_url.rstrip('/')}{url_for(p)}</loc></url>" for p in PUBLIC_PAGES)
    return app.response_class('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                              f'sitemap/0.9">{urls}</urlset>', mimetype="application/xml")


@app.route("/llms.txt")
def llms_txt():
    return app.response_class(render_template("llms.txt", feature_details=FEATURE_DETAILS),
                              mimetype="text/plain")


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
                    target = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
                    maker = conn.execute("SELECT is_admin FROM users WHERE id=?", (inv["created_by"],)).fetchone() \
                        if inv["created_by"] else None
                    members = conn.execute("SELECT COUNT(*) FROM users WHERE workspace_id=?", (ws_id,)).fetchone()[0]
                    # Team invites count against the plan's seats; operator (site admin) invites don't.
                    if inv["created_by"] and not (maker and maker["is_admin"]) and \
                            not plans.within(target, "seats", members):
                        return again("That team is full on its current plan. Ask the owner to upgrade "
                                     "or free a seat.")
                else:
                    ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                                         ((name or email.split("@")[0]) + "'s studio", 0, db.now())).lastrowid
                    if catalog.starter_credits():
                        db.adjust_credits(conn, ws_id, catalog.starter_credits(), "starter credits")
            joins = bool(inv and inv["workspace_id"]) or (owner and conn.execute(
                "SELECT 1 FROM users WHERE workspace_id=?", (ws_id,)).fetchone() is not None)
            cur = conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at, role) "
                               "VALUES (?,?,?,?,?,?,?)", (email, name, generate_password_hash(pw), is_admin, ws_id,
                                                          db.now(), "member" if joins else "owner"))
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
        _sync_brand_row(conn, g.ws["id"])
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
            _sync_brand_row(conn, g.ws["id"])
        flash("Brand kit saved.")
        return redirect(url_for("brand_kit"))
    with db.connect() as conn:
        photos = conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC", (g.ws["id"],)).fetchall()
        brands = conn.execute("SELECT * FROM brands WHERE workspace_id=? ORDER BY id", (g.ws["id"],)).fetchall()
    nxt = plans.next_plan_for("brands", len(brands))
    return render_template("brand.html", ws=g.ws, photos=photos, brands=brands,
                           can_add=plans.within(g.ws, "brands", len(brands)), next_plan=nxt)


@app.route("/app/brands", methods=["POST"])
@login_required
def add_brand():
    """A new, empty brand that becomes the active one (plan limit enforced here)."""
    name = (request.form.get("brand_name") or "").strip()[:80]
    if not name:
        flash("Give the new brand a name.")
        return redirect(url_for("brand_kit"))
    check = compliance.check(name)
    if not check["ok"]:
        flash("Not saved: " + check["blocks"][0])
        return redirect(url_for("brand_kit"))
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM brands WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0]
        if not plans.within(g.ws, "brands", n):
            nxt = plans.next_plan_for("brands", n)
            flash(f"Your plan includes {plans.limit(g.ws, 'brands')} brand{'s' if plans.limit(g.ws, 'brands') != 1 else ''}."
                  + (f" {nxt['name']} includes more." if nxt else ""))
            return redirect(url_for("brand_kit"))
        bid = conn.execute("INSERT INTO brands (workspace_id, brand_name, created_at) VALUES (?,?,?)",
                           (g.ws["id"], name, db.now())).lastrowid
        _activate_brand(conn, bid)
    flash(f"Added {name}. It's your active brand now.")
    return redirect(url_for("brand_kit"))


def _activate_brand(conn, bid):
    b = conn.execute("SELECT * FROM brands WHERE id=? AND workspace_id=?", (bid, g.ws["id"])).fetchone()
    if not b:
        abort(404)
    conn.execute("UPDATE workspaces SET brand_id=?, brand_name=?, product_name=?, claims=?, audience=? WHERE id=?",
                 (b["id"], *[b[c] for c in BRAND_COLS], g.ws["id"]))
    conn.commit()


@app.route("/app/brands/<int:bid>/use", methods=["POST"])
@login_required
def use_brand(bid):
    with db.connect() as conn:
        _activate_brand(conn, bid)
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/app")
                    else url_for("brand_kit"))


@app.route("/app/brands/<int:bid>/delete", methods=["POST"])
@owner_required
def delete_brand(bid):
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM brands WHERE workspace_id=? ORDER BY id", (g.ws["id"],)).fetchall()
        ids = [r["id"] for r in rows]
        if bid not in ids:
            abort(404)
        if len(ids) == 1:
            flash("That's your only brand. Edit it instead.")
            return redirect(url_for("brand_kit"))
        conn.execute("DELETE FROM brands WHERE id=?", (bid,))
        conn.commit()
        if g.ws["brand_id"] == bid:
            _activate_brand(conn, next(i for i in ids if i != bid))
    flash("Brand removed.")
    return redirect(url_for("brand_kit"))


# ---------------------------------------------------------------- team (seats)

@app.route("/app/team")
@login_required
def team():
    with db.connect() as conn:
        members = conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY id", (g.ws["id"],)).fetchall()
        invites = conn.execute("SELECT * FROM invites WHERE workspace_id=? AND used_by IS NULL AND created_by IS NOT NULL "
                               "ORDER BY created_at DESC", (g.ws["id"],)).fetchall()
    seats = plans.limit(g.ws, "seats")
    used = len(members) + len(invites)
    return render_template("team.html", members=members, invites=invites, seats=seats, used=used,
                           can_invite=is_owner() and plans.within(g.ws, "seats", used),
                           next_plan=plans.next_plan_for("seats", used), owner=is_owner(), base=request.host_url)


@app.route("/app/team/invite", methods=["POST"])
@owner_required
def team_invite():
    with db.connect() as conn:
        members = conn.execute("SELECT COUNT(*) FROM users WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0]
        open_inv = conn.execute("SELECT COUNT(*) FROM invites WHERE workspace_id=? AND used_by IS NULL "
                                "AND created_by IS NOT NULL", (g.ws["id"],)).fetchone()[0]
        if not plans.within(g.ws, "seats", members + open_inv):
            flash("Every seat on your plan is taken. Upgrade for more seats, or revoke an open invite.")
            return redirect(url_for("team"))
        code = secrets.token_urlsafe(12)
        conn.execute("INSERT INTO invites (code, workspace_id, note, created_by, created_at) VALUES (?,?,?,?,?)",
                     (code, g.ws["id"], (request.form.get("note") or "")[:120], g.user["id"], db.now()))
        conn.commit()
    flash(f"Invite link (works once): {request.host_url}signup?invite={code}")
    return redirect(url_for("team"))


@app.route("/app/team/invites/<code>/revoke", methods=["POST"])
@owner_required
def team_invite_revoke(code):
    with db.connect() as conn:
        conn.execute("DELETE FROM invites WHERE code=? AND workspace_id=? AND used_by IS NULL", (code, g.ws["id"]))
        conn.commit()
    return redirect(url_for("team"))


@app.route("/app/team/<int:uid>/remove", methods=["POST"])
@owner_required
def team_remove(uid):
    if uid == g.user["id"]:
        flash("You can't remove yourself.")
        return redirect(url_for("team"))
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE id=? AND workspace_id=?", (uid, g.ws["id"])).fetchone()
        if not u:
            abort(404)
        if u["role"] == "owner" or u["is_admin"]:
            abort(403, "Owners can't be removed here.")
        # Their account moves to a fresh, empty workspace of their own.
        ws_id = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                             ((u["name"] or u["email"].split("@")[0]) + "'s studio", 0, db.now())).lastrowid
        conn.execute("UPDATE users SET workspace_id=?, role='owner' WHERE id=?", (ws_id, uid))
        conn.commit()
    flash(f"Removed {u['email']} from the team.")
    return redirect(url_for("team"))


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
    elif request.args.get("plan") == "success":
        flash("You're subscribed, thank you. Your plan and its monthly credits switch on in a few seconds; "
              "refresh if they haven't yet.")
    with db.connect() as conn:
        purchases = conn.execute("SELECT * FROM purchases WHERE workspace_id=? AND status IN ('paid','open') "
                                 "ORDER BY id DESC LIMIT 50", (g.ws["id"],)).fetchall()
        ledger = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT 30",
                              (g.ws["id"],)).fetchall()
        invoices = conn.execute("SELECT * FROM plan_invoices WHERE workspace_id=? ORDER BY created_at DESC LIMIT 24",
                                (g.ws["id"],)).fetchall()
    return render_template("billing.html", purchases=purchases, ledger=ledger, packs_by=catalog.PACK_BY_CODE,
                           invoices=invoices, current=plans.plan(g.ws), subscribed=_subscribed(g.ws),
                           owner=is_owner())


def _subscribed(ws) -> bool:
    """A live Stripe subscription (not just a comp plan)."""
    return bool(ws["stripe_subscription_id"]) and ws["plan_status"] in plans.LIVE_STATUSES


@app.route("/app/billing/subscribe", methods=["POST"])
@owner_required
def subscribe():
    """Start a plan checkout (mode=subscription). Already subscribed: changing
    plan happens in the Stripe portal, so we send you there instead of opening
    a second subscription."""
    p = plans.PLANS.get(request.form.get("plan") or "")
    if not p or not p["price_id"]:
        abort(400)
    if not billing.configured():
        flash("Plans open soon. Your credits and credit packs work in the meantime.")
        return redirect(url_for("billing_page"))
    if _subscribed(g.ws):
        return _portal_redirect("Change your plan here. Upgrades apply right away; downgrades at your next renewal.")
    base = request.host_url.rstrip("/")
    try:
        sess = billing.create_subscription_session(
            price_id=p["price_id"],
            success_url=f"{base}{url_for('billing_page')}?plan=success",
            cancel_url=f"{base}{url_for('billing_page')}?checkout=cancel",
            email=g.user["email"], customer=g.ws["stripe_customer_id"],
            client_reference_id=f"cfy-ws-{g.ws['id']}",
            metadata={"app": billing.APP_TAG, "kind": "plan", "workspace_id": g.ws["id"], "plan": p["code"],
                      "user_id": g.user["id"]},
            idempotency_key=f"campaignify-plan-{g.ws['id']}-{p['code']}-{uuid.uuid4().hex[:12]}")
    except billing.BillingError as e:
        log.warning("plan checkout failed for workspace %s: %s", g.ws["id"], e)
        flash("Checkout couldn't start. Try again in a minute.")
        return redirect(url_for("billing_page"))
    return redirect(sess["url"], code=303)


@app.route("/app/billing/portal", methods=["POST"])
@owner_required
def billing_portal():
    return _portal_redirect()


def _portal_redirect(note=None):
    if not g.ws["stripe_customer_id"]:
        flash("There's no subscription to manage yet. Pick a plan below.")
        return redirect(url_for("billing_page"))
    try:
        url = billing.create_portal_session(customer=g.ws["stripe_customer_id"],
                                            return_url=request.host_url.rstrip("/") + url_for("billing_page"))
    except billing.BillingError as e:
        log.warning("portal session failed for workspace %s: %s", g.ws["id"], e)
        flash("Plan management isn't available right now. Email hello@campaignify.ai and we'll change or cancel "
              "your plan for you.")
        return redirect(url_for("billing_page"))
    if note:
        flash(note)
    return redirect(url, code=303)


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
    """Credit packs (checkout.session.completed, mode=payment) and plans
    (checkout.session.completed mode=subscription, invoice.paid,
    customer.subscription.updated/deleted). The Stripe account is shared, so
    anything that isn't ours is ignored."""
    payload = request.get_data()
    try:
        event = billing.verify_event(payload, request.headers.get("Stripe-Signature", ""))
    except billing.SignatureError as e:
        log.warning("stripe webhook rejected: %s", e)
        return jsonify({"error": "bad signature"}), 400
    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if typ in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("mode") == "subscription":
            return _plan_checkout_completed(obj)
        return _pack_paid(obj)
    if typ == "invoice.paid":
        return _plan_invoice_paid(obj)
    if typ in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
        return _plan_subscription_changed(obj, deleted=typ == "customer.subscription.deleted")
    return jsonify({"ignored": typ})


def _pack_paid(sess):
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


def _our_workspace(conn, meta: dict, sub_id: str | None):
    """The workspace a plan event belongs to, or None when it isn't ours."""
    if meta.get("app") == billing.APP_TAG and str(meta.get("workspace_id", "")).isdigit():
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (int(meta["workspace_id"]),)).fetchone()
    if sub_id:
        return conn.execute("SELECT * FROM workspaces WHERE stripe_subscription_id=?", (sub_id,)).fetchone()
    return None


def _iso(ts):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return None


def _plan_checkout_completed(sess):
    meta = sess.get("metadata") or {}
    sub_id = sess.get("subscription") if isinstance(sess.get("subscription"), str) else \
        (sess.get("subscription") or {}).get("id")
    with db.connect() as conn:
        ws = _our_workspace(conn, meta, sub_id)
        if not ws or meta.get("app") != billing.APP_TAG:
            return jsonify({"ignored": "not a campaignify plan"})
        code = meta.get("plan") if meta.get("plan") in plans.PLANS else ws["plan"]
        customer = sess.get("customer") if isinstance(sess.get("customer"), str) else (sess.get("customer") or {}).get("id")
        conn.execute("UPDATE workspaces SET stripe_customer_id=COALESCE(?, stripe_customer_id), "
                     "stripe_subscription_id=COALESCE(?, stripe_subscription_id), plan=?, "
                     "plan_status=COALESCE(plan_status, 'active') WHERE id=?",
                     (customer, sub_id, code, ws["id"]))
        conn.commit()
    return jsonify({"ok": True, "workspace": ws["id"]})


def _plan_invoice_paid(inv):
    """Grant the plan's monthly credits: on the first invoice and each renewal
    the plan bucket is replaced (unused plan credits expire); an upgrade
    invoice tops it up by the difference. Once per invoice id."""
    sub_id, meta = billing.invoice_subscription(inv)
    lines = billing.invoice_lines(inv)
    ours = [plans.BY_PRICE[ln["price"]] for ln in lines if ln["price"] in plans.BY_PRICE]
    with db.connect() as conn:
        ws = _our_workspace(conn, meta, sub_id)
        if not ws or not ours:
            return jsonify({"ignored": "not a campaignify plan invoice"})
        if (inv.get("currency") or "usd").lower() != "usd" or inv.get("status") not in (None, "paid"):
            return jsonify({"ignored": "invoice not paid in usd"})
        priced = [plans.BY_PRICE[ln["price"]] for ln in lines if ln["price"] in plans.BY_PRICE and ln["amount"] > 0]
        p = max(priced or ours, key=lambda x: plans.rank(x["code"]))
        reason = inv.get("billing_reason") or "subscription_cycle"
        if conn.execute("SELECT 1 FROM plan_invoices WHERE invoice_id=?", (inv.get("id"),)).fetchone():
            return jsonify({"ok": True, "duplicate": True})
        if reason == "subscription_update":
            grant = max(0, p["credits"] - int(ws["plan_granted"] or 0))
            if grant:
                db.adjust_credits(conn, ws["id"], grant, f"{p['name']} plan: upgrade credits", bucket="plan")
            granted = max(int(ws["plan_granted"] or 0), p["credits"])
        else:
            grant = p["credits"]
            db.set_plan_credits(conn, ws["id"], grant, f"{p['name']} plan: monthly credits")
            granted = grant
        period_end = max([ln["period_end"] or 0 for ln in lines] or [0]) or None
        customer = inv.get("customer") if isinstance(inv.get("customer"), str) else (inv.get("customer") or {}).get("id")
        conn.execute("INSERT INTO plan_invoices (invoice_id, workspace_id, plan, credits, amount_cents, billing_reason, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (inv.get("id"), ws["id"], p["code"], grant, int(inv.get("amount_paid") or 0), reason, db.now()))
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active', plan_granted=?, "
                     "plan_period_end=COALESCE(?, plan_period_end), stripe_subscription_id=COALESCE(?, stripe_subscription_id), "
                     "stripe_customer_id=COALESCE(?, stripe_customer_id) WHERE id=?",
                     (p["code"], granted, _iso(period_end), sub_id, customer, ws["id"]))
        conn.commit()
    return jsonify({"ok": True, "credited": grant, "plan": p["code"]})


def _plan_subscription_changed(sub, deleted=False):
    meta = sub.get("metadata") or {}
    sub_id = sub.get("id")
    with db.connect() as conn:
        ws = _our_workspace(conn, meta, sub_id)
        if not ws:
            return jsonify({"ignored": "not a campaignify subscription"})
        if ws["stripe_subscription_id"] and sub_id and ws["stripe_subscription_id"] != sub_id \
                and ws["plan_status"] in plans.LIVE_STATUSES:
            # An older subscription of this workspace ending must not cancel the current one.
            return jsonify({"ignored": "not the workspace's current subscription"})
        status = "canceled" if deleted else (sub.get("status") or "active")
        if status == "incomplete":
            # First payment still in progress; invoice.paid switches the plan on.
            return jsonify({"ignored": "subscription incomplete"})
        if status not in plans.LIVE_STATUSES:
            db.set_plan_credits(conn, ws["id"], 0, "plan ended")
            conn.execute("UPDATE workspaces SET plan='free', plan_status=?, plan_granted=0, plan_cancel_at_period_end=0, "
                         "plan_period_end=NULL WHERE id=?", (status, ws["id"]))
            conn.commit()
            return jsonify({"ok": True, "plan": "free"})
        ours = [plans.BY_PRICE[pid] for pid in billing.subscription_prices(sub) if pid in plans.BY_PRICE]
        code = ours[0]["code"] if ours else ws["plan"]
        customer = sub.get("customer") if isinstance(sub.get("customer"), str) else (sub.get("customer") or {}).get("id")
        conn.execute("UPDATE workspaces SET plan=?, plan_status=?, plan_cancel_at_period_end=?, "
                     "plan_period_end=COALESCE(?, plan_period_end), stripe_subscription_id=?, "
                     "stripe_customer_id=COALESCE(?, stripe_customer_id) WHERE id=?",
                     (code, status, 1 if (sub.get("cancel_at_period_end") or sub.get("cancel_at")) else 0,
                      _iso(billing.subscription_period_end(sub)), sub_id, customer, ws["id"]))
        conn.commit()
    return jsonify({"ok": True, "plan": code, "status": status})


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
                           markup=catalog.MARKUP, configured=hf.configured(), base=request.host_url,
                           ai_status=llm.status())


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


@app.route("/admin/plan", methods=["POST"])
@admin_required
def admin_plan():
    """Comp a workspace onto a plan (no Stripe involved), or clear the comp."""
    code = request.form.get("plan") or ""
    try:
        ws_id = int(request.form["workspace_id"])
    except (KeyError, ValueError):
        abort(400)
    if code and code not in plans.PLANS:
        abort(400)
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (code if code and code != "free" else None, ws_id))
        conn.commit()
    flash("Plan updated.")
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


import campaign_routes  # noqa: E402
import copy_routes  # noqa: E402  (blueprints import core, never app)
import seo_routes  # noqa: E402
import adhub_routes  # noqa: E402
import pages_routes  # noqa: E402
import forms_routes  # noqa: E402
import community_routes  # noqa: E402

app.register_blueprint(copy_routes.bp)
app.register_blueprint(campaign_routes.bp)
app.register_blueprint(seo_routes.bp)
app.register_blueprint(adhub_routes.bp)
app.register_blueprint(pages_routes.bp)
app.register_blueprint(forms_routes.bp)
app.register_blueprint(community_routes.bp)
app.jinja_env.globals["has_endpoint"] = lambda name: name in app.view_functions


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
