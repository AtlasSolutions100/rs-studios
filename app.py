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
import threading
import time
import secrets
import shutil
import subprocess
import uuid

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import accounts
import adoffer
import admin_funnel
import alerts
import billing
import billing_holds
import catalog
import compcodes
import conversions
import owners
import pixels
import compliance
import creator
import db
import gads
import hf
import nudges
import opslog
import plans
import presenter_stills
import preview
import products
import public_site
import site_events
import storage
import verify
import worker
import core  # noqa: E402
from core import admin_required, brand, feature_required, is_owner, login_required, owner_required, picked_product  # noqa: F401
from core import queue_job as _queue_job  # noqa: E402
from core import verify_block as core_verify_block  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
from core import MAX_PHOTO_MB, MAX_VIDEO_MB, PHOTO_TYPES, UPLOADS, VIDEO_TYPES, save_upload as _save_upload  # noqa: E402
# Signup abuse limits (per client IP): new accounts per hour and per day.
SIGNUPS_PER_HOUR, SIGNUPS_PER_DAY = accounts.SIGNUPS_PER_HOUR, accounts.SIGNUPS_PER_DAY
# Failed logins allowed per IP+email and per IP in 15 minutes.
LOGIN_FAILS_PER_EMAIL, LOGIN_FAILS_PER_IP = 8, 30
log = logging.getLogger("campaignify")

