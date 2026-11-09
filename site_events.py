"""First-party funnel events: the `site_events` table (modeled on Crowdcut's conversions.py).

One append-only row per funnel step a visitor or customer takes, carrying that visitor's
first touch (UTM tags, ad click id, referrer), so HQ (dashboard.rectorandstull.com) can count,
per source and per ad: visits, CTA clicks, sign-up page views, sign-ups, first ads, checkouts
and payments. HQ reads this table directly; its columns are a fixed contract:

    id             INTEGER PRIMARY KEY
    ts             TEXT     ISO 8601 UTC, seconds (db.now(): 2026-11-03T17:04:05+00:00)
    vid            TEXT     anonymous visitor id: random, from the first-party cfy_vid cookie
                            (1 year). Never an email or a user id. NULL when the browser keeps no cookie.
    name           TEXT     the event (FUNNEL below)
    path           TEXT     the page path, no query string
    utm_source, utm_medium, utm_campaign, utm_content, utm_term
                   TEXT     the visitor's FIRST touch, as kept by gads.py (cfy_ft cookie, then
                            users.first_touch at sign-up). NULL when the visit carried no tags.
                            utm_source is exactly what the link said; it is not inferred from a click id.
    click_id       TEXT     the ad click id of that first touch: gclid, else fbclid, else ttclid
    click_type     TEXT     'gclid' | 'fbclid' | 'ttclid' (NULL without one of those)
    referrer_host  TEXT     the external site the first visit came from (NULL for direct / unknown)
    detail         TEXT     a short label, <= 120 chars, never personal: which CTA, which tool,
                            plan:<code> / pack:<code>
    workspace_id   INTEGER  the workspace, on server-side steps (signup, first_ad, checkout, paid); NULL otherwise

Events, in funnel order (FUNNEL):

    lp_view      browser: a public marketing page loaded (/, /features, /pricing, solution pages),
                 once per page load
    cta_click    browser: a click on a link to /signup on those pages; detail = the CTA (data-cta)
    signup_view  browser: the sign-up page rendered (GET /signup, not invite links)
    signup       server: a new workspace created by a free sign-up (app.signup; not invites or owner links)
    first_ad     server: the workspace's first generation queued (core.queue_job), once per workspace
    first_run_viewed, first_run_kind_chosen, first_run_imported, verify_box_opened, first_output_viewed
                 browser, logged in: the first-ad form shown; a kind picked on it (detail = image | video |
                 preview); a product page read into it; the confirm-email code box opened; the first finished
                 ad shown on Home
    verify_completed  server: the sign-up's email confirmed (code box, link or the nudge email's magic link), once
    checkout     server: a Stripe Checkout Session created for a plan or a credit pack
    paid         server: from the Stripe webhook, a credit pack paid (once per purchase) or a plan's
                 first invoice paid (billing_reason subscription_create, once per invoice)

Server-side steps copy attribution from the workspace's `signup` row (which copied the creator's
users.first_touch, the cfy_ft cookie at sign-up), so the whole funnel credits one source; a workspace
older than this table falls back to its owner's users.first_touch.

Browsers post to POST /e (JSON, or a text/plain sendBeacon body): CSRF-exempt, same-origin only,
CLIENT_EVENTS only, bodies up to MAX_BODY bytes, RATE_PER_MIN per visitor id and per IP (in memory;
the IP is hashed and never written anywhere), and obvious bots (user agent) are dropped.
Nothing personal is stored: no IP address, no email, no query strings.

Consent: the same rule as the ad pixels (pixels.py). Under Global Privacy Control, Do Not Track,
a consent-region country from the edge, a European time zone, or the cfy_ads=0 cookie the pixels
leave, the beacon doesn't run and sets no cookie, and /e records nothing. Their server-side steps are
still counted, with no visitor id (they have no first touch either: gads.py doesn't keep one for them).

Test traffic is WRITTEN, then left out when read (Crowdcut's rule, conversions.test_traffic_sql):
utm_source in FUNNEL_TEST_SOURCES (default "probe") or click_id in FUNNEL_TEST_CLICK_IDS (default
"test123"). test_traffic_sql() below is that condition for this table; HQ applies the same one and
reports the rows it left out as "excluded".

Nothing here ever raises into a request, a job or the Stripe webhook.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import db
import gads
import pixels

log = logging.getLogger("campaignify")

CLIENT_EVENTS = ("lp_view", "cta_click", "signup_view",
                 # the first run, from the app (logged in; app.js cfyEvent): the first-ad form shown, a kind picked
                 # (detail = image / video / preview), the confirm-email code box opened, the first finished ad seen
                 "first_run_viewed", "first_run_kind_chosen", "first_run_imported", "verify_box_opened",
                 "first_output_viewed",
                 "first_run_sells")   # "What do you sell?" answered (detail = product / service; RS_SERVICE_PATH)
SERVER_EVENTS = ("signup", "first_ad", "checkout", "paid",
                 "verify_completed")   # the email confirmed (code, link or magic link), once per workspace
# Client events that carry a detail label.
DETAIL_EVENTS = {"cta_click", "first_run_viewed", "first_run_kind_chosen", "first_run_imported", "verify_box_opened",
                 "first_output_viewed", "first_run_sells"}
EVENTS = CLIENT_EVENTS + SERVER_EVENTS
FUNNEL = [("lp_view", "Landing visitors"), ("cta_click", "CTA clicks"), ("signup_view", "Sign-up page"),
          ("signup", "Sign-ups"), ("first_ad", "First ad"), ("checkout", "Checkouts"), ("paid", "Paid")]
# Counted as distinct visitors (vid); the other steps are counts of rows.
VISITOR_STEPS = {"lp_view", "cta_click", "signup_view"}

VID_COOKIE = "cfy_vid"
VID_DAYS = 365
ENDPOINT = "/e"
MAX_BODY = 2048
RATE_PER_MIN = 30
DETAIL_MAX = 120
# Public marketing pages that send lp_view (app.py endpoints). Any future blog endpoint ("blog", "blog.*")
# counts too.
LP_ENDPOINTS = {"index", "features", "pricing", "solution"}
CLICK_TYPES = ("gclid", "fbclid", "ttclid")
ATTR_COLS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "click_id", "click_type",
             "referrer_host")

_VID_RE = re.compile(r"^[a-z0-9]{8,40}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,100}$")
_DETAIL_RE = re.compile(r"[^A-Za-z0-9 _.:/+-]")
_PATH_RE = re.compile(r"[^A-Za-z0-9/_.~-]")
# Crawlers, link previews, uptime checks and scripts. People's browsers say "Mozilla/5.0 (...)".
BOT_RE = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|scrap|headless|phantom|puppeteer|playwright|selenium|lighthouse|pagespeed|"
    r"pingdom|uptime|statuscake|monitor|preview|facebookexternalhit|meta-externalagent|embedly|bitlybot|"
    r"curl|wget|python|httpx|aiohttp|go-http|java/|okhttp|axios|"
    r"node-fetch|undici|libwww|postman|insomnia|httpclient|feedfetcher|validator", re.I)

# The table and its indexes are created in db.SCHEMA.


# ---------------------------------------------------------------- test traffic

def _csv_env(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def test_traffic_sql() -> tuple[str, list]:
    """SQL condition (and params) matching our own test visits, which the funnel leaves out:
    utm_source in FUNNEL_TEST_SOURCES (default 'probe', any case) or click_id in
    FUNNEL_TEST_CLICK_IDS (default 'test123'). Same rule as Crowdcut's conversions.test_traffic_sql."""
    sources = [s.lower() for s in _csv_env("FUNNEL_TEST_SOURCES", "probe")]
    clicks = _csv_env("FUNNEL_TEST_CLICK_IDS", "test123")
    parts, params = [], []
    if sources:
        parts.append(f"lower(COALESCE(utm_source,'')) IN ({','.join('?' * len(sources))})")
        params += sources
    if clicks:
        parts.append(f"COALESCE(click_id,'') IN ({','.join('?' * len(clicks))})")
        params += clicks
    return ("(" + " OR ".join(parts) + ")") if parts else "0", params


