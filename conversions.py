"""Server-side copies of our own ad conversions: Meta Conversions API, TikTok Events API
and Reddit Conversions API (modeled on Crowdcut's conversions.py).

SignUp and Purchase go to each platform from the server as well as from its pixel
(pixels.py), with the same event id on both sides (signup-<user id>, purchase-<Checkout
Session id>), so each platform counts the conversion once and a blocked pixel or a closed
tab doesn't lose it. Purchase is sent from the Stripe webhook (checkout.session.completed),
so it arrives even when the buyer never comes back to the billing page.

Optional. A platform is skipped until both its pixel id and its token are set:

    META_CAPI_TOKEN       Meta Events Manager > the dataset > Settings > Conversions API > Generate access token
    TIKTOK_EVENTS_TOKEN   TikTok Events Manager > the pixel > Settings > Events API > Generate access token
    REDDIT_CAPI_TOKEN     Reddit Ads > Events Manager > Conversions API > Generate access token
    META_TEST_EVENT_CODE / TIKTOK_TEST_EVENT_CODE / REDDIT_CAPI_TEST=1
                          send to the platforms' test tools instead (for checking deduplication)

X has no server-side copy (its Conversions API needs OAuth app credentials); the X pixel covers it.

What is sent: the event name, time, event id, the page address without its query, the visitor's
IP address and browser user agent, the platform's own first-party cookie and ad click id when
present, and for Purchase the amount and currency. Never the email, name or account content.
Visitors the pixels skip for consent (pixels.opted_out: GPC, Do Not Track, EEA/UK/CH, a time
zone in Europe) are skipped here too. Rows are queued once per platform and event id
(conversion_queue), sent on a daemon thread and retried by the worker. Nothing here ever raises
into a request or the Stripe webhook.

Members' own pages and forms (roadmap M3.4, design doc 4.5; the second half of this file)
------------------------------------------------------------------------------------------
Campaignify landing pages (/p/<slug>) and hosted forms (/f/<code>) are where members' ads send people. With
CONVERSIONS_API=1 (default off) on top of AD_LAUNCH, each page view and each form lead gets a UUID event id
(site_events.page_events, the first-party collector). The browser pixels of the workspace's own connected
destinations fire with that id (static/pagepx.js: Meta eventID, TikTok event_id), and the server posts the
same event to each destination (ws_conversion_queue, sent on a daemon thread, retried by the worker):

    Meta      Conversions API, /<pixel id>/events on the pixel picked at connect (dataset = the pixel). The token
              is the one the member pastes for that pixel (Events Manager > the dataset > Settings > Generate
              access token), kept in a token box (pixels.capi_token_enc). META_CAPI_USE_CONNECTION_TOKEN=1 also
              lets a pixel without one use the connection's own token (only after tools/conversions_check.py
              shows Meta takes it). Dedup on event_name + event_id. PageView and Lead.
    TikTok    Events API 2.0, /open_api/v1.3/event/track/ with the pixel code and Events API token the member adds
              in Settings > Conversion tracking (no TikTok connect yet). Dedup on event_id (48 hours, first copy
              kept). ViewContent (page view) and SubmitForm (lead).
    Google    Data Manager API, events:ingest, leads only, to the connection's "lead_server" conversion action
              (conversion_actions, made by M3.5's sync), with the click's gclid; the connection's refresh token
              must carry the datamanager scope. Dedup on transaction_id = the event id.

Hashed only: an email (trimmed, lower case; Google also drops dots before @gmail.com) and a phone (E.164;
Meta without the +) leave as SHA-256 hex, per platform, and only for leads. The raw values stay in the
member's contacts. The page address is sent without its query string. The visitor's IP address and browser
user agent ride in the queued payload until it is sent, then are stripped from the row.

Consent: pages and forms have no consent banner, so each has a measurement setting (pages.measurement,
forms.measurement): 'us' (the default) measures visitors the edge places in the US (CF-IPCountry) who send no
Global Privacy Control or Do Not Track; 'off' never measures. Anyone else gets no pixel script, no event id
and no server copy. Preview pages and bots are never measured.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import db
import gads
import pixels

log = logging.getLogger("campaignify")

TOKEN_ENV = {"meta": "META_CAPI_TOKEN", "tiktok": "TIKTOK_EVENTS_TOKEN", "reddit": "REDDIT_CAPI_TOKEN"}
EVENTS = ("SignUp", "Purchase")
MAX_ATTEMPTS = 4
SITE = "https://campaignify.ai/"


def _token(platform: str) -> str:
    return (os.environ.get(TOKEN_ENV[platform]) or "").strip()


def enabled_platforms() -> list[str]:
    p = pixels.ids()
    return [k for k in ("meta", "tiktok", "reddit") if _token(k) and p[k]]


def context(req) -> dict | None:
    """The visitor's browser context for a server-side event, or None when it must not be sent
    (consent) or nothing would send it (no token set)."""
    if not enabled_platforms() or pixels.opted_out(req):
        return None
    ck = req.cookies
    ft = gads.parse_cookie(ck.get(gads.COOKIE))
    fbc = ck.get("_fbc") or (f"fb.1.{int(time.time() * 1000)}.{ft['fbclid']}" if ft.get("fbclid") else None)
    return {k: v for k, v in {
        "ip": (req.remote_addr or "")[:64], "ua": (req.user_agent.string or "")[:400],
        "url": (req.host_url.rstrip("/") + req.path)[:300],   # never the query string
        "fbp": ck.get("_fbp"), "fbc": fbc, "ttp": ck.get("_ttp"), "ttclid": ft.get("ttclid"),
        "rdt_uuid": ck.get("_rdt_uuid"), "rdt_cid": ft.get("rdt_cid"),
    }.items() if v}


def queue(conn, event: str, event_id: str, ctx: dict | None, value: float | None = None,
          currency: str = "USD") -> int:
    """Queue SignUp / Purchase for every platform with a token. `ctx` None (consent, or no
    context kept) sends nothing. Returns rows queued; a repeat of the same event id is a no-op."""
    if event not in EVENTS or not pixels.clean_event_id(event_id) or not ctx:
        return 0
    payload = {"event": event, "event_id": event_id, "ts": int(time.time()), "ctx": ctx,
               "value": None if value is None else round(float(value), 2), "currency": (currency or "USD").upper()}
    n = 0
    for platform in enabled_platforms():
        try:
            n += conn.execute("INSERT OR IGNORE INTO conversion_queue (platform, event, event_id, payload, created_at) "
                              "VALUES (?,?,?,?,?)", (platform, event, event_id, json.dumps(payload), db.now())).rowcount
        except Exception as e:  # noqa: BLE001
            log.warning("conversion queue error: %s", str(e)[:120])
    conn.commit()
    return n


def build(platform: str, p: dict) -> tuple[str, dict, dict]:
    """(url, headers, json body) for one conversion. Pure: the tests check the shapes."""
    ctx = p.get("ctx") or {}
    name = pixels.EVENTS[platform][p["event"]]
    pixel = pixels.ids()[platform]
    value = p.get("value")
    url = ctx.get("url") or SITE
    if platform == "meta":
        ud = {k: v for k, v in {"client_ip_address": ctx.get("ip"), "client_user_agent": ctx.get("ua"),
                                "fbc": ctx.get("fbc"), "fbp": ctx.get("fbp")}.items() if v}
        ev = {"event_name": name, "event_time": p["ts"], "event_id": p["event_id"], "action_source": "website",
              "event_source_url": url, "user_data": ud}
        if value is not None:
            ev["custom_data"] = {"value": value, "currency": p.get("currency", "USD")}
        body = {"data": [ev]}
        if os.environ.get("META_TEST_EVENT_CODE"):
            body["test_event_code"] = os.environ["META_TEST_EVENT_CODE"]
        # as Crowdcut sends it; errors below never log the URL
        return (f"https://graph.facebook.com/v21.0/{pixel}/events?access_token={_token('meta')}",
                {"Content-Type": "application/json"}, body)
    if platform == "tiktok":
        user = {k: v for k, v in {"ip": ctx.get("ip"), "user_agent": ctx.get("ua"), "ttclid": ctx.get("ttclid"),
                                  "ttp": ctx.get("ttp")}.items() if v}
        ev = {"event": name, "event_time": p["ts"], "event_id": p["event_id"], "user": user, "page": {"url": url}}
        if value is not None:
            ev["properties"] = {"value": value, "currency": p.get("currency", "USD")}
        body = {"event_source": "web", "event_source_id": pixel, "data": [ev]}
        if os.environ.get("TIKTOK_TEST_EVENT_CODE"):
            body["test_event_code"] = os.environ["TIKTOK_TEST_EVENT_CODE"]
        return ("https://business-api.tiktok.com/open_api/v1.3/event/track/",
                {"Access-Token": _token("tiktok"), "Content-Type": "application/json"}, body)
    if platform == "reddit":
        user = {k: v for k, v in {"ip_address": hashlib.sha256(ctx["ip"].encode()).hexdigest() if ctx.get("ip") else None,
                                  "user_agent": ctx.get("ua"), "uuid": ctx.get("rdt_uuid")}.items() if v}
        meta = {"conversion_id": p["event_id"]}
        if value is not None:
            meta.update(value_decimal=value, currency=p.get("currency", "USD"))
        ev = {"event_at": datetime.fromtimestamp(p["ts"], tz=timezone.utc).isoformat(timespec="seconds"),
              "event_type": {"tracking_type": name}, "event_metadata": meta, "user": user}
        if ctx.get("rdt_cid"):
            ev["click_id"] = ctx["rdt_cid"]
        body = {"events": [ev]}
        if os.environ.get("REDDIT_CAPI_TEST", "").strip() in ("1", "true", "yes"):
            body["test_mode"] = True
        return (f"https://ads-api.reddit.com/api/v2.0/conversions/events/{pixel}",
                {"Authorization": f"Bearer {_token('reddit')}", "Content-Type": "application/json",
                 "User-Agent": "campaignify-capi/1.0"}, body)
    raise ValueError(platform)


def _send(platform: str, payload: dict) -> tuple[bool, str]:
    url, headers, body = build(platform, payload)
    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
    except requests.RequestException as e:
        return False, f"network: {type(e).__name__}"
    txt = re.sub(r"access_token=[^&\s\"]+", "access_token=…", (r.text or "")[:300])
    if r.status_code >= 400:
        return False, f"{r.status_code} {txt}"
    if platform == "tiktok":
        try:
            if r.json().get("code") not in (0, None):
                return False, f"tiktok {txt}"
        except ValueError:
            pass
    return True, txt


def flush(conn, limit: int = 25) -> dict:
    """Send what is pending (and retry failures, up to MAX_ATTEMPTS, for 3 days). Each row is
    claimed before sending so two processes never send it twice."""
    st = {"sent": 0, "failed": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat()
    rows = conn.execute("SELECT * FROM conversion_queue WHERE status IN ('pending','failed') AND attempts<? "
                        "AND created_at>? ORDER BY id LIMIT ?", (MAX_ATTEMPTS, since, limit)).fetchall()
    for r in rows:
        if r["platform"] not in enabled_platforms():
            continue
        cur = conn.execute("UPDATE conversion_queue SET status='sending', attempts=attempts+1 WHERE id=? "
                           "AND status IN ('pending','failed')", (r["id"],))
        conn.commit()
        if not cur.rowcount:
            continue
        try:
            ok, note = _send(r["platform"], json.loads(r["payload"]))
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"error: {type(e).__name__}"
        conn.execute("UPDATE conversion_queue SET status=?, error=?, sent_at=? WHERE id=?",
                     ("sent" if ok else "failed", None if ok else note[:300], db.now() if ok else None, r["id"]))
        conn.commit()
        st["sent" if ok else "failed"] += 1
    # a row left 'sending' by a crashed process goes back in the queue after 10 minutes
    conn.execute("UPDATE conversion_queue SET status='failed', error='interrupted' WHERE status='sending' AND created_at<?",
                 ((datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat(),))
    conn.commit()
    return st


def flush_soon() -> None:
    """Send the queue on a daemon thread so the request that queued it returns at once."""
    if not enabled_platforms() or os.environ.get("RS_CAPI_SYNC") == "1":
        return

    def run():
        try:
            with db.connect() as c:
                flush(c)
        except Exception as e:  # noqa: BLE001
            log.warning("conversion flush error: %s", str(e)[:150])
    threading.Thread(target=run, daemon=True, name="capi-flush").start()


_last_tick = [float("-inf")]   # not 0.0: a just-booted machine has time.monotonic() under RETRY_SECONDS
RETRY_SECONDS = 300


def tick() -> None:
    """Worker housekeeping, every few minutes: retry what didn't go out (ours, and members' pages')."""
    own, members = bool(enabled_platforms()), ws_flag_on()
    if not (own or members) or time.monotonic() - _last_tick[0] < RETRY_SECONDS:
        return
    _last_tick[0] = time.monotonic()
    with db.connect() as c:
        if own:
            flush(c)
        if members:
            flush_ws(c)
            import site_events
            site_events.purge_page_events(c)