app = Flask(__name__, template_folder=os.path.join(HERE, "web"), static_folder=os.path.join(HERE, "static"))
def _secret_key() -> str:
    """RS_SECRET_KEY, else one generated once and kept in the data folder. A
    fresh random key per start would log every member out and expire every
    open form ("This form expired") on each deploy."""
    key = (os.environ.get("RS_SECRET_KEY") or "").strip()
    if key:
        return key
    path = os.path.join(db.DATA, "secret_key")
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError:
        key = ""
    if len(key) < 32:
        key = secrets.token_hex(32)
        try:
            os.makedirs(db.DATA, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
        except OSError:
            log.warning("couldn't keep the session key in %s; sessions end on restart", path)
    return key


app.secret_key = _secret_key()
app.config.update(MAX_CONTENT_LENGTH=(MAX_VIDEO_MB + 5) * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("RS_COOKIE_SECURE", "0") == "1")
# Behind Crowdcut's Caddy: trust its one hop for the client IP and https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
opslog.install(app)   # JSON logs with request and workspace ids, X-Request-Id
alerts.install(app)   # operator alerts from webhook responses (signature failures, refunds, disputes)
storage.install(app)  # body limits per route (uploads 155 MB, webhooks 256 KB, else 1 MB); 413 and 507 pages

db.init()
worker.start()


# ---------------------------------------------------------------- helpers

from core import csrf_token  # noqa: E402


def _has(feature):
    return bool(g.get("ws")) and plans.has(g.ws, feature)


def _balance():
    return db.balance(g.ws) if g.get("ws") else 0


import llm  # noqa: E402
import platforms as platforms_mod  # noqa: E402

app.jinja_env.globals.update(csrf_token=csrf_token, tools=catalog.DISPLAY_TOOLS, packs=catalog.PACKS, llm_on=llm.configured,
                             assistant_on=llm.anthropic_ready,
                             PLATFORMS=platforms_mod.PLATFORMS, PLATFORM_ORDER=platforms_mod.ORDER,
                             COPY_KINDS=platforms_mod.KINDS,
                             starter_credits=catalog.starter_credits, rates=catalog.rates,
                             billing_configured=billing.configured, starter_image_ads=catalog.STARTER_IMAGE_ADS,
                             plans=plans, has_feature=_has, balance=_balance,
                             comp_until_text=compcodes.free_until_text)

# Sidebar: (endpoint, label, icon, feature or None, endpoints that light it up),
# in groups (title or None, items). NAV is the flat list.
NAV_GROUPS = [
    (None, [
        ("studio", "Home", "home", None, ("studio",)),
        ("create.index", "Create", "create", None, ("create.index", "create.batch", "new", "start")),
        ("library", "Library", "library", None, ("library",)),
        ("brand_kit", "Products", "brand", None, ("brand_kit", "products.new", "products.edit", "products.brand_edit")),
    ]),
    ("Plan", [
        ("campaigns.index", "Campaigns", "planner", "planner", ("campaigns.index", "campaigns.show")),
        ("copy.studio", "Copy", "copy", "copy", ("copy.studio", "copy.run", "copy.optimize", "copy.optimized")),
        ("seo.index", "SEO & AEO", "seo", "seo", ("seo.index", "seo.result")),
        ("competitors.index", "Competitors", "competitors", "competitors", ("competitors.index", "competitors.show")),
    ]),
    ("Grow", [
        ("flows.index", "Flows", "flows", "flows", ("flows.index", "flows.edit", "flows.tasks")),
        ("pages.index", "Pages", "pages", "pages", ("pages.index", "pages.edit")),
        ("pages.links", "Links", "links", "links", ("pages.links", "pages.link_detail")),
        ("forms.index", "Forms", "forms", "forms", ("forms.index", "forms.edit", "forms.submissions")),
        ("forms.contacts", "Contacts", "contacts", "forms", ("forms.contacts", "forms.contact")),
        ("community.admin", "Community", "community", "community", ("community.admin", "community.course_edit",
                                                                     "community.lesson_edit")),
        # Creator Pools (creator_pools.py): shown only where creator_pools.visible() (the CREATOR_POOLS flag)
        ("cpools.index", "Creator Pools", "creators", None, ("cpools.index", "cpools.show")),
    ]),
    ("Launch", [
        ("adhub.index", "Ad accounts", "adhub", "adhub", ("adhub.index", "adhub.platform", "adhub.export_page")),
    ]),
    ("Settings", [
        ("team", "Team", "team", None, ("team",)),
        ("billing_page", "Billing", "billing", None, ("billing_page",)),
        ("messaging.sending", "Sending", "send", "flows", ("messaging.sending",)),
        ("mcp.settings", "API and Claude", "api", None, ("mcp.settings",)),   # shown while MCP_SERVER=on
    ]),
]
NAV = [item for _, items in NAV_GROUPS for item in items]


app.jinja_env.filters["fromjson"] = lambda s: json.loads(s or "{}")


def credits_text(n) -> str:
    """A credit amount as members see it: 1 -> "1 credit", 1250 -> "1,250 credits".
    Costs are shown in credits only; dollars are for plan and pack prices and payments."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    return f"{n:,} credit" + ("" if n == 1 else "s")


def first_upper(s) -> str:
    """Capitalise the first letter only ("AI angle ideas" stays "AI", unlike |capitalize)."""
    s = str(s or "")
    return s[:1].upper() + s[1:]


def localtime(iso, mode="datetime"):
    """A <time> the browser shows in the visitor's own zone (static/app.js);
    without scripts it reads as UTC."""
    from markupsafe import Markup, escape
    iso = str(iso or "")
    if not iso:
        return ""
    fallback = iso[:10] if mode == "date" else iso[:16].replace("T", " ") + " UTC"
    return Markup(f'<time datetime="{escape(iso)}" data-local="{escape(mode)}">{escape(fallback)}</time>')


app.jinja_env.filters.update(first_upper=first_upper, localtime=localtime, credits=credits_text,
                              failure_text=worker.failure_text)
app.jinja_env.globals.update(categories=products.CATEGORIES, category_of=products.category_of)


def _hd_offer(job):
    import jobs_routes
    return jobs_routes.hd_offer(job)


app.jinja_env.globals["hd_offer"] = _hd_offer


def _consent_texts(fm):
    import forms_routes as _fr
    return _fr.consent_texts(fm)


app.jinja_env.globals["consent_texts"] = _consent_texts


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
    out = {"nav_items": NAV, "adhub_links": ADHUB_LINKS, "nav_groups": NAV_GROUPS}
    if g.get("ws") and request.endpoint and request.endpoint != "static":
        with db.connect() as conn:
            out["switcher"] = products.tree(conn, g.ws)
            # Until the first ad, locked (paid-plan) items fold into one "More on paid plans" row.
            out["nav_collapse_locked"] = not conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1",
                                                          (g.ws["id"],)).fetchone()
        out["active_brand"], out["active_product"] = g.get("brand_row"), g.get("product_row")
    return out


@app.context_processor
def _verify_banner():
    """Free accounts that haven't confirmed their email see a banner with Enter
    code and Resend; the page also gets what the confirm box needs (app.js)."""
    if g.get("user") is not None and g.get("ws") is not None and request.endpoint not in (None, "static"):
        with db.connect() as conn:
            if verify.blocked(conn, g.ws, g.user):
                return {"verify_banner": {"email": g.user["email"], "message": verify.message(g.user["email"]),
                                          "box": {"email": g.user["email"], "csrf": csrf_token(),
                                                  "send": url_for("verify_code_send"),
                                                  "check": url_for("verify_code_check")}}}
    return {}


@app.context_processor
def _hold_banner():
    """A refund or dispute put this workspace on a billing hold (billing_holds.py), or its plan's
    renewal payment failed (plans.renewal_paused): every page says so."""
    if g.get("ws") is None or request.endpoint in (None, "static"):
        return {}
    why = core.hold_reason(g.ws)
    if why == "reversal":
        return {"hold_banner": {"message": billing_holds.HOLD_MESSAGE, "email": billing_holds.CONTACT}}
    if why == "renewal":
        return {"hold_banner": {"message": billing_holds.RENEWAL_MESSAGE, "billing": url_for("billing_page")}}
    return {}


CSRF_EXEMPT = {"static", "stripe_webhook", "site_event", "forms.public", "messaging.resend_webhook", "messaging.unsubscribe",
               "sms.inbound", "sms.status",
               "cpools.partner_asset_url", "cpools.partner_event",  # Crowdcut's server calls, HMAC-signed (creator_pools.py)  # public forms carry their own signed token
               # MCP clients (mcpserver.py): Bearer tokens or OAuth client credentials, never cookies
               "mcp.mcp_endpoint", "mcp.register", "mcp.token", "mcp.revoke"}


@app.before_request
def load_user():
    g.user = g.ws = g.viewing = None
    uid = session.get("uid")
    if uid:
        with db.connect() as conn:
            u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if u:
                g.user = u
                ws_id = u["workspace_id"]
                # An admin viewing another workspace (/admin "View"): the app shows
                # that workspace, read-only (see the POST check below).
                vw = session.get("view_ws")
                if vw:
                    other = conn.execute("SELECT * FROM workspaces WHERE id=?", (int(vw),)).fetchone() \
                        if u["is_admin"] and int(vw) != u["workspace_id"] else None
                    if other:
                        ws_id, g.viewing = other["id"], other
                    else:
                        session.pop("view_ws", None)
                core.load_workspace(conn, ws_id)
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        sent = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "This form expired. Go back, refresh and try again.")
    if g.viewing and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.endpoint not in VIEW_WRITE_OK and not (request.endpoint or "").startswith("admin"):
        abort(403, f"You're viewing {g.viewing['name']} read-only. Go back to your own workspace to make or "
                   "change anything.")


# What an admin may still POST while viewing another workspace (everything else
# is read-only there): leaving the view and signing out. /admin* routes act on
# explicit workspace ids, so they stay open too.
VIEW_WRITE_OK = {"admin_view_exit", "logout", "cpools.admin_page"}


@app.after_request
def _security_headers(resp):
    """Baseline on every response (pages that need stricter set their own CSP):
    files are served as the type we say (no sniffing an uploaded "photo" into a
    page), and nothing of ours is framed by another site (clickjacking the
    Make it, Billing or Team buttons)."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith(PRIVATE_PREFIXES) or request.path in PRIVATE_PATHS:
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    _first_touch(resp)
    return resp


# Never indexed, whatever they render (the app, admin, accounts, files, webhooks, share links).
PRIVATE_PREFIXES = ("/app", "/admin", "/api/", "/media/", "/photo/", "/ref/", "/presenter/", "/verify", "/webhooks/",
                    "/s/", "/t/", "/u/", "/_", "/auth/")
PRIVATE_PATHS = {"/login", "/signup", "/logout", "/health"}
# Our own public pages: where a visit's UTM tags / Google click id are kept for sign-up (gads.py).
# Members' pages and smart links (/p/, /l/, /f/, /c/) carry *their* campaigns and are left alone.
FIRST_TOUCH_ENDPOINTS = {"index", "features", "pricing", "solution", "privacy", "terms", "messaging.policy",
                         "signup", "login"}


def _first_touch(resp):
    if request.method != "GET" or request.endpoint not in FIRST_TOUCH_ENDPOINTS or not request.args:
        return
    from urllib.parse import urlsplit
    ref = urlsplit(request.referrer or "").hostname or ""
    value = gads.touch(request.args, request.headers, request.path, request.cookies.get(gads.COOKIE),
                       "" if ref == request.host.split(":")[0] else ref)
    if value:
        resp.set_cookie(gads.COOKIE, value, max_age=gads.COOKIE_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=request.is_secure)


def _signup_offer() -> dict:
    """What a free sign-up gives this visitor right now (adoffer.offer): the paid-ad
    offer only when sign-up would grant it to them, the normal starter credits otherwise."""
    if "signup_offer" not in g:
        n = None
        if adoffer.credits() and not g.get("user"):
            touch = adoffer.effective_touch(request, FIRST_TOUCH_ENDPOINTS)
            if adoffer.paid_source(touch):
                with db.connect() as conn:
                    if adoffer.eligible(conn, touch, client_ip(), bool(request.cookies.get(adoffer.BROWSER_COOKIE))):
                        n = adoffer.credits()
        g.signup_offer = adoffer.offer(n)
    return g.signup_offer


app.jinja_env.globals["signup_offer"] = _signup_offer


@app.context_processor
def _public_meta():
    """Title, description, canonical, share card and JSON-LD for the public pages
    (public_site.py), plus the Google tag, the ad pixels (pixels.py) and this page's conversion events."""
    out = {"gads_tag": gads.tag_id(), "gads_consent_regions": gads.CONSENT_REGIONS, "gads_events": _gads_events,
           "ad_pixels": lambda: pixels.context(request, _gads_events, request.endpoint),
           "site_beacon": lambda: site_events.beacon(request, g.get("user"))}
    m = public_site.meta(request.endpoint, request.view_args)
    if m:
        out["page_meta"] = m
    return out


def _gads_events():
    """The conversions to fire on this page, taken from the session once per request
    (the Google tag and the pixels both read this same list)."""
    if "gads_events" not in g:
        g.gads_events = gads.take(session) if gads.SESSION_KEY in session else []
    return g.gads_events


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


# Landing-page media. Every example was generated for this site with AI: the
# products, brands (Aurelle Roast, Petalune, ...) and presenters are invented,
# and nothing here is presented as a customer's result. Presenter clips
# (`talk`) are labeled as AI presenters, have sound and make no result claims.
_PRODUCT_HERO = {"src": "previews/ad_image.mp4", "poster": "previews/ad_image.jpg", "title": "Product hero",
                 "tool": "Coffee bag", "alt": "An unlabeled terracotta coffee bag on dark slate as a warm light sweeps across it."}
_FLOATING_HERO = {"src": "previews/photo_to_video.mp4", "poster": "previews/photo_to_video.jpg", "title": "Floating hero",
                  "tool": "Sneaker", "alt": "A plain white sneaker turning slowly above a sand-colored pedestal."}
_IN_HAND = {"src": "previews/motion_copy.mp4", "poster": "previews/motion_copy.jpg", "title": "In hand", "tool": "Serum",
            "alt": "A hand turning a frosted serum bottle against rippling green light."}
GALLERY = [
    {"src": "site/show-amber.mp4", "poster": "site/show-amber.jpg", "title": "Molten gold", "tool": "Coffee bag",
     "alt": "A gold coffee bag floating before a wall of type, then landing in a splash of liquid gold."},
    {"src": "site/ugc-coffee.mp4", "poster": "site/ugc-coffee.jpg", "title": "Coffee review", "tool": "AI presenter",
     "talk": True, "alt": "An AI presenter in a sunny kitchen holds up a coffee bag and talks about how it smells."},
    {"src": "site/show-kawaii.mp4", "poster": "site/show-kawaii.jpg", "title": "Kawaii summer", "tool": "Coffee bag",
     "alt": "A coffee bag on a pink gingham beach picnic with glass hearts, a splash and cute speech bubbles."},
    {"src": "site/ugc-serum.mp4", "poster": "site/ugc-serum.jpg", "title": "Serum routine", "tool": "AI presenter",
     "talk": True, "alt": "An AI presenter in a bright bathroom shows a face serum and puts a drop on her hand."},
    {"src": "site/show-y2k.mp4", "poster": "site/show-y2k.jpg", "title": "Y2K charms", "tool": "Coffee bag",
     "alt": "A coffee bag on denim among chains, charms and a pink flip phone, ending as a backlit silhouette."},
    {"src": "site/ugc-sneakers.mp4", "poster": "site/ugc-sneakers.jpg", "title": "Gym sneakers", "tool": "AI presenter",
     "talk": True, "alt": "An AI presenter in a gym shows off a sage-green running sneaker."},
    {"src": "site/show-pastel.mp4", "poster": "site/show-pastel.jpg", "title": "Pastel studio", "tool": "Coffee bag",
     "alt": "A coffee bag tumbling among floating pastel cubes and landing on a white pedestal."},
    {"src": "site/ugc-app.mp4", "poster": "site/ugc-app.jpg", "title": "App demo", "tool": "AI presenter",
     "talk": True, "alt": "An AI presenter at a desk shows a to-do list app on his phone."},
    {"src": "site/show-ingredients.mp4", "poster": "site/show-ingredients.jpg", "title": "Ingredient story",
     "tool": "Coffee bag", "alt": "Macro shots of coffee beans and cinnamon with bold titles, ending on the bag."},
    {"src": "site/ugc-bakery.mp4", "poster": "site/ugc-bakery.jpg", "title": "Local bakery", "tool": "AI presenter",
     "talk": True, "alt": "An AI presenter behind a bakery counter shows a tray of fresh croissants."},
    _PRODUCT_HERO, _FLOATING_HERO, _IN_HAND,
    {"src": "site/headphones.mp4", "poster": "site/headphones.jpg", "title": "Bold color", "tool": "Headphones",
     "alt": "Blue over-ear headphones floating against a bright orange background."},
    {"src": "site/candle.mp4", "poster": "site/candle.jpg", "title": "Mood", "tool": "Candle",
     "alt": "A lit candle in an amber glass jar beside dried eucalyptus."},
]
HERO_MEDIA = [_IN_HAND, _PRODUCT_HERO, _FLOATING_HERO]
# Clips the answer-first pages can put in their hero (public_site.SOLUTIONS[...]["clips"]), by a short key.
CLIP_BY_KEY = {g["poster"].rsplit("/", 1)[-1].rsplit(".", 1)[0]: g for g in GALLERY}
CLIP_BY_KEY["product-video"] = dict(_FLOATING_HERO, title="5-second product video")

# The landing page's spine: one campaign, start to finish. `features` are
# plans.FEATURES keys (their plan tags come from there); `shot` is a real
# screenshot of the app (light/dark), `video` a generated loop.
JOURNEY = [
    {"id": "plan", "step": "Plan it", "title": "Plan the whole campaign",
     "body": "Give the AI planner your offer, audience, goal, budget and platforms. It proposes angles, writes hooks "
             "and scripts, lines up the ads with a credit quote for each, then adds a budget split, what to watch "
             "and a launch checklist.",
     "features": ["planner", "competitors"], "shot": "plan", "alt": "A campaign plan with a budget split, what to watch and three angles."},
    {"id": "make", "step": "Make the ads", "title": "Make the ads from one product photo",
     "body": "Pick the product, choose an angle and let AI write the script. Then make UGC videos with AI "
             "presenters, product videos, image ads in four shapes, or copy the motion of a reference clip, in "
             "batches of variants. Nothing is generated until you've seen the price.",
     "features": ["studio", "brand_kit"], "video": "site/moodboard",
     "alt": "Hands placing printed product photos into a grid on a dark desk."},
    {"id": "copy", "step": "Write the copy", "title": "Write the copy for every platform",
     "body": "Every text field each platform takes, sized to its display lengths, plus hooks, captions, headlines, "
             "emails and landing copy. Paste a draft into the content optimizer for a score and three rewrites.",
     "features": ["copy", "optimizer"], "shot": "copy", "alt": "Ad copy variants for Facebook & Instagram and TikTok hooks."},
    {"id": "found", "step": "Get found", "title": "Get found in search and in AI answers",
     "body": "Audit a page for search engines, answer engines and AI assistants. You get the fixes in order, a "
             "better title and description, an FAQ block, ready-to-paste schema and an llms.txt draft.",
     "features": ["seo"], "shot": "seo", "alt": "An SEO, AEO and GEO audit with a score and the fixes in order."},
    {"id": "launch", "step": "Set up and launch", "title": "Set up every ad account, then launch",
     "body": "Step-by-step setup for eight ad platforms with links to each one's own guides, pixel and tag basics, "
             "and ticks your team shares. Export each finished ad at every platform's sizes and launch it in that "
             "platform's ads manager.",
     "features": ["adhub"], "shot": "adhub", "alt": "The ad-account setup hub with progress for each platform."},
    {"id": "leads", "step": "Capture leads", "title": "Turn clicks into contacts",
     "body": "Publish a landing page from blocks, share smart links with your UTM tags, and add forms. Everyone who "
             "fills one in lands in your contacts with the page, link and form they came through.",
     "features": ["pages", "links", "forms"], "shot": "page", "themed": False,
     "alt": "A published landing page with benefits and a sign-up form."},
    {"id": "grow", "step": "Grow an audience", "title": "Keep the people you reach",
     "body": "Run a community on campaignify.ai: spaces and posts, courses with lessons, members with their own "
             "logins, and events with an add-to-calendar file.",
     "features": ["community"], "shot": "community", "alt": "A community home with spaces, courses, posts and an event."},
    {"id": "measure", "step": "See what's working", "title": "See what's working",
     "body": "Clicks per day, referrers, countries, devices and UTM sources for every smart link, with page views "
             "and form submissions alongside. It covers the pages, links and forms you run on Campaignify; ad "
             "spend and results stay in each platform's ads manager.",
     "features": ["link_analytics"], "shot": "links", "alt": "Smart-link analytics: clicks over 30 days and referrers."},
]


@app.route("/")
def index():
    """Logged in: the studio. Logged out: the public landing page."""
    if g.user:
        return redirect(url_for("studio"))
    return render_template("landing.html", gallery=GALLERY, hero=HERO_MEDIA, journey=JOURNEY,
                           faq=public_site.home_faq())


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


@app.route(site_events.ENDPOINT, methods=["POST"])
def site_event():
    """First-party funnel beacon (web/_beacon.html): CLIENT_EVENTS only, small, rate-limited, no bots."""
    body, status = site_events.handle(request, g.get("user"))
    resp = app.response_class(body, status=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/features")
def features():
    return render_template("features.html", feature_details=FEATURE_DETAILS, journey=JOURNEY)


@app.route("/for-reps")
def for_reps():
    """The compliance block for independent reps (the For reps nav link), off the home page's default path."""
    return render_template("for_reps.html")


@app.route("/pricing")
def pricing():
    cpools = None
    if creator_pools.public() and creator_pools.connected():
        cpools = {"fee": creator_pools.money(creator_pools.fee_cents()),
                  "min_pool": creator_pools.money(creator_pools.min_pool_cents()), "refund_days": creator_pools.REFUND_DAYS}
    return render_template("pricing.html", faq=public_site.pricing_faq(), creator_pools=cpools)


def solution(slug):
    """The answer-first pages for search intent (public_site.SOLUTIONS)."""
    sol = public_site.solution(slug)
    return render_template("solution.html", s=sol, clips=[CLIP_BY_KEY[k] for k in sol.get("clips", [])],
                           others=[public_site.card(o) for o in public_site.SOLUTIONS[slug]["related"]])


def guide(slug):
    """The guides and the roundup (public_pages.GUIDES)."""
    g = public_site.guide(slug)
    return render_template("guide.html", gd=g, clips=[CLIP_BY_KEY[k] for k in g.get("clips", [])],
                           others=[public_site.card(o) for o in g["related"]])


@app.route("/guides")
def guides():
    return render_template("guides.html", ix=public_site.GUIDES_INDEX,
                           items=[public_site.guide(k) for k in public_site.GUIDE_ORDER])


for _slug in public_site.SOLUTION_ORDER:
    app.add_url_rule(f"/{_slug}", "solution", solution, defaults={"slug": _slug})
for _slug in public_site.GUIDE_ORDER:
    app.add_url_rule(public_site.guide_path(_slug), "guide", guide, defaults={"slug": _slug})


@app.route("/robots.txt")
def robots_txt():
    body = ("User-agent: *\nDisallow: /app\nDisallow: /admin\nDisallow: /api\nDisallow: /media\nDisallow: /photo\n"
            "Disallow: /ref\nDisallow: /verify\nDisallow: /webhooks\nDisallow: /t/\nDisallow: /u/\nAllow: /\n\n"
            f"Sitemap: {public_site.SITE_URL}/sitemap.xml\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """The public pages only: never the app, admin, share links (/s/) or members' pages."""
    mod = public_site.lastmod()
    urls = "".join(f"<url><loc>{public_site.absolute(p)}</loc><lastmod>{mod}</lastmod></url>"
                   for p in public_site.sitemap_paths())
    return app.response_class('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                              f'sitemap/0.9">{urls}</urlset>', mimetype="application/xml")


def _llms_text() -> str:
    import mcpserver
    return render_template("llms.txt", mcp_on=mcpserver.enabled(), feature_details=FEATURE_DETAILS, journey=JOURNEY, site=public_site,
                           tools_full=catalog.TOOLS, rc=catalog.rates(),
                           solutions=[public_site.solution(x) for x in public_site.SOLUTION_ORDER],
                           guides=[public_site.guide(x) for x in public_site.GUIDE_ORDER])


@app.route("/llms.txt")
def llms_txt():
    return app.response_class(_llms_text(), mimetype="text/plain")


@app.route("/llms-full.txt")
def llms_full_txt():
    """llms.txt plus the full text of every answer-first page and FAQ, for AI assistants that read one file."""
    body = render_template("llms-full.txt", summary=_llms_text(), site=public_site, home_faq=public_site.home_faq(),
                           solutions=[public_site.solution(x) for x in public_site.SOLUTION_ORDER],
                           guides=[public_site.guide(x) for x in public_site.GUIDE_ORDER],
                           answer_text=public_site.answer_text)
    return app.response_class(body, mimetype="text/plain")


def indexnow_key_file():
    """IndexNow's key file (public by design): search engines read it to confirm our pings."""
    return app.response_class(public_site.INDEXNOW_KEY, mimetype="text/plain")


app.add_url_rule(f"/{public_site.INDEXNOW_KEY}.txt", "indexnow_key", indexnow_key_file)


@app.route("/llm-info")
def llm_info():
    """The same text as /llms.txt as an HTML page (headings, lists and links), for people and crawlers that read HTML."""
    return render_template("llm_info.html", blocks=public_site.text_blocks(_llms_text()))


@app.route("/docs/mcp")
def mcp_docs():
    """How to connect Claude and other MCP clients (mcp_tools.py)."""
    import mcp_tools
    import mcpserver
    return render_template("mcp_docs.html", on=mcpserver.enabled(), url=request.host_url.rstrip("/") + "/mcp",
                           settings_path=mcp_tools.CampaignifyAdapter.settings_path,
                           tools=[{"name": t.name, "description": t.description, "annotations": t.annotations}
                                  for t in mcp_tools.TOOLS])


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/health")
def health():
    """Database read and write, the worker heartbeat, disk and the vendor's last
    probe (alerts.py). 503 when the database or the worker is down."""
    out, code = alerts.health()
    out["generation_configured"] = hf.configured()
    if request.args.get("deep"):
        out["generation"] = hf.probe()
    return jsonify(out), code


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
        if u and u["auth_provider"] == "google":
            flash("This account signs in with Google. Use the Google button.")
        else:
            flash("That email and password don't match.")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Open to anyone: a new account gets its own workspace and the starter
    credits, then the guided first ad (accounts.self_serve_signup, shared with
    "Continue with Google"). An invite link joins an existing workspace instead;
    owner setup links (owners.py) make a Rector & Stull admin and work only with
    their own email."""
    code = (request.form.get("invite") if request.method == "POST" else request.args.get("invite")) or ""
    code = code.strip()
    owner = owners.owner_email(code)

    comp = ((request.form.get("comp_code") if request.method == "POST" else request.args.get("code")) or "").strip()[:40]

    def again(msg, status=200):
        flash(msg)
        return render_template("signup.html", invite=code, owner=owner, email=request.form.get("email", ""),
                               comp_code=comp), status

    if g.user:
        return redirect(url_for("studio"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        name = (request.form.get("name") or "").strip()[:80]   # no longer asked; kept if an old form sends it
        ip = client_ip()
        if request.form.get("website"):            # honeypot: people never see this field
            return again("Something went wrong. Please try again.", 400)
        if "@" not in email or "." not in email.split("@")[-1] or len(email) > 200 or len(pw) < 8:
            return again("Use a real email and a password of at least 8 characters.")
        if not owner and accounts.signup_throttled(ip):
            return again("Too many new accounts from your network. Try again later.", 429)
        if not owner:
            try:
                return accounts.self_serve_signup(email, name, generate_password_hash(pw), request, ip, app.secret_key,
                                                  invite_code=code, comp_code=comp)
            except accounts.SignupRefused as e:
                return again(str(e), e.status)
        # An owner setup link: a Rector & Stull admin, confirmed (the link only works for its own address).
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                return again("That email already has an account. Log in instead.")
            if owner != email:
                return again("This setup link is for a different email.")
            ws = conn.execute("SELECT id FROM workspaces WHERE name=? ORDER BY id LIMIT 1",
                              ("Rector & Stull",)).fetchone()
            ws_id = ws["id"] if ws else conn.execute(
                "INSERT INTO workspaces (name, credits, created_at) VALUES (?,?,?)",
                ("Rector & Stull", 0, db.now())).lastrowid
            joins = conn.execute("SELECT 1 FROM users WHERE workspace_id=?", (ws_id,)).fetchone() is not None
            cur = conn.execute("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at, role, "
                               "email_verified_at, verify_nonce, first_touch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (email, name, generate_password_hash(pw), 1, ws_id, db.now(),
                                "member" if joins else "owner", db.now(), None,
                                gads.first_touch_json(request.cookies.get(gads.COOKIE))))
            conn.commit()
            session.clear()
            session["uid"] = cur.lastrowid
            csrf_token()
        resp = redirect(url_for("brand_kit"))
        if request.cookies.get(gads.COOKIE):
            resp.delete_cookie(gads.COOKIE)   # it's on the account now
        return resp
    return render_template("signup.html", invite=code, owner=owner, email="", comp_code=comp)


@app.route("/verify")
def verify_email():
    """The link in the confirmation email. Works once, for 48 hours; it confirms
    the address but doesn't log anyone in."""
    with db.connect() as conn:
        outcome, u = verify.confirm(conn, app.secret_key, request.args.get("t") or "")
        conn.commit()
    mine = bool(g.user and u is not None and g.user["id"] == u["id"])
    if outcome == "ok":
        flash("Email confirmed. Video ads are unlocked." if mine else
              "That email is confirmed." if g.user else "Email confirmed. Log in to make your ads.", "ok")
    elif outcome == "already":
        flash("Your email is already confirmed.", "ok")
    elif outcome == "expired":
        flash("That link has expired. Press Resend for a new one." if g.user else
              "That link has expired. Log in and press Resend for a new one.")
    else:
        flash("That link doesn't work. Use the newest email we sent, or press Resend." if g.user else
              "That link doesn't work. Log in and press Resend for a new one.")
    return redirect(url_for("studio") if g.user else url_for("login"))


@app.route("/m")
def magic_login():
    """The link in a first-run nudge email (nudges.py): logs that account in and
    opens Home, the first-ad form. Reaching the inbox confirms the address too."""
    with db.connect() as conn:
        u = nudges.open_link(conn, app.secret_key, request.args.get("t") or "")
    if u is None:
        flash("That link has expired. Log in with your email and password.")
        return redirect(url_for("login"))
    session.clear()
    session["uid"] = u["id"]
    csrf_token()
    return redirect(url_for("studio"))


def _resend_wait():
    """Why a confirmation email can't go out right now (once a minute, three an
    hour per account, ten an hour per network), or None after recording the send."""
    key, ipkey = f"verify:{g.user['id']}", f"verify-ip:{client_ip()}"
    if db.throttled(key, 1, verify.RESEND_GAP_S):
        return "We just sent one. Give it a minute, and check your spam folder."
    if db.throttled(key, verify.RESEND_PER_HOUR, 3600) or db.throttled(ipkey, verify.RESEND_PER_IP_HOUR, 3600):
        return "We've sent a few links already. Check your spam folder, or try again in an hour."
    db.hit(key)
    db.hit(ipkey)
    return None


@app.route("/app/verify/resend", methods=["POST"])
@login_required
def verify_resend():
    """Send the confirmation link and a new code again: once a minute, a few times an hour."""
    back = request.form.get("back") or ""
    dest = back if back.startswith("/") and not back.startswith("//") else url_for("studio")
    if verify.is_verified(g.user):
        flash("Your email is already confirmed.", "ok")
        return redirect(dest)
    wait = _resend_wait()
    if wait:
        flash(wait)
        return redirect(dest)
    with db.connect() as conn:
        sent = verify.send_link(conn, app.secret_key, g.user)
    flash(f"Sent a new link and code to {g.user['email']}. It can take a minute; check spam too." if sent else
          "We couldn't send the email just now. Try again in a few minutes.", "ok" if sent else "message")
    return redirect(dest)


@app.route("/app/verify/code/send", methods=["POST"])
@login_required
def verify_code_send():
    """The confirm box (JSON). {"auto": true} when it opens: send a code only if
    the last one can't be used any more. Otherwise it's the Resend code button.
    Same limits as Resend."""
    if verify.is_verified(g.user):
        return jsonify({"ok": True, "verified": True})
    auto = bool((request.get_json(silent=True) or {}).get("auto"))
    if auto and verify.code_live(g.user):
        return jsonify({"ok": True, "sent": False, "message": f"We sent a code to {g.user['email']}."})
    wait = _resend_wait()
    if wait:
        if auto:   # the signup email (or the last Resend) is still on its way
            return jsonify({"ok": True, "sent": False, "message": wait})
        return jsonify({"error": wait}), 429
    with db.connect() as conn:
        sent = verify.send_link(conn, app.secret_key, g.user)
    if not sent:
        return jsonify({"error": "We couldn't send the email just now. Try again in a few minutes."}), 503
    return jsonify({"ok": True, "sent": True, "message": f"We sent a new code to {g.user['email']}. "
                                                         "It can take a minute; check spam too."})


@app.route("/app/verify/code", methods=["POST"])
@login_required
def verify_code_check():
    """Check the 6-digit code from the confirm box (JSON {"code": "123456"})."""
    ipkey = f"verify-check-ip:{client_ip()}"
    if db.throttled(ipkey, verify.CHECKS_PER_IP_HOUR, 3600):
        return jsonify({"error": "Too many tries from here. Use the link in the email, or try again in an hour."}), 429
    db.hit(ipkey)
    code = str((request.get_json(silent=True) or {}).get("code") or request.form.get("code") or "")
    with db.connect() as conn:
        outcome, left = verify.check_code(conn, app.secret_key, g.user, code)
    if outcome in ("ok", "already"):
        return jsonify({"ok": True, "message": "Email confirmed. Video ads are unlocked."})
    msg = {"format": "Enter the 6-digit code from the email.",
           "none": "Press Resend code and we'll email you one.",
           "expired": "That code has expired. Press Resend code for a new one.",
           "locked": "Too many wrong tries. Press Resend code for a new one.",
           "wrong": f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left."}[outcome]
    return jsonify({"error": msg, "outcome": outcome, "left": left}), 400


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- studio

@app.route("/app")
@login_required
def studio():
    return _studio_page()


def _studio_page(form=None, status=200):
    """Home: for a new account, the first-ad form and nothing else; afterwards,
    ad types for the active product, its saved angles, recent batches and the
    template library."""
    pid = g.ws["product_id"]
    with db.connect() as conn:
        recent = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 6",
                              (g.ws["id"],)).fetchall()
        photos = conn.execute("SELECT COUNT(*) c FROM photos WHERE workspace_id=?", (g.ws["id"],)).fetchone()["c"]
        outs = _outputs(conn, [j["id"] for j in recent])
        angles = conn.execute("SELECT * FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 8",
                              (pid or 0, g.ws["id"])).fetchall()
        batches = conn.execute("SELECT b.*, (SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id) n, "
                               "(SELECT COUNT(*) FROM jobs j WHERE j.batch_id=b.id AND j.status IN ('pending','running')) busy, "
                               "p.name pname FROM batches b LEFT JOIN products p ON p.id=b.product_id "
                               "WHERE b.workspace_id=? ORDER BY b.id DESC LIMIT 5", (g.ws["id"],)).fetchall()
        nprod = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=? AND archived=0", (g.ws["id"],)).fetchone()[0]
        pphotos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (pid or 0,)).fetchone()[0]
    from_price = catalog.from_prices()
    bal = db.balance(g.ws)
    types = sorted(catalog.ad_types(), key=lambda t: from_price[t["tool"]] > bal)  # what they can afford first
    first, pv_ok, made = None, False, None
    with db.connect() as conn:
        if not recent or request.args.get("preview") or form:
            first = _first_ad_context(conn, form or ({"kind": "preview"} if request.args.get("preview") else {}))
            pv_ok = first["preview"]["ok"]
        else:
            pv_ok = preview.status(conn, g.ws, g.user["email"], g.user)["ok"]
            made = _made_context(conn, batches, request.args.get("made", type=int))
    return render_template("studio.html", templates=catalog.templates_for(brand()), templates_by_id=catalog.TEMPLATE_BY_ID,
                           recent=recent, outs=outs, photos=photos, brand=brand(), configured=hf.configured(),
                           first_run=not recent or bool(first), ad_types=types, preview_ok=pv_ok, angles=angles, batches=batches,
                           nprod=nprod, pphotos=pphotos, from_price=from_price, ad_type_names=catalog.AD_TYPE_BY_ID,
                           bal=bal, first=first, preview_seconds=preview.SECONDS, made=made,
                           verify_banner_below=bool(first)), status


def _made_context(conn, batches, made_id):
    """The first batch, rendering on Home right after the first-run form (or
    while the workspace's only batch is still busy): its jobs as live cards, the
    time it usually takes, and whether the first finished ad is on the page."""
    import jobs_routes
    b = None
    if made_id:
        b = conn.execute("SELECT * FROM batches WHERE id=? AND workspace_id=?", (made_id, g.ws["id"])).fetchone()
    elif len(batches) == 1 and batches[0]["busy"]:
        b = batches[0]
    if b is None:
        return None
    jobs = conn.execute("SELECT * FROM jobs WHERE batch_id=? AND workspace_id=? AND deleted_at IS NULL ORDER BY id",
                        (b["id"], g.ws["id"])).fetchall()
    if not jobs:
        return None
    first_batch = conn.execute("SELECT COUNT(*) FROM batches WHERE workspace_id=? AND id<?",
                               (g.ws["id"], b["id"])).fetchone()[0] == 0
    cfg = json.loads(b["config_json"] or "{}")
    return {"batch": b, "jobs": jobs, "outs": _outputs(conn, [j["id"] for j in jobs]),
            "running": [j for j in jobs if j["status"] in ("pending", "running")],
            "done": [j for j in jobs if j["status"] == "done"], "eta": jobs_routes.eta_minutes(conn),
            "verdicts": jobs_routes.verdicts(conn, [j["id"] for j in jobs]), "first_batch": first_batch,
            "is_video": cfg.get("type") != "image", "video_unlock": verify.needs(conn, g.ws, g.user, "photo_to_video"),
            "full_price": catalog.price_credits("ugc_video", duration=preview.SECONDS) if cfg.get("type") == "ugc" else 0}


def _first_ad_context(conn, form):
    """What the first-ad form needs: templates, photos already added, the kinds on
    offer and which one is picked first.

    Everyone starts on image ads (2 at once when the free credits cover them: the
    first thing people should see is the form, not a confirm-your-email wall).
    Ad-grant accounts (adoffer.py, 500 credits) are offered the 5-second product
    video first, since the grant covers it. The free talking-video preview stays
    as a later choice, "after you confirm your email"."""
    pv = preview.status(conn, g.ws, g.user["email"], g.user)
    photos = conn.execute("SELECT * FROM photos WHERE workspace_id=? ORDER BY id DESC LIMIT 12",
                          (g.ws["id"],)).fetchall()
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
    bal = db.balance(ws)
    price = catalog.price_credits("ad_image", batch=1)
    two = catalog.price_credits("ad_image", batch=2)
    n_images = 2 if bal >= two else 1
    images_price = two if n_images == 2 else price
    video_price = catalog.price_credits("photo_to_video", duration=5)
    grant = conn.execute("SELECT 1 FROM ad_signup_grants WHERE workspace_id=?", (g.ws["id"],)).fetchone() is not None
    video_on = grant and bal >= video_price
    kind = form.get("kind") if form.get("kind") in ("image", "video", "preview") else ("video" if video_on else "image")
    if kind == "video" and not video_on:
        kind = "image"
    tpls = catalog.first_run_templates(brand())
    # Which choice opens the confirm-your-email box first: any video, and an image ad only once the
    # unconfirmed allowance is used up.
    needs = {"preview": verify.needs(conn, ws, g.user, "ugc_video"),
             "image": verify.needs(conn, ws, g.user, "ad_image", images_price),
             "video": verify.needs(conn, ws, g.user, "photo_to_video", video_price)}
    return {"tpls": tpls, "photos": photos, "form": form, "preview": pv, "kind": kind, "needs": needs,
            "price": price, "n_images": n_images, "images_price": images_price, "video_on": video_on,
            "video_price": video_price, "grant": grant, "bal": bal,
            "free_images": n_images == 2 and bal <= catalog.starter_credits(),   # the 2 free ads of a plain sign-up
            "presenters": [p for p in presenter_stills.with_photos(url_for) if p["id"] in preview.PRESENTERS],
            "line": form.get("line") or "", "default_line": preview.DEFAULT_LINE, "pick": form.get("template") or
            tpls[0]["id"], "seconds": preview.SECONDS, "scene": FIRST_VIDEO_SCENE,
            "max_words": int(catalog.max_words(preview.SECONDS) * 1.2), "verify_email": g.user["email"],
            "import_quote": 0}


FIRST_VIDEO_SCENE = ("A slow, smooth turn of {product} on a clean surface in soft daylight, the label readable, "
                     "nothing else in frame.")


@app.route("/app/start/import", methods=["POST"])
@login_required
def start_import():
    """The first-run form's "Paste your product page" (JSON {"url"}): the same
    importer as Products > Import from a link (product_routes.import_url), without
    the AI read, so it costs nothing and the free credits stay for the ads. Makes
    the product, downloads its first photo, and answers with what the form
    pre-fills: name, brand, the photo, a description for the brief."""
    import fetcher
    import product_import
    import product_routes
    url = str((request.get_json(silent=True) or {}).get("url") or request.form.get("url") or "").strip()[:500]
    if not url:
        return jsonify({"error": "Paste the link to your product's page."}), 400
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    key = f"import:{g.ws['id']}"
    if db.throttled(key, product_routes.IMPORTS_PER_HOUR, 3600):
        return jsonify({"error": "That's a lot of imports this hour. Add the name and a photo by hand."}), 429
    db.hit(key)
    with db.connect() as conn:
        try:
            pid, facts, _ = product_routes.import_url(conn, url, g.ws["brand_id"], use_ai=False)
            products.activate(conn, g.ws, product_id=pid)
        except product_import.Blocked:
            return jsonify({"error": "That store doesn't let apps read its pages. Type the name and upload a photo "
                                     "instead; it takes 30 seconds.", "manual": True}), 422
        except fetcher.FetchError as e:
            return jsonify({"error": f"Couldn't read that page: {e} Type the name and upload a photo instead.",
                            "manual": True}), 422
        except products.ProductError as e:
            return jsonify({"error": str(e), "manual": True}), 422
        p = products.get_product(conn, g.ws["id"], pid)
        imp = products.import_info(p)
        photo = None
        if imp.get("images"):
            product_routes.add_candidate_photos(conn, g.ws["id"], p, imp["images"][:1])
            row = conn.execute("SELECT id, filename FROM photos WHERE product_id=? ORDER BY id DESC LIMIT 1",
                               (pid,)).fetchone()
            if row:
                photo = {"id": row["id"], "url": url_for("photo", pid=row["id"]), "name": row["filename"] or ""}
        b = products.get_brand(conn, g.ws["id"], p["brand_id"])
    return jsonify({"ok": True, "product_id": pid, "name": p["name"] or "", "brand": (b["brand_name"] if b else "") or "",
                    "description": (p["description"] or "")[:300] if "description" in p.keys() else "",
                    "photo": photo, "photos": len(imp.get("images") or [])})


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
    """Retired (owner decision D1): templates are presets in Create now. Old links redirect."""
    tpl = catalog.TEMPLATE_BY_ID.get(request.args.get("template", ""))
    pid = picked_product()
    if tpl:
        return redirect(url_for("create.index", template=tpl["id"], product=pid))
    typ = catalog.TOOL_AD_TYPE.get(request.args.get("tool", ""), "image")
    return redirect(url_for("create.index", type=typ, product=pid))


@app.route("/app/generate", methods=["POST"])
@login_required
def generate():
    f = request.form
    tool = f.get("tool")
    if tool not in catalog.offered_tools() or tool == "ugc_video":
        abort(400)
    pid = picked_product() or g.ws["product_id"]
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
        jid = _queue_job(conn, tool, tpl_id, brief, params, usd, credits, product_id=pid)
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
    """The first-ad form lives on Home; this takes its POST. Product name, brand
    (optional) and one photo (uploaded, or the one the page import found), then
    one of: image ads from a template (2 at once when the free credits cover
    them), the 5-second product video (ad-grant accounts), or the free
    talking-video preview. Fills the product as a side effect and goes back to
    Home, which shows the ads rendering."""
    if request.method == "GET":
        return redirect(url_for("studio"))
    f = request.form
    kind = f.get("kind") if f.get("kind") in ("image", "video", "preview") else "image"
    product = (f.get("product_name") or "").strip()[:80]
    brand_name = (f.get("brand_name") or "").strip()[:80]
    audience = (f.get("audience") or "").strip()[:200]
    tpl = catalog.TEMPLATE_BY_ID.get(f.get("template") or "")
    tpl = tpl if tpl and tpl["tool"] == "ad_image" else None

    def again(msg):
        flash(msg)
        return _studio_page(f, 400)

    with db.connect() as conn:
        ctx = _first_ad_context(conn, f)
    if kind == "video" and not ctx["video_on"]:
        return again("The free video ad isn't on this account. Make image ads instead.")
    n_images = ctx["n_images"]
    img_price = ctx["images_price"]
    tool = {"image": "ad_image", "video": "photo_to_video", "preview": "ugc_video"}[kind]
    with db.connect() as conn:   # videos wait for a confirmed email; image ads don't
        blocked = core_verify_block(conn, tool, {"image": img_price, "video": ctx["video_price"], "preview": 0}[kind])
    if blocked:
        flash(blocked, "verify-cap" if g.get("verify_reason") == "cap" else "verify")
        return _studio_page(f, 403)
    if not product:
        return again("Add your product's name.")
    b = {"name": brand_name or g.ws["brand_name"] or "", "product": product}
    if kind == "preview":
        presenter = f.get("presenter") if f.get("presenter") in preview.PRESENTERS else preview.PRESENTERS[0]
        line = " ".join((f.get("line") or "").split())[:300] or preview.line_for(product)
        line = catalog.fill(line, b)
        if len(line.split()) > int(catalog.max_words(preview.SECONDS) * 1.2):
            return again(f"Keep the line to about {catalog.max_words(preview.SECONDS)} words: it's an "
                         f"{preview.SECONDS}-second video.")
        check = compliance.check(line, product, brand_name, audience)
        if not check["ok"] or creator.CUSTOMER_VOICE in check["warnings"]:
            return again(" ".join(check["blocks"] or [creator.CUSTOMER_VOICE]))
    elif kind == "video":
        scene = " ".join((f.get("scene") or "").split())[:600] or catalog.fill(FIRST_VIDEO_SCENE, b)
        if "{product}" in scene or "{brand}" in scene:
            scene = catalog.fill(scene, b)
        check = compliance.check(scene, product, brand_name, audience)
        if not check["ok"]:
            return again(" ".join(check["blocks"]))
    else:
        if not tpl:
            return again("Pick a template.")
        brief = (f.get("brief") or "").strip()[:2000] or catalog.fill(tpl["brief"], b)
        if "{product}" in brief or "{brand}" in brief:
            brief = catalog.fill(brief, b)
        check = compliance.check(brief, audience, product, brand_name)
        if not check["ok"]:
            return again(" ".join(check["blocks"]))
    fs = request.files.get("photo")
    photo_id, path = None, None
    with db.connect() as conn:
        mine = {r["id"] for r in conn.execute("SELECT id FROM photos WHERE workspace_id=?", (g.ws["id"],))}
        if kind == "preview" and not preview.status(conn, g.ws, g.user["email"], g.user)["ok"]:
            return again("The free preview isn't available on this account. Make an image ad instead, or see "
                         "plans on Billing.")
    if fs and fs.filename:
        try:
            path, ctype = _save_upload(fs, os.path.join(UPLOADS, str(g.ws["id"])), PHOTO_TYPES, MAX_PHOTO_MB, ws=g.ws)
        except ValueError as e:
            return again(f"{fs.filename}: {e}")
    else:
        chosen = f.get("photo_id", type=int)
        if chosen and chosen in mine:
            photo_id = chosen
        else:
            return again("Add one photo of your product.")
    with db.connect() as conn:
        try:
            pid = products.save_kit(conn, g.ws, brand_name=brand_name, product_name=product, audience=audience,
                                    first_ad=True)
        except products.ProductError as e:
            return again(str(e))
        if path:
            photo_id = products.add_photo(conn, g.ws["id"], pid, path, fs.filename, ctype)
        elif pid:
            conn.execute("UPDATE photos SET product_id=COALESCE(product_id, ?) WHERE id=?", (pid, photo_id))
            conn.execute("UPDATE products SET primary_photo_id=COALESCE(primary_photo_id, ?) WHERE id=?", (photo_id, pid))
            conn.commit()
        if kind == "preview":
            cfg = {"type": "ugc", "product": pid, "angle": {"title": "First look"}, "items": [line], "body": "",
                   "presenters": [presenter], "duration": preview.SECONDS, "aspects": ["9:16"], "photos": [photo_id]}
            title = "Your free preview"
        elif kind == "video":
            cfg = {"type": "product_video", "product": pid, "angle": {"title": "First look"}, "items": [scene],
                   "duration": 5, "photos": [photo_id]}
            title = "Your free video ad"
        else:
            cfg = {"type": "image", "product": pid, "angle": {"title": tpl["name"]}, "items": [], "body": brief,
                   "aspects": [tpl.get("aspect") or "9:16"], "versions": n_images, "photos": [photo_id]}
            title = "Your first ads" if n_images > 1 else "Your first ad"
        bid = conn.execute("INSERT INTO batches (workspace_id, user_id, product_id, ad_type, title, config_json, credits, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (g.ws["id"], g.user["id"], pid, cfg["type"], title, json.dumps(cfg), 0, db.now())).lastrowid
        if kind == "preview":
            params = {"photo_ids": [photo_id], "ad_type": "ugc", "product": product, "duration": preview.SECONDS,
                      "aspect": "9:16", "presenter": presenter, "voice": create_routes._default_voice(presenter),
                      "resolution": preview.RESOLUTION, "preview": True, "label": "Free preview · 480p",
                      "hook": line[:200]}
            jid = _queue_job(conn, "ugc_video", None, line, params, preview.cost_usd(), 0, product_id=pid,
                             batch_id=bid)
            if jid and not preview.claim(conn, g.ws["id"], g.user["email"], client_ip(), jid, db.now()):
                conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
                conn.execute("DELETE FROM ledger WHERE job_id=?", (jid,))
                jid = None
            if jid:
                conn.execute("UPDATE ledger SET reason='free talking-video preview' WHERE job_id=?", (jid,))
            conn.commit()
        elif kind == "video":
            params = {"photo_ids": [photo_id], "duration": 5, "audio": False, "resolution": catalog.DEFAULT_RESOLUTION,
                      "label": "5s · your free video ad", "hook": scene[:200]}
            jid = _queue_job(conn, "photo_to_video", None, scene, params, catalog.cost_usd("photo_to_video", duration=5),
                             ctx["video_price"], product_id=pid, batch_id=bid)
            if jid:
                conn.execute("UPDATE batches SET credits=? WHERE id=?", (ctx["video_price"], bid))
                conn.commit()
        else:
            params = {"photo_ids": [photo_id], "batch": n_images, "aspect": tpl.get("aspect") or "9:16",
                      "audience": audience or tpl.get("audience") or "", "niche": "", "label": tpl["name"]}
            jid = _queue_job(conn, "ad_image", tpl["id"], brief, params, catalog.cost_usd("ad_image", batch=n_images),
                             img_price, product_id=pid, batch_id=bid)
            if jid:
                conn.execute("UPDATE batches SET credits=? WHERE id=?", (img_price, bid))
                conn.commit()
        if not jid:
            conn.execute("DELETE FROM batches WHERE id=?", (bid,))
            conn.commit()
            if g.get("verify_blocked"):
                return _studio_page(f, 403)
            return redirect(url_for("billing_page"))
    for w in check["warnings"]:
        flash("Heads up: " + w)
    # Back to Home, which shows the ads rendering (and, unconfirmed, the confirm-your-email box under them).
    return redirect(url_for("studio", made=bid))


LIB_TYPES = {"image": ("ad_image",), "video": ("photo_to_video", "motion_copy"), "talking": ("ugc_video",)}
LIB_STATUS = {"done": ("done",), "making": ("pending", "running"), "failed": ("failed",)}


@app.route("/app/library")
@login_required
def library():
    """Everything made, newest first. Filters are plain GET params, so a
    filtered view can be bookmarked: product or brand, type, status, review,
    batch."""
    import jobs_routes
    pid = request.args.get("product", type=int)
    bid = request.args.get("brand", type=int)
    batch_id = request.args.get("batch", type=int)
    filters = {k: request.args.get(k, "") for k in ("type", "status", "review")}
    where, args = ["j.workspace_id=?", "j.deleted_at IS NULL"], [g.ws["id"]]
    if pid:
        where.append("j.product_id=?")
        args.append(pid)
    elif bid:
        where.append("j.brand_id=?")
        args.append(bid)
    if batch_id:
        where.append("j.batch_id=?")
        args.append(batch_id)
    if filters["type"] in LIB_TYPES:
        tools_ = LIB_TYPES[filters["type"]]
        where.append(f"j.tool IN ({','.join('?' * len(tools_))})")
        args.extend(tools_)
    if filters["status"] in LIB_STATUS:
        st = LIB_STATUS[filters["status"]]
        where.append(f"j.status IN ({','.join('?' * len(st))})")
        args.extend(st)
    if filters["review"] in ("approved", "rejected"):
        where.append("EXISTS (SELECT 1 FROM job_reviews r WHERE r.job_id=j.id AND r.verdict=?)")
        args.append(filters["review"])
    elif filters["review"] == "unreviewed":
        where.append("j.status='done' AND NOT EXISTS (SELECT 1 FROM job_reviews r WHERE r.job_id=j.id)")
    with db.connect() as conn:
        jobs = conn.execute(f"SELECT j.* FROM jobs j WHERE {' AND '.join(where)} ORDER BY j.id DESC LIMIT 200",
                            args).fetchall()
        outs = _outputs(conn, [j["id"] for j in jobs])
        names = {p["id"]: p["name"] for p in products.products(conn, g.ws["id"])}
        tree = products.tree(conn, g.ws)
        verdicts = jobs_routes.verdicts(conn, [j["id"] for j in jobs])
        prices = {j["id"]: jobs_routes.price_now(conn, j)[0] for j in jobs if j["status"] == "failed"}
        eta = jobs_routes.eta_minutes(conn)

    def filter_url(key, val):
        q = {k: v for k, v in request.args.items() if k not in ("highlight", "pick")}
        if val:
            q[key] = val
        else:
            q.pop(key, None)
        return url_for("library", **q)

    chips = [("type", {"label": "Type", "items": [("", "All"), ("talking", "Talking videos"), ("video", "Product videos"),
                                                  ("image", "Image ads")]}),
             ("status", {"label": "Status", "items": [("", "Any"), ("done", "Ready"), ("making", "Making"),
                                                      ("failed", "Failed")]}),
             ("review", {"label": "Review", "items": [("", "Any"), ("approved", "Approved"), ("rejected", "Rejected"),
                                                      ("unreviewed", "Not reviewed")]})]
    return render_template("library.html", jobs=jobs, outs=outs, templates=catalog.TEMPLATE_BY_ID,
                           highlight=request.args.get("highlight", type=int), tree=tree, names=names,
                           f_product=pid, f_brand=bid if not pid else None, f_batch=batch_id, filters=filters,
                           chips=chips, filter_url=filter_url, verdicts=verdicts, prices=prices, eta=eta)


@app.route("/api/jobs")
@login_required
def api_jobs():
    ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.isdigit()][:50]
    if not ids:
        return jsonify({"jobs": []})
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id, status, error, finished_at FROM jobs WHERE workspace_id=? AND id IN ({','.join('?' * len(ids))})",
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
    if not o["url"]:
        abort(404)   # free previews are only ever served watermarked, from disk
    return redirect(o["url"])


@app.route("/photo/<int:pid>")
@login_required
def photo(pid):
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM photos WHERE id=? AND workspace_id=?", (pid, g.ws["id"])).fetchone()
    if not p or not os.path.exists(p["path"]):
        abort(404)
    return send_file(p["path"], conditional=True)


# ---------------------------------------------------------------- brands + products hub

@app.route("/app/brand", methods=["GET", "POST"])
@app.route("/app/products", methods=["GET"])
@login_required
def brand_kit():
    """The products hub: brands, their products, and the active one. POST is the
    original one-form brand kit (brand name, product, claims, audience), which
    writes the active brand and product."""
    if request.method == "POST":
        f = request.form
        with db.connect() as conn:
            try:
                products.save_kit(conn, g.ws, brand_name=f.get("brand_name") or "", product_name=f.get("product_name") or "",
                                  claims=f.get("claims") or "", audience=f.get("audience") or "")
            except products.ProductError as e:
                flash(str(e))
                return redirect(url_for("brand_kit"))
        flash("Saved.", "ok")
        return redirect(url_for("brand_kit"))
    with db.connect() as conn:
        tree = products.tree(conn, g.ws)
        counts = products.counts(conn, g.ws["id"])
        loose = conn.execute("SELECT COUNT(*) FROM photos WHERE workspace_id=? AND product_id IS NULL",
                             (g.ws["id"],)).fetchone()[0]
    n = len(tree)
    return render_template("products.html", tree=tree, counts=counts, loose=loose, owner=is_owner(),
                           can_add=plans.within(g.ws, "brands", n), next_plan=plans.next_plan_for("brands", n))


@app.route("/app/brands", methods=["POST"])
@login_required
def add_brand():
    """A new, empty brand that becomes the active one (plan limit enforced here)."""
    with db.connect() as conn:
        try:
            bid = products.create_brand(conn, g.ws, request.form.get("brand_name") or "")
            products.activate(conn, g.ws, brand_id=bid)
        except products.ProductError as e:
            flash(str(e))
            return redirect(url_for("brand_kit"))
        name = products.get_brand(conn, g.ws["id"], bid)["brand_name"]
    flash(f"Added {name}. It's your active brand now. Add its first product next.")
    return redirect(url_for("products.new", brand=bid))


@app.route("/app/brands/<int:bid>/use", methods=["POST"])
@login_required
def use_brand(bid):
    with db.connect() as conn:
        try:
            products.activate(conn, g.ws, brand_id=bid)
        except products.ProductError as e:
            if str(e) == "not found":
                abort(404)
            flash(str(e))
    return redirect(_back(url_for("brand_kit")))


def _back(default):
    nxt = request.form.get("back") or request.args.get("back") or ""
    return nxt if nxt.startswith("/app") and not nxt.startswith("//") else default


@app.route("/app/brands/<int:bid>/delete", methods=["POST"])
@owner_required
def delete_brand(bid):
    with db.connect() as conn:
        ids = [r["id"] for r in products.brands(conn, g.ws["id"])]
        if bid not in ids:
            abort(404)
        if len(ids) == 1:
            flash("That's your only brand. Edit it instead.")
            return redirect(url_for("brand_kit"))
        conn.execute("UPDATE products SET archived=1 WHERE brand_id=? AND workspace_id=?", (bid, g.ws["id"]))
        conn.execute("DELETE FROM brands WHERE id=?", (bid,))
        conn.commit()
        if g.ws["brand_id"] == bid:
            products.activate(conn, g.ws, brand_id=next(i for i in ids if i != bid))
    flash("Brand removed. Ads made for it stay in your library.")
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
    """Add photos to a product (the form's `product`, else the active one)."""
    pid = request.form.get("product", type=int) or g.ws["product_id"]
    n = 0
    with db.connect() as conn:
        if pid and not products.get_product(conn, g.ws["id"], pid):
            abort(404)
        for fs in request.files.getlist("photos")[:20]:
            if not fs or not fs.filename:
                continue
            try:
                path, ctype = _save_upload(fs, os.path.join(UPLOADS, str(g.ws["id"])), PHOTO_TYPES, MAX_PHOTO_MB,
                                           ws=g.ws)
            except ValueError as e:
                flash(f"{fs.filename}: {e}")
                continue
            products.add_photo(conn, g.ws["id"], pid, path, fs.filename, ctype)
            n += 1
    if n:
        flash(f"Added {n} photo{'s' if n != 1 else ''}.", "ok")
    return redirect(_back(url_for("products.edit", pid=pid) if pid else url_for("brand_kit")))


@app.route("/app/photos/<int:pid>/delete", methods=["POST"])
@login_required
def delete_photo(pid):
    with db.connect() as conn:
        p = products.remove_photo(conn, g.ws["id"], pid)
    if p:
        try:
            os.remove(p["path"])
        except OSError:
            pass
    return redirect(_back(url_for("brand_kit")))


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
    if "success" in (request.args.get("checkout"), request.args.get("plan")):
        _report_purchase((request.args.get("session_id") or "").strip()[:200], request.args.get("plan") == "success")
    if request.args.get("plan") == "success":
        session.pop("promo", None)
    if request.args.get("code") and is_owner() and not _subscribed(g.ws):
        _save_promo(request.args.get("code"), from_link=True)
    try:
        if _maybe_reconcile(g.ws["id"], force=request.args.get("plan") == "success"):
            with db.connect() as conn:
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("plan sync on billing page failed")
    with db.connect() as conn:
        purchases = conn.execute("SELECT * FROM purchases WHERE workspace_id=? AND status IN ('paid','open') "
                                 "ORDER BY id DESC LIMIT 50", (g.ws["id"],)).fetchall()
        # The job's tool and template, so "generation" rows can say what was made.
        ledger = conn.execute("SELECT l.*, j.tool job_tool, j.template_id job_template FROM ledger l "
                              "LEFT JOIN jobs j ON j.id=l.job_id AND j.workspace_id=l.workspace_id "
                              "WHERE l.workspace_id=? ORDER BY l.id DESC LIMIT 30", (g.ws["id"],)).fetchall()
        invoices = conn.execute("SELECT * FROM plan_invoices WHERE workspace_id=? ORDER BY created_at DESC LIMIT 24",
                                (g.ws["id"],)).fetchall()
        used_code = compcodes.used_code(conn, g.ws["id"])
    return render_template("billing.html", templates_by_id=catalog.TEMPLATE_BY_ID,
                           purchases=purchases, ledger=ledger, packs_by=catalog.PACK_BY_CODE,
                           invoices=invoices, current=plans.plan(g.ws), subscribed=_subscribed(g.ws),
                           owner=is_owner(), comp_on=compcodes.active(g.ws), used_code=used_code,
                           promo=session.get("promo"))


def _save_promo(raw, from_link=False) -> bool:
    """Remember a discount code from our email (billing.find_promotion_code) for this
    browser's next plan checkout. Says what it gives, or why it can't be used."""
    code = billing.normalize_promo(raw)
    if not code:
        if from_link:
            flash("That code doesn't look right. You can still enter a code at checkout.")
        return False
    try:
        promo = billing.find_promotion_code(code)
    except billing.BillingError as e:
        log.warning("promotion code lookup failed: %s", e)
        flash(f"We couldn't check {code} just now. Pick a plan below and enter it at checkout.")
        return True
    if not promo:
        if from_link:
            flash(f"{code} isn't valid or has expired. You can still pick a plan below.")
        return False
    session["promo"] = {"id": promo["id"], "code": promo["code"], "label": promo["label"]}
    flash(f"{promo['code']} saved: {promo['label']}. It comes off at checkout when you pick a plan below.", "ok")
    return True


@app.route("/app/billing/code", methods=["POST"])
@owner_required
def billing_code():
    """"Have a code?" on Billing: a free-month code (compcodes.py, no Stripe involved),
    or a discount code from our email (a Stripe promotion code, kept for checkout)."""
    raw = request.form.get("code") or ""
    if not compcodes.normalize(raw) and not _subscribed(g.ws) and billing.normalize_promo(raw) \
            and _save_promo(raw):
        return redirect(url_for("billing_page") + "#plans")
    with db.connect() as conn:
        try:
            row = compcodes.redeem(conn, g.ws["id"], g.user["id"], request.form.get("code") or "")
        except compcodes.Refused as e:
            conn.rollback()
            flash(str(e))
            return redirect(url_for("billing_page") + "#code")
    flash(compcodes.applied_message(row), "ok")
    return redirect(url_for("billing_page"))


def _report_purchase(sid: str, plan: bool):
    """Google Ads (and pixel) purchase conversion for the Checkout Session Stripe just returned
    from: only a session this workspace started (ad_conversions), once. A plan's value
    is what Stripe charged (promotion codes included) when we can read the session,
    else the plan's price recorded at checkout."""
    if not sid.startswith("cs_") or not gads.wanted("purchase"):
        return
    cents = currency = None
    if plan:
        try:
            cs = billing.get_checkout_session(sid)
            meta = cs.get("metadata") or {}
            if meta.get("app") != billing.APP_TAG or str(meta.get("workspace_id")) != str(g.ws["id"]):
                return
            if cs.get("status") not in (None, "complete") and cs.get("payment_status") not in ("paid", "no_payment_required"):
                return
            if isinstance(cs.get("amount_total"), int):
                cents, currency = cs["amount_total"], cs.get("currency")
        except Exception:  # noqa: BLE001 — never break the billing page over a conversion
            log.info("couldn't read checkout session %s for the conversion; using the recorded price", sid)
    with db.connect() as conn:
        ev = gads.claim_purchase(conn, sid, g.ws["id"], db.now(), value_cents=cents, currency=currency)
    if ev:
        gads.queue(session, "purchase", **ev)


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
    if g.ws["plan_status"] == "pending":
        flash("Your last checkout is still confirming its payment. Give it a minute, then refresh this page.")
        return redirect(url_for("billing_page"))
    base = request.host_url.rstrip("/")
    promo_id = None
    if session.get("promo"):
        # checked again: the code may have expired or been used up since it was saved
        try:
            fresh = billing.find_promotion_code(session["promo"].get("code"))
        except billing.BillingError as e:
            log.warning("promotion code re-check failed: %s", e)
            fresh = None
        if fresh:
            promo_id = fresh["id"]
        else:
            flash(f"{session['promo'].get('code')} couldn't be applied. You can enter a code on the checkout page.")
            session.pop("promo", None)
    try:
        sess = billing.create_subscription_session(
            promotion_code=promo_id,
            price_id=p["price_id"],
            success_url=f"{base}{url_for('billing_page')}?plan=success&session_id={{CHECKOUT_SESSION_ID}}",
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
    with db.connect() as conn:
        gads.record_checkout(conn, sess["id"], g.ws["id"], g.user["id"], p["usd"] * 100, "usd", db.now(),
                            conversions.context(request))
        conn.commit()
        site_events.on_workspace_step(conn, "checkout", g.ws["id"], request, detail=f"plan:{p['code']}")
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
            success_url=f"{base}{url_for('billing_page')}?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
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
        gads.record_checkout(conn, sess["id"], g.ws["id"], g.user["id"], pack["usd"] * 100, "usd", db.now(),
                            conversions.context(request))
        conn.commit()
        site_events.on_workspace_step(conn, "checkout", g.ws["id"], request, detail=f"pack:{pack['code']}")
    return redirect(sess["url"], code=303)


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Credit packs (checkout.session.completed, mode=payment) and plans
    (checkout.session.completed mode=subscription, invoice.paid,
    customer.subscription.updated/deleted), and refunds and disputes
    (charge.refunded, charge.dispute.created/closed: billing_holds.py). The
    Stripe account is shared, so anything that isn't ours is ignored."""
    payload = request.get_data()
    try:
        event = billing.verify_event(payload, request.headers.get("Stripe-Signature", ""))
    except billing.SignatureError as e:
        log.warning("stripe webhook rejected: %s", e)
        return jsonify({"error": "bad signature"}), 400
    typ = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    # Once per Stripe event id: a retry or replay of an event we already acted
    # on is answered without running it again. The claim is let go when the
    # event did nothing (ignored, pending) or failed, so Stripe's retry runs it.
    eid = event.get("id") if isinstance(event.get("id"), str) else None
    if eid and not _claim_stripe_event(eid, typ):
        return jsonify({"ok": True, "duplicate": True})
    keep = False
    try:
        resp = app.make_response(_stripe_event(typ, obj))
        out = resp.get_json(silent=True) or {}
        keep = resp.status_code < 400 and not out.get("ignored") and not out.get("pending")
        return resp
    finally:
        if eid and not keep:
            _release_stripe_event(eid)


def _claim_stripe_event(eid, typ) -> bool:
    with db.connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO stripe_events (id, type, received_at) VALUES (?,?,?)",
                           (eid, typ, db.now()))
        conn.commit()
    return cur.rowcount == 1


def _release_stripe_event(eid):
    with db.connect() as conn:
        conn.execute("DELETE FROM stripe_events WHERE id=?", (eid,))
        conn.commit()


def _stripe_event(typ, obj):
    if typ == "charge.refunded":
        return jsonify(billing_holds.charge_refunded(obj))
    if typ == "charge.dispute.created":
        return jsonify(billing_holds.dispute_created(obj))
    if typ == "charge.dispute.closed":
        return jsonify(billing_holds.dispute_closed(obj))
    if typ in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("mode") == "subscription":
            return _plan_checkout_completed(obj)
        meta = obj.get("metadata") or {}
        if meta.get("app") == billing.APP_TAG and meta.get("kind") == "creator_pool":
            with db.connect() as conn:
                out = creator_pools.paid(conn, obj)
            return jsonify(out), (400 if out.get("error") else 200)
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
        # a refund or dispute that beat this event here (billing_holds.py)
        billing_holds.apply_parked(conn, sess.get("payment_intent"))
        conn.commit()
        _server_purchase(conn, sess, p["workspace_id"])
        # the funnel's paid step: this purchase only just turned paid, so once per pack
        site_events.on_workspace_step(conn, "paid", p["workspace_id"], detail=f"pack:{p['pack']}", path="")
    return jsonify({"ok": True, "credited": p["credits"]})


def _server_purchase(conn, sess, workspace_id):
    """Server-side Purchase for Meta / TikTok / Reddit (conversions.py), same event id as the pixels."""
    try:
        conversions.on_checkout_paid(conn, sess, workspace_id)
    except Exception:  # noqa: BLE001 — never fail the webhook over measurement
        log.exception("server-side purchase conversion not queued for %s", sess.get("id"))


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
        if plans.pending_on():
            # BILL-3: the plan switches on when its first invoice is paid (invoice.paid,
            # or the subscription turning active), never at checkout. Until then the
            # billing page says "Confirming payment". Already live (invoice.paid came
            # first): left as it is.
            conn.execute("UPDATE workspaces SET stripe_customer_id=COALESCE(?, stripe_customer_id), "
                         "stripe_subscription_id=COALESCE(?, stripe_subscription_id), "
                         "plan_pending=CASE WHEN plan_status IN ('active','trialing','past_due') THEN NULL ELSE ? END, "
                         "plan_pending_at=CASE WHEN plan_status IN ('active','trialing','past_due') THEN NULL ELSE ? END, "
                         "plan_status=CASE WHEN plan_status IN ('active','trialing','past_due') THEN plan_status "
                         "ELSE 'pending' END WHERE id=?",
                         (customer, sub_id, code, db.now(), ws["id"]))
        else:
            conn.execute("UPDATE workspaces SET stripe_customer_id=COALESCE(?, stripe_customer_id), "
                         "stripe_subscription_id=COALESCE(?, stripe_subscription_id), plan=?, "
                         "plan_status=COALESCE(plan_status, 'active') WHERE id=?",
                         (customer, sub_id, code, ws["id"]))
        conn.commit()
        _server_purchase(conn, sess, ws["id"])
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
        if sub_id and ws["stripe_subscription_id"] == sub_id and ws["plan_status"] == "canceled":
            # Canceled is final in Stripe: a late or replayed invoice.paid for that
            # subscription must not switch the plan (and its credits) back on.
            log.warning("stripe webhook: invoice %s paid for canceled subscription %s (workspace %s); not applied",
                        inv.get("id"), sub_id, ws["id"])
            return jsonify({"ignored": "subscription already canceled"})
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
        pi = billing.invoice_payment_intent(inv)
        conn.execute("INSERT INTO plan_invoices (invoice_id, workspace_id, plan, credits, amount_cents, billing_reason, "
                     "created_at, payment_intent) VALUES (?,?,?,?,?,?,?,?)",
                     (inv.get("id"), ws["id"], p["code"], grant, int(inv.get("amount_paid") or 0), reason, db.now(), pi))
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active', plan_granted=?, "
                     "plan_period_end=COALESCE(?, plan_period_end), plan_paid_through=COALESCE(?, plan_paid_through), "
                     "plan_pending=NULL, plan_pending_at=NULL, stripe_subscription_id=COALESCE(?, stripe_subscription_id), "
                     "stripe_customer_id=COALESCE(?, stripe_customer_id) WHERE id=?",
                     (p["code"], granted, _iso(period_end), _iso(period_end), sub_id, customer, ws["id"]))
        billing_holds.apply_parked(conn, pi)
        conn.commit()
        if reason == "subscription_create":
            # the funnel's paid step: a plan's first invoice (this invoice id only gets here once)
            site_events.on_workspace_step(conn, "paid", ws["id"], detail=f"plan:{p['code']}", path="")
    return jsonify({"ok": True, "credited": grant, "plan": p["code"]})


def _plan_not_billed(conn, ws, status):
    """unpaid, paused, canceled, incomplete_expired. Nothing paid for is taken
    back early (observation 8):
      - a period that was paid for still runs: the plan and its credits stay to
        its end (plans.GRACE_STATUSES); then settle_plan_lapse drops it;
      - unpaid / paused after it (the renewal failed, Stripe keeps the
        subscription): paused at once, plan credits kept but not spendable
        (plans.renewal_paused), until it's paid or canceled;
      - canceled / incomplete_expired after it: the plan ends now and its unused
        plan credits expire (they belonged to the period that's over)."""
    ws_id = ws["id"]
    conn.execute("UPDATE workspaces SET plan_status=?, plan_pending=NULL, plan_pending_at=NULL"
                 + (", plan_cancel_at_period_end=0" if status == "canceled" else "") + " WHERE id=?", (status, ws_id))
    fresh = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if plans.paid_period_left(fresh):
        conn.commit()
        return jsonify({"ok": True, "plan": plans.plan_code(fresh), "status": status,
                        "until": fresh["plan_paid_through"]})
    if status in plans.RENEWAL_FAILED and plans.renewal_pause_on():
        conn.commit()
        return jsonify({"ok": True, "plan": plans.plan_code(fresh), "status": status, "paused": True})
    if status in plans.RENEWAL_FAILED:      # RENEWAL_PAUSE=off: as before, the plan ends now
        db.set_plan_credits(conn, ws_id, 0, "plan ended")
        conn.execute("UPDATE workspaces SET plan='free', plan_granted=0, plan_cancel_at_period_end=0, "
                     "plan_period_end=NULL WHERE id=?", (ws_id,))
    conn.commit()
    core.settle_plan_lapse(conn, ws_id)     # canceled / incomplete_expired (keeps a free-month code's credits)
    return jsonify({"ok": True, "plan": "free", "status": status})


def _plan_subscription_changed(sub, deleted=False):
    meta = sub.get("metadata") or {}
    sub_id = sub.get("id")
    with db.connect() as conn:
        ws = _our_workspace(conn, meta, sub_id)
        if not ws:
            return jsonify({"ignored": "not a campaignify subscription"})
        if ws["stripe_subscription_id"] and sub_id and ws["stripe_subscription_id"] != sub_id \
                and ws["plan_status"] in plans.LIVE_STATUSES | {"pending"}:
            # An older subscription of this workspace ending must not cancel the current
            # one (or the checkout that's being confirmed).
            return jsonify({"ignored": "not the workspace's current subscription"})
        status = "canceled" if deleted else (sub.get("status") or "active")
        if status == "incomplete":
            # First payment still in progress; invoice.paid switches the plan on.
            return jsonify({"ignored": "subscription incomplete"})
        if status not in plans.LIVE_STATUSES:
            return _plan_not_billed(conn, ws, status)
        ours = [plans.BY_PRICE[pid] for pid in billing.subscription_prices(sub) if pid in plans.BY_PRICE]
        code = ours[0]["code"] if ours else ws["plan"]
        customer = sub.get("customer") if isinstance(sub.get("customer"), str) else (sub.get("customer") or {}).get("id")
        conn.execute("UPDATE workspaces SET plan=?, plan_status=?, plan_cancel_at_period_end=?, "
                     "plan_period_end=COALESCE(?, plan_period_end), stripe_subscription_id=?, "
                     "plan_pending=NULL, plan_pending_at=NULL, "
                     "stripe_customer_id=COALESCE(?, stripe_customer_id) WHERE id=?",
                     (code, status, 1 if (sub.get("cancel_at_period_end") or sub.get("cancel_at")) else 0,
                      _iso(billing.subscription_period_end(sub)), sub_id, customer, ws["id"]))
        conn.commit()
    return jsonify({"ok": True, "plan": code, "status": status})


# ---------------------------------------------------------------- plan sync
# Webhooks are the fast path; this is the safety net. It reads the workspace's
# plan subscription and newest paid invoice straight from Stripe and applies
# whatever the webhook missed. Invoices apply once (plan_invoices), so running
# it again is harmless.

_last_sync: dict = {}


def _reconcile_plan(ws_id: int) -> int:
    """Returns the credits granted by this sync (0 when nothing was missing)."""
    if not billing.configured():
        return 0
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if not ws:
        return 0
    try:
        subs = [billing.get_subscription(ws["stripe_subscription_id"])] if ws["stripe_subscription_id"] \
            else billing.find_plan_subscriptions(ws_id)
    except billing.BillingError as e:
        log.warning("plan sync for workspace %s: %s", ws_id, e)
        return 0
    granted = 0
    for sub in sorted(subs, key=lambda x: x.get("created") or 0):
        meta = sub.get("metadata") or {}
        if meta.get("app") != billing.APP_TAG or str(meta.get("workspace_id")) != str(ws_id):
            continue
        status = sub.get("status")
        if status in plans.LIVE_STATUSES:
            try:
                inv = billing.latest_paid_invoice(sub["id"])
            except billing.BillingError as e:
                log.warning("plan sync for workspace %s: %s", ws_id, e)
                inv = None
            if inv:
                out = _plan_invoice_paid(inv).get_json() or {}
                granted += int(out.get("credited") or 0)
        _plan_subscription_changed(sub, deleted=status == "canceled")
    if granted:
        log.info("plan sync for workspace %s granted %s credits", ws_id, granted)
    return granted


def _maybe_reconcile(ws_id: int, force: bool = False) -> int:
    now = time.time()
    if not force and now - _last_sync.get(ws_id, 0) < 60:
        return 0
    _last_sync[ws_id] = now
    return _reconcile_plan(ws_id)


PENDING_HOURS = 24


def _reconcile_all():
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM workspaces WHERE stripe_subscription_id IS NOT NULL AND stripe_subscription_id != ''")]
    for ws_id in ids:
        try:
            _reconcile_plan(ws_id)
        except Exception:  # noqa: BLE001
            log.exception("plan sync failed for workspace %s", ws_id)
    _settle_plans()


def _settle_plans():
    """Plans whose paid period ended after a cancel drop to free, and a checkout
    still "Confirming payment" after a day (Stripe never confirmed it) goes back
    to what the workspace had."""
    from datetime import datetime, timedelta, timezone
    stale = (datetime.now(timezone.utc) - timedelta(hours=PENDING_HOURS)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        for r in conn.execute("SELECT id FROM workspaces WHERE plan_status IN ('canceled','incomplete_expired') "
                              "AND plan != 'free'").fetchall():
            core.settle_plan_lapse(conn, r["id"])
        n = conn.execute("UPDATE workspaces SET plan_status='incomplete_expired', plan_pending=NULL, plan_pending_at=NULL "
                         "WHERE plan_status='pending' AND plan_pending_at < ?", (stale,)).rowcount
        conn.commit()
    if n:
        log.warning("plan sync: %s checkout(s) never confirmed after %s h; set back", n, PENDING_HOURS)


def _start_plan_sync():
    if os.environ.get("RS_PLAN_SYNC", "1") != "1":
        return

    def loop():
        while True:
            time.sleep(3600)
            with app.app_context():
                _reconcile_all()
                try:
                    with db.connect() as conn:
                        compcodes.settle_all(conn)
                except Exception:  # noqa: BLE001
                    log.exception("free-month code sweep failed")

    threading.Thread(target=loop, name="rs-plan-sync", daemon=True).start()


_start_plan_sync()
nudges.start(app)


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
        row = conn.execute("SELECT value FROM meta WHERE key='vendor_prices'").fetchone()
        stills = presenter_stills.state(conn)
        if any(j.get("status") == "running" for j in stills["jobs"].values()) and hf.configured():
            presenter_stills.collect(conn)
            stills = presenter_stills.state(conn)
        sys_health = admin_funnel.health(conn)
        funnel = admin_funnel.signups(conn, 30)["totals"]
        holds = billing_holds.holds(conn)
    vendor = json.loads(row["value"]) if row else None
    return render_template("admin.html", wss=wss, invites=invites, refs=refs, jobs=jobs, purchases=purchases,
                           billing_on=billing.configured(), vendor=vendor,
                           markup=catalog.MARKUP, hd_markup=catalog.HD_MARKUP, configured=hf.configured(),
                           base=request.host_url, stills=stills, presenters=presenter_stills.with_photos(url_for),
                           stills_cap=presenter_stills.CAP_USD,
                           ai_status=llm.status(), health=sys_health, funnel=funnel, holds=holds,
                           holds_on=billing_holds.enabled())


@app.route("/admin/workspaces/<int:wid>/view", methods=["POST"])
@admin_required
def admin_view_workspace(wid):
    """See another workspace as its members do (Library, products, campaigns...),
    read-only, until Back to my workspace."""
    with db.connect() as conn:
        ws = conn.execute("SELECT id, name FROM workspaces WHERE id=?", (wid,)).fetchone()
    if not ws:
        abort(404)
    if ws["id"] == g.user["workspace_id"]:
        session.pop("view_ws", None)
    else:
        session["view_ws"] = ws["id"]
    return redirect(url_for("library"))


@app.route("/admin/view/exit", methods=["POST"])
@login_required
def admin_view_exit():
    session.pop("view_ws", None)
    return redirect(url_for("admin") if g.user["is_admin"] else url_for("studio"))


@app.route("/admin/signups")
@admin_required
def admin_signups():
    """Sign-ups in the last 30 days (emails masked): where they came from, the
    starter credits they got (the ad offer included) and whether they made anything."""
    with db.connect() as conn:
        data = admin_funnel.signups(conn, 30)
    return render_template("admin_signups.html", data=data, ad_credits=adoffer.credits(),
                           starter=catalog.starter_credits())


@app.route("/admin/health-check", methods=["POST"])
@admin_required
def admin_health_check():
    """Ask the generation provider now whether our key is accepted (a free call)."""
    flash(f"Generation provider: {hf.probe(max_age=0)}.", "ok")
    return redirect(url_for("admin") + "#health")


@app.route("/admin/presenters", methods=["POST"])
@admin_required
def admin_presenter_stills():
    """Make the presenter sample stills (once, capped). See presenter_stills.py."""
    if not hf.configured():
        flash("The generation service isn't connected (HF_KEY).")
        return redirect(url_for("admin") + "#presenters")
    with db.connect() as conn:
        try:
            msg = presenter_stills.start(conn) if request.form.get("do") == "start" else \
                f"Saved {presenter_stills.collect(conn)} finished still(s)."
        except hf.GenError as e:
            msg = f"The vendor refused: {e}"
    flash(msg, "ok")
    return redirect(url_for("admin") + "#presenters")


@app.route("/presenter/<pid>.jpg")
@login_required
def presenter_photo(pid):
    p = presenter_stills.path_for(pid) if pid in catalog.PRESENTER_BY_ID else None
    if not p:
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=86400)


@app.route("/admin/prices", methods=["POST"])
@admin_required
def admin_prices():
    """Ask the generation vendor what each format costs right now (free
    estimates) and compare with what Campaignify charges."""
    img = url_for("static", filename="previews/ad_image.jpg", _external=True)
    samples = [("ad_image", {"batch": 1}, {"prompt": "A product on a table", "image_urls": [img], "batch_size": 1,
                                           "aspect_ratio": "9:16", "language": "English", "niche": "", "target_audience": ""}),
               ("photo_to_video", {"duration": 5}, {"prompt": "Slow pan", "image_url": img, "duration": 5,
                                                    "resolution": "720p", "bitrate_mode": "high", "generate_audio": False}),
               ("ugc_video", {"duration": 10}, {"prompt": "A presenter holds the product and says hello.",
                                                "image_urls": [img], "duration": 10, "aspect_ratio": "9:16",
                                                "resolution": "720p", "bitrate_mode": "high", "generate_audio": True}),
               ("photo_to_video", {"duration": 5, "resolution": "1080p"},
                {"prompt": "Slow pan", "image_url": img, "duration": 5, "resolution": "1080p", "bitrate_mode": "high",
                 "generate_audio": False}),
               ("ugc_video", {"duration": 10, "resolution": "1080p"},
                {"prompt": "A presenter holds the product and says hello.", "image_urls": [img], "duration": 10,
                 "aspect_ratio": "9:16", "resolution": "1080p", "bitrate_mode": "high", "generate_audio": True})]
    if catalog.upscale_on():
        samples.append(("video_upscale", {"duration": 5},
                        {"video_url": url_for("static", filename="site/ugc-coffee.mp4", _external=True)}))
    rows = []
    for tool, kw, inputs in samples:
        ours = catalog.cost_usd(tool, **kw)
        vendor, why = hf.estimate_detail(catalog.TOOLS[tool]["model"], inputs)
        if vendor is None:
            log.warning("vendor price check: no readable estimate for %s (%s)", tool, why)
        rows.append({"tool": tool, "name": catalog.TOOLS[tool]["name"], "setting": ", ".join(f"{k} {v}" for k, v in kw.items()),
                     "ours": round(ours, 4), "vendor": vendor, "why": why, "model": catalog.TOOLS[tool]["model"],
                     "how": hf.last_estimate.get("how"), "sample": (hf.last_estimate.get("sample") or "")[:300],
                     "credits": catalog.price_credits(tool, **kw)})
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('vendor_prices', ?)",
                     (json.dumps({"at": db.now(), "rows": rows}),))
        conn.commit()
    got = sum(r["vendor"] is not None for r in rows)
    flash(f"Vendor prices checked: {got} of {len(rows)} formats priced."
          + ("" if got else " Generation isn't blocked: talking videos start at our table price."), "ok")
    return redirect(url_for("admin") + "#prices")


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
    flash("Plan updated.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/workspaces/<int:wid>/lift-hold", methods=["POST"])
@admin_required
def admin_lift_hold(wid):
    """Lift a refund or dispute hold by hand. Voided credits stay voided (top up
    below if they should come back)."""
    with db.connect() as conn:
        billing_holds.lift(conn, wid)
        conn.commit()
    flash(f"Hold lifted for workspace #{wid}.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/credits", methods=["POST"])
@admin_required
def admin_credits():
    try:
        ws_id, amount = int(request.form["workspace_id"]), int(request.form["amount"])
    except (KeyError, ValueError):
        abort(400)
    with db.connect() as conn:
        if not db.adjust_credits(conn, ws_id, amount, (request.form.get("reason") or "admin top-up")[:100], hold_ok=True):
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
    flash("Reference added.", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/refs/<int:rid>/toggle", methods=["POST"])
@admin_required
def admin_ref_toggle(rid):
    with db.connect() as conn:
        conn.execute("UPDATE refs SET active = 1 - active WHERE id=?", (rid,))
        conn.commit()
    return redirect(url_for("admin"))


@app.route("/admin/outreach", methods=["GET", "POST"])
@admin_required
def admin_outreach():
    """One-to-one outreach emails to small brands (outreach.py has the rules)."""
    import mailer
    import outreach
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k) or "" for k in ("to", "brand", "subject", "body")}
        form["follow_up"] = bool(request.form.get("follow_up"))
        test = request.form.get("do") == "test"
        with db.connect() as conn:
            try:
                row = outreach.send(conn, to=form["to"], brand=form["brand"], subject=form["subject"],
                                    body=form["body"], follow_up=form["follow_up"],
                                    test_to=g.user["email"] if test else None, user_id=g.user["id"])
            except outreach.Refused as e:
                flash(str(e))
                row = None
        if row is not None:
            if row["status"] == "sent":
                flash(f"Test sent to {row['to_addr']}." if test else f"Sent to {row['to_addr']}.", "ok")
                if not test:
                    return redirect(url_for("admin_outreach"))
            else:
                flash(f"The email service refused it: {row['error']}")
    with db.connect() as conn:
        log, sup, today = outreach.recent(conn), outreach.suppressions(conn), outreach.sent_last_day(conn)
    return render_template("admin_outreach.html", form=form, log=log, sup=sup, today=today, cap=outreach.DAILY_CAP,
                           repeat_days=outreach.REPEAT_DAYS, frm=outreach.from_header(), reply_to=outreach.reply_to(),
                           address=outreach.postal_address(), opt_out=outreach.OPT_OUT_LINE,
                           mail_on=mailer.configured())