# ---------------------------------------------------------------- attribution

def _clean_host(h) -> str | None:
    h = str(h or "").strip().lower()[:100]
    return h if h and _HOST_RE.match(h) else None


def _clean_detail(v) -> str | None:
    v = _DETAIL_RE.sub("", str(v or "")).strip()[:DETAIL_MAX]
    return v or None


def _clean_path(p) -> str | None:
    p = str(p or "").split("?", 1)[0].split("#", 1)[0]
    if not p.startswith("/"):
        return None
    return _PATH_RE.sub("", p)[:200] or None


def _touch_dict(raw) -> dict:
    """A first touch (users.first_touch JSON, or a dict from gads.parse_cookie) as a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        d = {}
    return d if isinstance(d, dict) else {}


def from_touch(touch) -> dict:
    """The attribution columns for a gads.py first touch (empty when it has nothing)."""
    t = _touch_dict(touch)
    out = {k: (gads._clean(t.get(k))[:100] or None) for k in gads.UTM_KEYS}
    ct = next((k for k in CLICK_TYPES if gads._clean(t.get(k))), None)
    out["click_type"] = ct
    out["click_id"] = gads._clean(t.get(ct))[:100] if ct else None
    out["referrer_host"] = _clean_host(t.get("ref"))
    return out if any(out.values()) or any(gads._clean(t.get(k)) for k in gads.CLICK_KEYS) else {}


def _earliest_for_vid(conn, vid) -> dict:
    if not vid:
        return {}
    r = conn.execute(f"SELECT {', '.join(ATTR_COLS)} FROM site_events WHERE vid=? ORDER BY id LIMIT 1",
                     (vid,)).fetchone()
    return {k: r[k] for k in ATTR_COLS} if r else {}


def attribution(conn, vid, touch, referrer_host=None) -> dict:
    """First touch for this visitor: the gads.py touch when it has tags or a click id, else whatever
    this vid's first row said (an earlier organic visit), else just this visit's external referrer."""
    a = from_touch(touch)
    if a:
        if not a.get("referrer_host"):
            a["referrer_host"] = _earliest_for_vid(conn, vid).get("referrer_host") or _clean_host(referrer_host)
        return a
    first = _earliest_for_vid(conn, vid)
    if first:
        return first
    return {"referrer_host": _clean_host(referrer_host)}


# ---------------------------------------------------------------- writing

def record(conn, name: str, *, vid=None, path=None, attr: dict | None = None, detail=None,
           workspace_id=None) -> int | None:
    """One row. Never raises."""
    if name not in EVENTS:
        return None
    try:
        vid = vid if vid and _VID_RE.match(str(vid)) else None
        a = {k: (attr or {}).get(k) for k in ATTR_COLS}
        if a["click_type"] not in CLICK_TYPES:
            a["click_type"] = a["click_id"] = None
        cur = conn.execute(
            "INSERT INTO site_events (ts, vid, name, path, utm_source, utm_medium, utm_campaign, utm_content, "
            "utm_term, click_id, click_type, referrer_host, detail, workspace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (db.now(), vid, name, _clean_path(path), a["utm_source"], a["utm_medium"], a["utm_campaign"],
             a["utm_content"], a["utm_term"], a["click_id"], a["click_type"], a["referrer_host"],
             _clean_detail(detail), workspace_id))
        conn.commit()
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — measurement never breaks a page
        log.warning("site event %s not recorded: %s", name, str(e)[:120])
        return None


def request_vid(req) -> str | None:
    """The visitor id from the cfy_vid cookie, or None (no cookie, a malformed one, or opted out)."""
    if req is None:
        return None
    try:
        if pixels.opted_out(req):
            return None
        v = (req.cookies.get(VID_COOKIE) or "")[:40]
    except Exception:  # noqa: BLE001 — outside a request
        return None
    return v if _VID_RE.match(v) else None


def on_signup(conn, workspace_id: int, user_id: int, req, detail=None) -> int | None:
    """A new workspace from a free sign-up: its creator's users.first_touch (just copied from cfy_ft)."""
    try:
        if conn.execute("SELECT 1 FROM site_events WHERE workspace_id=? AND name='signup'", (workspace_id,)).fetchone():
            return None
        u = conn.execute("SELECT first_touch FROM users WHERE id=?", (user_id,)).fetchone()
        vid = request_vid(req)
        attr = attribution(conn, vid, u["first_touch"] if u else None)
        return record(conn, "signup", vid=vid, path="/signup", attr=attr, detail=detail, workspace_id=workspace_id)
    except Exception as e:  # noqa: BLE001
        log.warning("signup event not recorded: %s", str(e)[:120])
        return None