# ---------------------------------------------------------------- the two moments

def on_signup(conn, user_id: int, ctx: dict | None) -> int:
    n = queue(conn, "SignUp", gads.event_id("sign_up", f"signup-{user_id}"), ctx)
    if n:
        flush_soon()
    return n


def on_checkout_paid(conn, sess: dict, workspace_id) -> int:
    """checkout.session.completed (paid) for a checkout we started (ad_conversions) in this
    workspace: Purchase at what Stripe charged. Webhook retries are no-ops (same event id)."""
    sid = str((sess or {}).get("id") or "")
    if not sid.startswith("cs_") or sess.get("payment_status") not in ("paid", "no_payment_required"):
        return 0
    if not enabled_platforms():
        return 0
    row = conn.execute("SELECT * FROM ad_conversions WHERE kind='purchase' AND ref=? AND workspace_id=?",
                       (sid, workspace_id)).fetchone()
    if not row or not row["ctx_json"]:
        return 0
    try:
        ctx = json.loads(row["ctx_json"])
    except ValueError:
        return 0
    cents = sess["amount_total"] if isinstance(sess.get("amount_total"), int) else row["value_cents"]
    n = queue(conn, "Purchase", gads.event_id("purchase", sid), ctx, value=(cents or 0) / 100,
              currency=sess.get("currency") or row["currency"] or "usd")
    if n:
        flush_soon()
    return n


