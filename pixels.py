"""Ad-platform pixels on our own site: Meta, TikTok, Reddit and X.

Each pixel loads only once its id is set (settings.txt, sent by the deploy from GitHub
repo variables). Empty or "off" means that pixel isn't loaded at all:

    META_PIXEL_ID        Meta Events Manager > the dataset (pixel) id, the number in fbq('init', ...)
    TIKTOK_PIXEL_ID      TikTok Ads Manager > Assets > Events > Web events > the pixel code (C...)
    REDDIT_PIXEL_ID      Reddit Ads > Events Manager > Reddit Pixel id (a2_... or t2_...)
    X_PIXEL_ID           X Ads > Tools > Events manager > the base pixel id (twq('config', ...))
    X_EVENT_SIGNUP       X event ids (tw-<pixel>-<event>), one per conversion event created in X Ads.
    X_EVENT_PURCHASE     X has no standard event names, so an event without its id isn't sent to X.
    X_EVENT_VIEWCONTENT  optional, the pricing page view

They load on every page built on web/base.html (the public site and the app) through
web/_pixels.html, never on members' pages, share links or admin pages, and not on a page
whose address carries anything but campaign tags, ad click ids or our checkout return
(see page_ok): an invite code, a search box or an email in a link never reaches an ad platform.

Events (same moments as the Google Ads conversions in gads.py):

    page view    every page: Meta PageView, TikTok page(), Reddit PageVisit, X config (its page view)
    ViewContent  /pricing: Meta + TikTok + Reddit ViewContent, X_EVENT_VIEWCONTENT when set
    SignUp       the first page after a new account is created (not owners' setup links):
                 Meta CompleteRegistration, TikTok CompleteRegistration, Reddit SignUp, X_EVENT_SIGNUP;
                 event id signup-<user id>
    Purchase     the billing page Stripe returns to after a plan or credit-pack checkout, once per
                 Checkout Session, with value + currency: Meta Purchase, TikTok CompletePayment,
                 Reddit Purchase, X_EVENT_PURCHASE; event id purchase-<Checkout Session id>

The server reports SignUp and Purchase too when a token is set (conversions.py), with the same
event id, so each platform counts the conversion once.

Consent: there's no cookie banner. The pixels don't load at all (no cookies, no requests) for a
browser that sends Global Privacy Control or Do Not Track, for a visitor the edge places in the
EEA/UK/CH (gads.opted_out), or whose time zone is in Europe; those visitors get the cfy_ads=0
cookie so the server-side copies skip them too. Only event names, ids and the purchase amount
leave the browser: no email, name or account content.
"""
import os
import re

import gads

ENV = {"meta": "META_PIXEL_ID", "tiktok": "TIKTOK_PIXEL_ID", "reddit": "REDDIT_PIXEL_ID", "x": "X_PIXEL_ID"}
X_EVENT_ENV = {"SignUp": "X_EVENT_SIGNUP", "Purchase": "X_EVENT_PURCHASE", "ViewContent": "X_EVENT_VIEWCONTENT"}
# Our names -> each platform's standard event.
EVENTS = {
    "meta": {"SignUp": "CompleteRegistration", "Purchase": "Purchase", "ViewContent": "ViewContent"},
    "tiktok": {"SignUp": "CompleteRegistration", "Purchase": "CompletePayment", "ViewContent": "ViewContent"},
    "reddit": {"SignUp": "SignUp", "Purchase": "Purchase", "ViewContent": "ViewContent"},
}
# gads.py kinds -> our names
KIND = {"sign_up": "SignUp", "purchase": "Purchase"}
OPTOUT_COOKIE = "cfy_ads"
# Time zones treated like the consent regions when the edge doesn't tell us the country.
CONSENT_TZ_PREFIX = "Europe/"
CONSENT_TZ = ["Atlantic/Reykjavik", "Atlantic/Canary", "Atlantic/Madeira", "Atlantic/Azores", "Atlantic/Faroe",
              "Arctic/Longyearbyen"]

# Query keys a page may carry and still load the pixels (everything else could be personal:
# an invite code, a search term, an email).
SAFE_ARGS = set(gads.UTM_KEYS) | set(gads.CLICK_KEYS) | {"checkout", "plan", "session_id"}
# Never on these, whatever the query.
NO_PIXEL_PREFIXES = ("/admin", "/verify", "/s/", "/api/", "/t/", "/u/", "/_")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,100}$")


def _env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if v.lower() in ("off", "none", "0", "false"):
        return ""
    return v if _ID_RE.match(v) else ""   # anything else is dropped rather than echoed into a script


def ids() -> dict:
    """{"meta", "tiktok", "reddit", "x"}: the ids set (each "" when not)."""
    return {k: _env(v) for k, v in ENV.items()}


def x_events() -> dict:
    return {k: _env(v) for k, v in X_EVENT_ENV.items()}


def enabled() -> bool:
    """Any pixel set."""
    return any(ids().values())


def clean_event_id(v) -> str:
    v = str(v or "")
    return v if _EVENT_ID_RE.match(v) else ""


def opted_out(req) -> bool:
    """GPC / DNT / a consent-region country from the edge, or the browser already told us (cfy_ads=0)."""
    return gads.opted_out(req.headers) or req.cookies.get(OPTOUT_COOKIE) == "0"


def page_ok(req) -> bool:
    """Whether this page's address is safe to hand to the ad platforms (the pixels send it)."""
    if req.path.startswith(NO_PIXEL_PREFIXES):
        return False
    if any(k not in SAFE_ARGS for k in req.args.keys()):
        return False
    return "@" not in req.query_string.decode("latin-1") and "%40" not in req.query_string.decode("latin-1")


def events(gads_events: list[dict]) -> list[dict]:
    """The pixel events for this page from gads.take() (each carries an event id)."""
    out = []
    for ev in gads_events or []:
        name, eid = KIND.get(ev.get("kind") or ""), clean_event_id(ev.get("event_id"))
        if not name or not eid:
            continue
        e = {"name": name, "event_id": eid}
        if ev.get("value") is not None:
            e["value"] = round(float(ev["value"]), 2)
            e["currency"] = (ev.get("currency") or "USD").upper()
        out.append(e)
    return out


def context(req, gads_events_fn, endpoint: str | None) -> dict | None:
    """What web/_pixels.html needs, or None when nothing loads on this page."""
    p = ids()
    if not any(p.values()) or opted_out(req) or not page_ok(req):
        return None
    return {"ids": p, "x_events": x_events(), "events": events(gads_events_fn()),
            "view_content": endpoint == "pricing", "consent_tz": CONSENT_TZ, "consent_tz_prefix": CONSENT_TZ_PREFIX,
            "optout_cookie": OPTOUT_COOKIE}