@app.route("/admin/codes", methods=["GET", "POST"])
@admin_required
def admin_codes():
    """Single-use free-month codes (compcodes.py): make a batch, see every code's status, revoke."""
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k) or "" for k in ("plan", "months", "labels", "count", "redeem_by")}
        with db.connect() as conn:
            try:
                batch, rows = compcodes.generate(conn, plan=form["plan"] or "growth", months=form["months"] or 1,
                                                 labels=form["labels"].splitlines(), count=form["count"],
                                                 redeem_by=form["redeem_by"], user_id=g.user["id"])
            except compcodes.Refused as e:
                flash(str(e))
                batch = None
        if batch:
            flash(f"Made {len(rows)} code{'s' if len(rows) != 1 else ''}.", "ok")
            return redirect(url_for("admin_codes", batch=batch))
    batch = (request.args.get("batch") or "").strip()[:40]
    with db.connect() as conn:
        rows = compcodes.listing(conn)
        fresh = conn.execute("SELECT * FROM comp_codes WHERE batch=? ORDER BY id", (batch,)).fetchall() if batch else []
    return render_template("admin_codes.html", rows=rows, fresh=fresh, batch=batch, form=form, status=compcodes.status,
                           now=db.now(), default_days=compcodes.DEFAULT_REDEEM_DAYS, max_months=compcodes.MAX_MONTHS,
                           max_codes=compcodes.MAX_CODES)