# ================================================================ members' own pages and forms (M3.4)

WS_PLATFORMS = ("meta", "tiktok", "google")
# Our names -> each platform's event. Google gets leads only (Data Manager).
WS_EVENTS = {"meta": {"PageView": "PageView", "Lead": "Lead"},
             "tiktok": {"PageView": "ViewContent", "Lead": "SubmitForm"},
             "google": {"Lead": "Lead"}}
MEASURE_VALUES = ("us", "off")
MEASURE_COUNTRIES = {"US"}
TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"
DATA_MANAGER_URL = "https://datamanager.googleapis.com/v1/events:ingest"
DATAMANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
WS_MAX_ATTEMPTS = 4
_META_PIXEL_RE = re.compile(r"^\d{5,20}$")
_TIKTOK_CODE_RE = re.compile(r"^[A-Z0-9]{10,30}$")
_GOOGLE_TAG_RE = re.compile(r"^AW-\d{6,15}$")
_GOOGLE_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def ws_flag_on() -> bool:
    """CONVERSIONS_API=1 turns conversion tracking on members' pages on (default off)."""
    return (os.environ.get("CONVERSIONS_API") or "").strip().lower() in ("1", "on", "true", "yes")


def ws_enabled(ws, user=None) -> bool:
    """Both flags: CONVERSIONS_API and AD_LAUNCH for this workspace (the destinations come from its connections)."""
    if not ws_flag_on() or ws is None:
        return False
    import adplatforms
    return adplatforms.enabled(ws, user)