def _workspace_attr(conn, workspace_id: int, req) -> tuple[str | None, dict]:
    """(vid, attribution) for a later server-side step: the workspace's signup row, else its owner's
    users.first_touch with this browser's vid."""
    s = conn.execute(f"SELECT vid, {', '.join(ATTR_COLS)} FROM site_events WHERE workspace_id=? AND name='signup' "
                     "ORDER BY id LIMIT 1", (workspace_id,)).fetchone()
    if s:
        return s["vid"], {k: s[k] for k in ATTR_COLS}
    u = conn.execute("SELECT first_touch FROM users WHERE workspace_id=? ORDER BY (role='owner') DESC, id LIMIT 1",
                     (workspace_id,)).fetchone()
    vid = request_vid(req)
    return vid, attribution(conn, vid, u["first_touch"] if u else None)


def on_workspace_step(conn, name: str, workspace_id: int, req=None, detail=None, path=None,
                      once: bool = False) -> int | None:
    """first_ad / checkout / paid for a workspace. `once`: skip when this workspace already has one."""
    if name not in ("first_ad", "checkout", "paid", "verify_completed") or not workspace_id:
        return None
    try:
        if once and conn.execute("SELECT 1 FROM site_events WHERE workspace_id=? AND name=?",
                                 (workspace_id, name)).fetchone():
            return None
        vid, attr = _workspace_attr(conn, workspace_id, req)
        if path is None and req is not None:
            try:
                path = req.path
            except Exception:  # noqa: BLE001
                path = None
        return record(conn, name, vid=vid, path=path, attr=attr, detail=detail, workspace_id=workspace_id)
    except Exception as e:  # noqa: BLE001
        log.warning("site event %s not recorded: %s", name, str(e)[:120])
        return None