@app.route("/admin/codes.csv")
@admin_required
def admin_codes_csv():
    batch = (request.args.get("batch") or "").strip()[:40]
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM comp_codes WHERE batch=? ORDER BY id", (batch,)).fetchall() if batch \
            else conn.execute("SELECT * FROM comp_codes ORDER BY id").fetchall()
    name = f"campaignify-codes-{batch or 'all'}.csv"
    return app.response_class(compcodes.to_csv(rows), mimetype="text/csv",
                              headers={"Content-Disposition": f'attachment; filename="{name}"',
                                       "Cache-Control": "no-store"})


@app.route("/admin/codes/<int:cid>/revoke", methods=["POST"])
@admin_required
def admin_code_revoke(cid):
    with db.connect() as conn:
        ok = compcodes.revoke(conn, cid, g.user["id"])
    flash("Code revoked. It won't work any more." if ok else "Only unused codes can be revoked.", "ok" if ok else "message")
    return redirect(url_for("admin_codes") + "#codes")


@app.route("/admin/outreach/suppress", methods=["POST"])
@admin_required
def admin_outreach_suppress():
    import outreach
    with db.connect() as conn:
        try:
            entry = outreach.suppress(conn, request.form.get("entry"), request.form.get("note"), g.user["id"])
            flash(f"{entry} is on the do-not-contact list.", "ok")
        except outreach.Refused as e:
            flash(str(e))
    return redirect(url_for("admin_outreach") + "#dnc")