def meta_connection_token_ok() -> bool:
    return (os.environ.get("META_CAPI_USE_CONNECTION_TOKEN") or "").strip().lower() in ("1", "on", "true", "yes")


# ---------------------------------------------------------------- consent

def visitor_country(req) -> str:
    return (req.headers.get("CF-IPCountry") or req.headers.get("X-Country") or "").strip().upper()[:2]


def measured(req, setting) -> bool:
    """May this visit be measured under the owner's setting? 'us' = a US visitor (by the edge) without GPC or
    Do Not Track, in a person's browser; anything else, or 'off', is never measured."""
    try:
        if (setting or "us") != "us":
            return False
        h = req.headers
        if h.get("Sec-GPC", "").strip() == "1" or h.get("DNT", "").strip() == "1":
            return False
        if visitor_country(req) not in MEASURE_COUNTRIES:
            return False
        import site_events
        return not site_events.is_bot(req.user_agent.string or "")
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- hashing (never the raw value)

def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def norm_email(email, platform: str) -> str | None:
    e = str(email or "").strip().lower()
    if not re.match(r"^[^@\s]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$", e):
        return None
    if platform == "google":
        local, dom = e.rsplit("@", 1)
        if dom in ("gmail.com", "googlemail.com"):
            e = local.replace(".", "") + "@" + dom
    return e


def norm_phone(phone, platform: str) -> str | None:
    import messaging
    e = messaging.e164(str(phone or ""))
    if not e:
        return None
    return e[1:] if platform == "meta" else e


def lead_hashes(email, phone) -> dict:
    """{platform: {"email": sha256, "phone": sha256}} with each platform's normalization. Only hashes."""
    out = {}
    for p in WS_PLATFORMS:
        em, ph = norm_email(email, p), norm_phone(phone, p)
        d = {k: _sha(v) for k, v in (("email", em), ("phone", ph)) if v}
        if d:
            out[p] = d
    return out


# ---------------------------------------------------------------- destinations