def on_job_queued(conn, workspace_id: int, job_id: int, tool: str, req=None) -> int | None:
    """first_ad: only for the workspace's first job ever (its lowest job id), once."""
    try:
        first = conn.execute("SELECT MIN(id) FROM jobs WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
        if first != job_id:
            return None
        return on_workspace_step(conn, "first_ad", workspace_id, req, detail=tool, once=True)
    except Exception as e:  # noqa: BLE001
        log.warning("first_ad event not recorded: %s", str(e)[:120])
        return None


# ---------------------------------------------------------------- the browser beacon

_rate: dict[str, deque] = {}
_rate_lock = threading.Lock()


def _rate_ok(keys: list[str]) -> bool:
    """RATE_PER_MIN per key in a sliding minute, in memory (one gunicorn process: Dockerfile)."""
    now = time.monotonic()
    with _rate_lock:
        if len(_rate) > 20000:   # forget idle keys
            for k in [k for k, q in _rate.items() if not q or now - q[-1] > 60]:
                _rate.pop(k, None)
        for k in keys:
            q = _rate.setdefault(k, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE_PER_MIN:
                return False
        for k in keys:
            _rate[k].append(now)
    return True


def _ip_key(ip: str) -> str:
    salt = os.environ.get("RS_SECRET_KEY", "") or _SALT
    return "ip:" + hashlib.sha256(f"cfy-ev:{salt}:{ip}".encode()).hexdigest()[:20]


_SALT = secrets.token_hex(8)


def is_bot(ua: str) -> bool:
    ua = (ua or "").strip()
    return not ua or not ua.startswith("Mozilla/") or bool(BOT_RE.search(ua))


def _same_origin(req) -> bool:
    """Browsers mark a cross-site request (Sec-Fetch-Site, Origin); those never count."""
    site = (req.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = (req.headers.get("Origin") or "").strip()
    if origin and origin != "null":
        return (urlsplit(origin).netloc or "").lower() == (req.host or "").lower()
    return True


def handle(req, user=None):
    """POST /e. Returns (body, status). 204 for anything accepted or quietly dropped (bots,
    consent, cross-site); 400 for a name not in CLIENT_EVENTS or a bad body; 413 too big; 429 too fast."""
    n = req.content_length
    if n is None or n > MAX_BODY:
        return ("", 413)
    try:
        d = json.loads(req.get_data(cache=False, as_text=True) or "{}")
    except (ValueError, TypeError):
        return ("", 400)
    if not isinstance(d, dict):
        return ("", 400)
    name = str(d.get("name") or "")
    if name not in CLIENT_EVENTS:
        return ("", 400)
    if is_bot(req.user_agent.string or "") or not _same_origin(req) or pixels.opted_out(req):
        return ("", 204)
    vid = request_vid(req)
    keys = [_ip_key(req.remote_addr or "")] + ([f"vid:{vid}"] if vid else [])
    if not _rate_ok(keys):
        return ("", 429)
    ref = _clean_host(d.get("ref"))
    if ref and ref == (req.host or "").split(":")[0].lower():
        ref = None
    touch = gads.parse_cookie(req.cookies.get(gads.COOKIE)) or (user["first_touch"] if user is not None else None)
    detail = d.get("detail") if name in DETAIL_EVENTS else None
    ws_id = user["workspace_id"] if user is not None and "workspace_id" in user.keys() else None
    with db.connect() as conn:
        if ws_id:   # a member's event: credited to the workspace's sign-up source, like the server-side steps
            vid, attr = _workspace_attr(conn, ws_id, req)
        else:
            attr = attribution(conn, vid, touch, ref)
        record(conn, name, vid=vid, path=d.get("path"), attr=attr, detail=detail, workspace_id=ws_id)
    return ("", 204)


def beacon(req, user=None) -> dict | None:
    """What web/_beacon.html needs on this page, or None when it doesn't run here: public marketing
    pages (lp_view + CTA clicks) and the sign-up page (signup_view), for logged-out visitors who
    haven't opted out (the browser re-checks GPC / DNT / a European time zone itself)."""
    try:
        if user is not None or req.method != "GET" or pixels.opted_out(req):
            return None
        ep = req.endpoint or ""
        if ep in LP_ENDPOINTS or ep == "blog" or ep.startswith("blog."):
            view, cta = "lp_view", True
        elif ep == "signup" and not req.args.get("invite"):
            view, cta = "signup_view", False
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"url": ENDPOINT, "view": view, "cta": cta, "vid_cookie": VID_COOKIE, "vid_days": VID_DAYS,
            "optout_cookie": pixels.OPTOUT_COOKIE, "consent_tz": pixels.CONSENT_TZ,
            "consent_tz_prefix": pixels.CONSENT_TZ_PREFIX}


# ---------------------------------------------------------------- members' pages: the first-party collector (M3.4)
# A separate table (page_events), not site_events: site_events is Campaignify's own funnel, read by HQ under a
# fixed contract. page_events is per workspace and is the record behind conversions.py's server-side copies:
# every page view sent server side and every form lead gets a UUID here, and the browser pixels fire with the
# same UUID, so Meta (event_id), TikTok (event_id) and Google (transaction_id) count it once.

PAGE_EVENTS = ("PageView", "Lead")
LEAD_BROWSER_S = 3600          # the thank-you page fires the browser Lead for a lead at most this old, once
PAGE_EVENTS_KEEP_DAYS = 90
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_CLICK_RE = re.compile(r"^[A-Za-z0-9._~-]{4,200}$")


def new_event_id() -> str:
    import uuid
    return str(uuid.uuid4())


def clean_event_id(v) -> str | None:
    """A version 4 UUID (lower case), or None."""
    v = str(v or "").strip().lower()
    return v if _UUID_RE.match(v) else None


def click_of(values) -> tuple[str | None, str | None]:
    """(click_type, click_id) from query args or a form's hidden fields: gclid, else fbclid, else ttclid.
    `values` maps either "gclid" or "_gclid" style keys."""
    for k in CLICK_TYPES:
        v = str((values.get(k) or values.get("_" + k) or "") if values is not None else "").strip()
        if v and _CLICK_RE.match(v):
            return k, v
    return None, None


def clicks_of(values) -> dict:
    """Every ad click id in query args or hidden fields: {"gclid": ..., "fbclid": ..., "ttclid": ...} (those present)."""
    out = {}
    for k in CLICK_TYPES:
        v = str((values.get(k) or values.get("_" + k) or "") if values is not None else "").strip()
        if v and _CLICK_RE.match(v):
            out[k] = v
    return out


def record_page_event(conn, workspace_id: int, name: str, event_id: str, *, page_id=None, form_id=None,
                      submission_id=None, path=None, click=(None, None), hashes=None) -> bool:
    """One collector row. False when the event id is taken (a repeat) or anything is wrong. Never raises."""
    eid = clean_event_id(event_id)
    if name not in PAGE_EVENTS or not eid or not workspace_id:
        return False
    try:
        ct, cid = click if click and click[0] in CLICK_TYPES else (None, None)
        cur = conn.execute("INSERT OR IGNORE INTO page_events (workspace_id, page_id, form_id, submission_id, name, "
                           "event_id, path, click_type, click_id, hashes_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (int(workspace_id), page_id, form_id, submission_id, name, eid, _clean_path(path), ct,
                            (cid or None) and cid[:200], json.dumps(hashes) if hashes else None, db.now()))
        conn.commit()
        return cur.rowcount == 1
    except Exception as e:  # noqa: BLE001 — measurement never breaks a page
        log.warning("page event %s not recorded: %s", name, str(e)[:120])
        return False


def lead_for_browser(conn, event_id, *, page_id=None, form_id=None):
    """The thank-you page's lead: a Lead row of this page (or hosted form) made in the last hour that the browser
    hasn't fired yet. Marks it fired and returns it, else None. So a link with ?ev= can't make the pixels fire."""
    eid = clean_event_id(event_id)
    if not eid or (page_id is None and form_id is None):
        return None
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(seconds=LEAD_BROWSER_S)).replace(microsecond=0).isoformat()
    try:
        row = conn.execute("SELECT * FROM page_events WHERE event_id=? AND name='Lead' AND browser_at IS NULL AND "
                           "created_at>? AND " + ("page_id=?" if page_id is not None else "form_id=? AND page_id IS NULL"),
                           (eid, since, page_id if page_id is not None else form_id)).fetchone()
        if row is None:
            return None
        cur = conn.execute("UPDATE page_events SET browser_at=? WHERE id=? AND browser_at IS NULL", (db.now(), row["id"]))
        conn.commit()
        return row if cur.rowcount == 1 else None
    except Exception as e:  # noqa: BLE001
        log.warning("thank-you lead lookup failed: %s", str(e)[:120])
        return None


def purge_page_events(conn) -> int:
    """Collector rows older than PAGE_EVENTS_KEEP_DAYS go (the worker, with the conversion retries)."""
    from datetime import datetime, timedelta, timezone
    cut = (datetime.now(timezone.utc) - timedelta(days=PAGE_EVENTS_KEEP_DAYS)).replace(microsecond=0).isoformat()
    n = conn.execute("DELETE FROM page_events WHERE created_at<?", (cut,)).rowcount
    conn.commit()
    return n