@app.route("/ref/<int:rid>")
@login_required
def ref_video(rid):
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
    if not r or not os.path.exists(r["path"]):
        abort(404)
    return send_file(r["path"], conditional=True)


import product_routes  # noqa: E402
import create_routes  # noqa: E402
import jobs_routes  # noqa: E402
import flows_routes  # noqa: E402
import messaging  # noqa: E402
import messaging_routes  # noqa: E402
import sms  # noqa: E402
import sms_routes  # noqa: E402
import assistant_routes  # noqa: E402
import campaign_routes  # noqa: E402
import copy_routes  # noqa: E402  (blueprints import core, never app)
import seo_routes  # noqa: E402
import competitor_routes  # noqa: E402
import adhub_routes  # noqa: E402
import pages_routes  # noqa: E402
import forms_routes  # noqa: E402
import community_routes  # noqa: E402
import share_routes  # noqa: E402
import creator_pool_routes  # noqa: E402
import creator_pools  # noqa: E402
import google_auth  # noqa: E402

app.register_blueprint(product_routes.bp)
app.register_blueprint(google_auth.bp)
app.jinja_env.globals["google_signin"] = google_auth.configured   # the Continue with Google button
app.register_blueprint(create_routes.bp)
app.register_blueprint(jobs_routes.bp)
app.register_blueprint(flows_routes.bp)
app.register_blueprint(messaging_routes.bp)
app.register_blueprint(sms_routes.bp)
messaging.register()
sms.register_hooks()
app.register_blueprint(assistant_routes.bp)
app.register_blueprint(copy_routes.bp)
app.register_blueprint(campaign_routes.bp)
app.register_blueprint(seo_routes.bp)
app.register_blueprint(competitor_routes.bp)
app.register_blueprint(adhub_routes.bp)
app.register_blueprint(pages_routes.bp)
app.register_blueprint(forms_routes.bp)
app.register_blueprint(community_routes.bp)
app.register_blueprint(share_routes.bp)
app.register_blueprint(creator_pool_routes.bp)