def destinations(conn, ws_id: int) -> dict:
    """The workspace's destinations: Meta pixels of active connections, TikTok pixels added by hand, Google
    conversion actions (ENABLED) of active Google connections."""
    ws_id = int(ws_id)
    meta = conn.execute("SELECT p.*, c.id AS conn_id, c.token_enc AS conn_token FROM pixels p JOIN ad_connections c "
                        "ON c.id=p.connection_id AND c.workspace_id=p.workspace_id WHERE p.workspace_id=? AND "
                        "p.platform='meta' AND c.platform='meta' AND c.status='active' ORDER BY p.id", (ws_id,)).fetchall()
    tiktok = conn.execute("SELECT * FROM pixels WHERE workspace_id=? AND platform='tiktok' ORDER BY id", (ws_id,)).fetchall()
    google = conn.execute("SELECT a.*, c.business_id AS login_id, c.scopes AS conn_scopes, c.token_enc AS conn_token "
                          "FROM conversion_actions a JOIN ad_connections c ON c.id=a.connection_id AND "
                          "c.workspace_id=a.workspace_id WHERE a.workspace_id=? AND c.platform='google' AND "
                          "c.status='active' AND a.status='ENABLED' ORDER BY a.id", (ws_id,)).fetchall()
    from adplatforms import google as gads_api
    return {"meta": [r for r in meta if _META_PIXEL_RE.match(r["external_pixel_id"] or "")],
            "tiktok": [r for r in tiktok if _TIKTOK_CODE_RE.match(r["external_pixel_id"] or "")],
            "google": google if gads_api.flag_on() else []}       # Google also needs GOOGLE_ADS


def has_datamanager(row) -> bool:
    return DATAMANAGER_SCOPE in str(row["conn_scopes"] or "").split()


def server_ready(platform: str, row, event: str) -> bool:
    """Would the server send `event` to this destination (a token or the right action and scope)?"""
    if event not in WS_EVENTS.get(platform, {}):
        return False
    if platform == "meta":
        return bool(row["capi_token_enc"]) or (meta_connection_token_ok() and bool(row["conn_token"]))
    if platform == "tiktok":
        return bool(row["capi_token_enc"])
    if platform == "google":
        from adplatforms import google
        return google.flag_on() and row["kind"] == "lead_server" and bool(row["conn_token"]) and has_datamanager(row)
    return False


def browser_config(dests: dict) -> dict:
    """What static/pagepx.js loads: pixel ids only (validated: they end up in a script's JSON)."""
    cfg = {}
    ids = [r["external_pixel_id"] for r in dests.get("meta") or []]
    if ids:
        cfg["meta"] = sorted(set(ids))
    codes = [r["external_pixel_id"] for r in dests.get("tiktok") or []]
    if codes:
        cfg["tiktok"] = sorted(set(codes))
    return cfg


def tracking(conn, ws, setting, req, preview: bool = False) -> dict | None:
    """Conversion tracking for one render of a member's page or hosted form, or None (flag, consent, preview,
    or no destination). {"nonce", "browser" (pagepx.js config, with "pv"), "dests", "pv"}."""
    try:
        if preview or not ws_enabled(ws) or not measured(req, setting):
            return None
        dests = destinations(conn, ws["id"])
        if not any(dests.values()):
            return None
        import site_events
        pv = site_events.new_event_id()
        browser = browser_config(dests)
        if browser:
            browser["pv"] = pv
        return {"nonce": secrets.token_urlsafe(18), "browser": browser, "dests": dests, "pv": pv}
    except Exception as e:  # noqa: BLE001 — measurement never breaks a page
        log.warning("page tracking skipped: %s", str(e)[:150])
        return None


def form_fields(req) -> dict:
    """Hidden fields one form on a tracked page carries: its lead's event id and the visit's ad click ids."""
    import site_events
    out = {"_eid": site_events.new_event_id()}
    for k, v in site_events.clicks_of(req.args).items():
        out["_" + k] = v
    return out


# ---------------------------------------------------------------- queueing

def _visit_ctx(req, path: str, clicks: dict) -> dict:
    ck = req.cookies
    fbc = ck.get("_fbc") or (f"fb.1.{int(time.time() * 1000)}.{clicks['fbclid']}" if clicks.get("fbclid") else None)
    return {k: v for k, v in {
        "ip": (req.remote_addr or "")[:64], "ua": (req.user_agent.string or "")[:400],
        "url": (req.host_url.rstrip("/") + path)[:300],          # never the query string
        "fbp": ck.get("_fbp"), "fbc": fbc, "ttp": ck.get("_ttp"), "ttclid": clicks.get("ttclid"),
        "gclid": clicks.get("gclid"),
    }.items() if v}


def ws_queue(conn, ws_id: int, platform: str, dest_id: int, payload: dict) -> int:
    try:
        n = conn.execute("INSERT OR IGNORE INTO ws_conversion_queue (workspace_id, platform, dest_id, event, event_id, "
                         "payload, created_at) VALUES (?,?,?,?,?,?,?)",
                         (int(ws_id), platform, int(dest_id), payload["event"], payload["event_id"], json.dumps(payload),
                          db.now())).rowcount
        conn.commit()
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("page conversion queue error: %s", str(e)[:120])
        return 0


def on_page_view(conn, ws_id: int, tr: dict | None, req, *, page_id=None, form_id=None) -> int:
    """A counted view of a tracked page (or hosted form): the collector row, then PageView to every destination
    that takes it server side, with the id the browser fires. Returns rows queued."""
    if not tr:
        return 0
    try:
        import site_events
        site_events.record_page_event(conn, ws_id, "PageView", tr["pv"], page_id=page_id, form_id=form_id,
                                      path=req.path, click=site_events.click_of(req.args))
        ctx = _visit_ctx(req, req.path, site_events.clicks_of(req.args))
        payload = {"event": "PageView", "event_id": tr["pv"], "ts": int(time.time()), "ctx": ctx}
        n = sum(ws_queue(conn, ws_id, platform, row["id"], payload)
                for platform in ("meta", "tiktok") for row in tr["dests"][platform]
                if server_ready(platform, row, "PageView"))
        if n:
            flush_ws_soon()
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("page view conversion skipped: %s", str(e)[:150])
        return 0


def on_lead(conn, ws, req, *, form, page=None, submission_id=None, email="", phone="") -> str | None:
    """A form submitted on a member's page (or its hosted form). Records the Lead with the event id the form
    carried (_eid; a new one if missing or someone else's), queues it for every destination with hashed email
    and phone, and returns the event id for the thank-you page's browser copy. None when not measured."""
    try:
        setting = page["measurement"] if page is not None else form["measurement"]
        if not ws_enabled(ws) or not measured(req, setting):
            return None
        dests = destinations(conn, ws["id"])
        if not any(dests.values()):
            return None
        import site_events
        f = req.form
        clicks = site_events.clicks_of(f)
        path = f"/p/{page['slug']}" if page is not None else f"/f/{form['code']}"
        hashes = lead_hashes(email, phone)
        has_tag = any(r["kind"] == "lead" and r["tag_id"] for r in dests["google"])
        keep = {"google": hashes["google"]} if has_tag and hashes.get("google") else None
        common = dict(page_id=page["id"] if page is not None else None, form_id=form["id"], submission_id=submission_id,
                      path=path, click=site_events.click_of(f), hashes=keep)
        eid = site_events.clean_event_id(f.get("_eid"))
        if eid and not site_events.record_page_event(conn, ws["id"], "Lead", eid, **common):
            have = conn.execute("SELECT workspace_id, form_id FROM page_events WHERE event_id=?", (eid,)).fetchone()
            if have is not None and have["workspace_id"] == ws["id"] and have["form_id"] == form["id"]:
                return None           # the same form sent twice: one lead was counted already
            eid = None
        if not eid:
            eid = site_events.new_event_id()
            if not site_events.record_page_event(conn, ws["id"], "Lead", eid, **common):
                return None
        ctx = _visit_ctx(req, path, clicks)
        n = 0
        for platform in WS_PLATFORMS:
            for row in dests[platform]:
                if not server_ready(platform, row, "Lead"):
                    continue
                user = hashes.get(platform) or {}
                if platform == "google" and not (user or ctx.get("gclid")):
                    continue          # Data Manager needs an identifier
                n += ws_queue(conn, ws["id"], platform, row["id"], {"event": "Lead", "event_id": eid,
                                                                     "ts": int(time.time()), "ctx": ctx, "user": user})
        if n:
            flush_ws_soon()
        return eid
    except Exception as e:  # noqa: BLE001 — a lead is saved whatever happens here
        log.warning("lead conversion skipped: %s", str(e)[:150])
        return None


# ---------------------------------------------------------------- the calls