def _has_endpoint(name) -> bool:
    """A nav item shows when its page exists; Creator Pools also needs creator_pools.visible() (the flag)."""
    if name not in app.view_functions:
        return False
    if name.startswith("cpools."):
        if g.get("ws") is None:
            return False
        if "cpools_visible" not in g:
            with db.connect() as conn:
                g.cpools_visible = creator_pools.visible(conn, g.ws, g.user)
        return g.cpools_visible
    if name == "mcp.settings":          # the MCP key page shows while MCP_SERVER=on
        return mcpserver.enabled()
    return True


import mcpserver  # noqa: E402
import mcp_tools  # noqa: E402  the MCP server (/mcp, OAuth, API keys); MCP_SERVER=on switches it on
mcp_tools.init(app)
app.jinja_env.globals["has_endpoint"] = _has_endpoint
app.jinja_env.globals.update(faq_answer=public_site.answer_html, solution_links=lambda: public_site.SOLUTION_ORDER,
                             solution_nav=lambda slug: public_site.SOLUTIONS[slug]["nav"], basket=public_site.basket,
                             related_cards=public_site.related_cards, footer_groups=public_site.footer_groups)


@app.errorhandler(core.BillingHold)
def billing_hold_error(e):
    """Something that spends credits, on a workspace with a billing hold (402)."""
    if request.is_json or request.accept_mimetypes.best == "application/json" \
            or request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": e.description, "billing_hold": True, "reason": e.reason,
                        "email": billing_holds.CONTACT, "billing": url_for("billing_page")}), 402
    return render_template("billing_hold.html", hold_email=billing_holds.CONTACT, reason=e.reason), 402


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