def _rfc3339(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ws_build(platform: str, p: dict, dest, secret: str) -> dict:
    """{"url", "params", "headers", "json"} for one queued event. Pure: the tests check the shapes.
    `secret`: Meta CAPI token, TikTok Events API token, or a Google access token."""
    ctx, user = p.get("ctx") or {}, p.get("user") or {}
    name = WS_EVENTS[platform][p["event"]]
    if platform == "meta":
        from adplatforms import meta
        ud = {"em": [user["email"]] if user.get("email") else None, "ph": [user["phone"]] if user.get("phone") else None,
              "client_ip_address": ctx.get("ip"), "client_user_agent": ctx.get("ua"), "fbc": ctx.get("fbc"),
              "fbp": ctx.get("fbp")}
        ev = {"event_name": name, "event_time": p["ts"], "event_id": p["event_id"], "action_source": "website",
              "event_source_url": ctx.get("url") or "", "user_data": {k: v for k, v in ud.items() if v}}
        body = {"data": [ev]}
        if dest["test_code"]:
            body["test_event_code"] = dest["test_code"]
        return {"url": f"{meta.GRAPH}/{dest['external_pixel_id']}/events", "params": {"access_token": secret},
                "headers": {}, "json": body}
    if platform == "tiktok":
        u = {"email": user.get("email"), "phone": user.get("phone"), "ip": ctx.get("ip"), "user_agent": ctx.get("ua"),
             "ttclid": ctx.get("ttclid"), "ttp": ctx.get("ttp")}
        ev = {"event": name, "event_time": p["ts"], "event_id": p["event_id"], "user": {k: v for k, v in u.items() if v},
              "page": {"url": ctx.get("url") or ""}}
        body = {"event_source": "web", "event_source_id": dest["external_pixel_id"], "data": [ev]}
        if dest["test_code"]:
            body["test_event_code"] = dest["test_code"]
        return {"url": TIKTOK_EVENTS_URL, "params": None, "headers": {"Access-Token": secret}, "json": body}
    if platform == "google":
        ids = [{"emailAddress": user["email"]}] if user.get("email") else []
        ids += [{"phoneNumber": user["phone"]}] if user.get("phone") else []
        ev = {"transactionId": p["event_id"], "eventTimestamp": _rfc3339(p["ts"]), "eventSource": "WEB"}
        if ctx.get("gclid"):
            ev["adIdentifiers"] = {"gclid": ctx["gclid"]}
        if ids:
            ev["userData"] = {"userIdentifiers": ids}
        dest_obj = {"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": str(dest["customer_id"])},
                    "productDestinationId": str(dest["external_id"])}
        if dest["login_id"]:
            dest_obj["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": re.sub(r"\D", "", str(dest["login_id"]))}
        body = {"destinations": [dest_obj], "encoding": "HEX", "events": [ev],
                "validateOnly": (os.environ.get("GOOGLE_DATAMANAGER_VALIDATE_ONLY") or "").strip() == "1"}
        return {"url": DATA_MANAGER_URL, "params": None, "headers": {"Authorization": f"Bearer {secret}"}, "json": body}
    raise ValueError(platform)


def _dest_row(conn, platform: str, dest_id: int, ws_id: int):
    if platform in ("meta", "tiktok"):
        return conn.execute("SELECT p.*, c.token_enc AS conn_token, c.status AS conn_status FROM pixels p LEFT JOIN "
                            "ad_connections c ON c.id=p.connection_id WHERE p.id=? AND p.workspace_id=? AND p.platform=?",
                            (dest_id, ws_id, platform)).fetchone()
    return conn.execute("SELECT a.*, c.business_id AS login_id, c.scopes AS conn_scopes, c.token_enc AS conn_token, "
                        "c.status AS conn_status, c.workspace_id AS conn_ws FROM conversion_actions a JOIN ad_connections c "
                        "ON c.id=a.connection_id WHERE a.id=? AND a.workspace_id=?", (dest_id, ws_id)).fetchone()


def _secret(platform: str, row) -> str:
    """The credential for one send, opened from its token box here and never kept."""
    import secretsbox
    if platform in ("meta", "tiktok") and row["capi_token_enc"]:
        return secretsbox.open_box(row["capi_token_enc"], secretsbox.aad_for("pixels", row["workspace_id"]))
    if platform == "meta" and meta_connection_token_ok() and row["conn_token"]:
        return secretsbox.open_box(row["conn_token"], secretsbox.aad_for("ad_connections", row["workspace_id"]))
    if platform == "google":
        from adplatforms import google
        refresh = secretsbox.open_box(row["conn_token"], secretsbox.aad_for("ad_connections", row["workspace_id"]))
        return google.access_token(refresh)
    raise secretsbox.BoxError("no token")


def _kind(e) -> str:
    """The kind of failure, first in the stored error, so Settings can say what to do (conversion_routes)."""
    import adhttp
    if isinstance(e, adhttp.TokenInvalid):
        return "token"
    if isinstance(e, adhttp.PermissionDenied):
        return "permission"
    if isinstance(e, adhttp.NetworkError):
        return "network"
    if isinstance(e, adhttp.RateLimited):
        return "rate"
    if e.platform == "tiktok" and any(c in str(e) for c in ('"code": 40105', '"code": 40104', '"code": 40001')):
        return "token"            # TikTok answers 200 with its own code for a bad token
    if e.platform == "google" and "PERMISSION_DENIED" in str(e):
        return "permission"
    return "refused"


def _ws_send(conn, platform: str, payload: dict, row) -> tuple[bool, str]:
    import adhttp
    import secretsbox
    if row is None or row["conn_status"] not in (None, "active") or not server_ready(platform, row, payload["event"]):
        return False, "dropped"
    try:
        secret = _secret(platform, row)
    except (secretsbox.BoxError, secretsbox.NotConfigured):
        return False, "dropped"
    except adhttp.AdHttpError as e:
        return False, f"{_kind(e)}: google access: {str(e)}"[:300]
    req = ws_build(platform, payload, row, secret)
    try:
        body = adhttp.call(platform, "POST", req["url"], params=req["params"], headers=req["headers"],
                           json_body=req["json"], retries=1, secrets=[secret])
    except adhttp.AdHttpError as e:
        return False, f"{_kind(e)}: {str(e)}"[:300]
    if platform == "meta" and int((body or {}).get("events_received") or 0) < 1:
        return False, "meta: no event received"
    return True, ""


def flush_ws(conn, limit: int = 25) -> dict:
    """Send members' queued events (and retry failures, up to WS_MAX_ATTEMPTS, for 3 days). Claimed before sending,
    so two processes never send one twice. A sent row keeps no IP address or user agent."""
    st = {"sent": 0, "failed": 0, "dropped": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat()
    rows = conn.execute("SELECT * FROM ws_conversion_queue WHERE status IN ('pending','failed') AND attempts<? AND "
                        "created_at>? ORDER BY id LIMIT ?", (WS_MAX_ATTEMPTS, since, limit)).fetchall()
    for r in rows:
        cur = conn.execute("UPDATE ws_conversion_queue SET status='sending', attempts=attempts+1 WHERE id=? AND "
                           "status IN ('pending','failed')", (r["id"],))
        conn.commit()
        if not cur.rowcount:
            continue
        payload = json.loads(r["payload"])
        try:
            ok, note = _ws_send(conn, r["platform"], payload, _dest_row(conn, r["platform"], r["dest_id"], r["workspace_id"]))
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"error: {type(e).__name__}"
        status = "sent" if ok else ("dropped" if note == "dropped" else "failed")
        if ok:
            ctx = payload.get("ctx") or {}
            payload["ctx"] = {k: v for k, v in ctx.items() if k not in ("ip", "ua")}
        conn.execute("UPDATE ws_conversion_queue SET status=?, error=?, sent_at=?, payload=? WHERE id=?",
                     (status, None if ok else note[:300], db.now() if ok else None, json.dumps(payload), r["id"]))
        conn.commit()
        st[status] += 1
    conn.execute("UPDATE ws_conversion_queue SET status='failed', error='interrupted' WHERE status='sending' AND "
                 "created_at<?", ((datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat(),))
    conn.commit()
    return st


def flush_ws_soon() -> None:
    if not ws_flag_on() or os.environ.get("RS_CAPI_SYNC") == "1":
        return

    def run():
        try:
            with db.connect() as c:
                flush_ws(c)
        except Exception as e:  # noqa: BLE001
            log.warning("page conversion flush error: %s", str(e)[:150])
    threading.Thread(target=run, daemon=True, name="ws-capi-flush").start()


def stats(conn, ws_id: int, days: int = 7) -> dict:
    """{(platform, dest_id): {"sent", "failed", "last_error", "last_sent"}} for the Settings page."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    out = {}
    for r in conn.execute("SELECT platform, dest_id, status, error, sent_at FROM ws_conversion_queue WHERE "
                          "workspace_id=? AND created_at>? ORDER BY id", (int(ws_id), since)):
        d = out.setdefault((r["platform"], r["dest_id"]), {"sent": 0, "failed": 0, "last_error": None, "last_sent": None})
        if r["status"] == "sent":
            d["sent"] += 1
            d["last_sent"] = r["sent_at"]
            d["last_error"] = None
        elif r["status"] == "failed" and r["error"] != "interrupted":
            d["failed"] += 1
            d["last_error"] = r["error"]
    return out


def add_lead(track: dict, lead) -> dict:
    """The thank-you render: the browser fires Lead with the server's event id (site_events.lead_for_browser)."""
    if track and track.get("browser"):
        track["browser"]["lead"] = lead["event_id"]
    return track
