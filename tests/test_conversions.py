"""Conversion tracking on members' Campaignify pages and forms (roadmap M3.4: conversions.py, site_events.py,
conversion_routes.py, static/pagepx.js) on a throwaway database. Meta, TikTok and Google are fakes (tests/adfake.py,
tests/fakegoogleads.py) answering from tests/fixtures/{meta,tiktok,google}/*.json, hand-written from each API's
reference (Meta Conversions API, TikTok Events API 2.0, Google Data Manager API, read 2026-11-09): no request
leaves the machine. tools/conversions_check.py re-checks the shapes against the live APIs.

    python tests/test_conversions.py      (tests/test_app.py runs it too)

  * CONVERSIONS_API off (the default) or AD_LAUNCH off: no script on pages, the strict CSP, Settings is a 404
  * a measured visit (US, no GPC / DNT, a browser, published page, measurement on): one UUID per page view, the
    pixel config and pagepx.js under a nonce CSP with only the connected platforms' hosts, hidden _eid and click
    ids in forms; the server copy of PageView with the same id (once per visitor per 30 minutes)
  * not measured: another country, no country, GPC, Do Not Track, a bot, the preview, measurement off
  * a lead: the form's _eid becomes the event id everywhere (Meta event_id, TikTok event_id, Google transactionId),
    email and phone only as SHA-256 per platform's rules, the thank-you page fires the browser Lead once, a resent
    form counts once, someone else's or a made-up id is replaced
  * Google: Data Manager events:ingest with the gclid and hashed identifiers to the lead_server action; needs the
    datamanager scope and an identifier
  * errors: refused tokens, network, retries up to 4, plain words on Settings; removed destinations are dropped
  * Settings: Meta CAPI token and test code, TikTok pixel, owner only, tokens only in token boxes
  * no token, raw email or raw phone in the queue, the collector, logs, pages or the database file
"""
import atexit
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_conv_", dir=os.environ.get("TMPDIR"))
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0", RS_LOG_JSON="1",
                  RS_DISK_MIN_GB="0.2", RS_CAPI_SYNC="1", TOKEN_KEY=base64.b64encode(b"C" * 32).decode(),
                  GOOGLE_OAUTH_CLIENT_ID="555-fake.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET="GOCSPX-x-1")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "GOOGLE_ADS", "CONVERSIONS_API", "META_CAPI_USE_CONNECTION_TOKEN",
          "TOKEN_KEYS_OLD", "META_CAPI_TOKEN", "TIKTOK_EVENTS_TOKEN", "REDDIT_CAPI_TOKEN", "META_PIXEL_ID",
          "TIKTOK_PIXEL_ID", "GOOGLE_DATAMANAGER_VALIDATE_ONLY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import conversions  # noqa: E402
import db  # noqa: E402
import secretsbox  # noqa: E402
import site_events  # noqa: E402
from adfake import FakeTransport  # noqa: E402
from fakegoogleads import FakeGoogleAds  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


CAP = Capture()
for name in ("", "campaignify", "rs", "werkzeug", "urllib3"):
    lg = logging.getLogger(name)
    lg.addHandler(CAP)
    lg.setLevel(logging.DEBUG)
adhttp.SLEEP = lambda s: None
adhttp.BACKOFF = 0.0
appmod._maybe_reconcile = lambda ws_id, force=False: 0

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1"
US = {"User-Agent": UA, "X-Country": "US"}
META_TOKEN = "EAAGfakeCapiToken1234567890abcdefXYZ"
TIKTOK_TOKEN = "tt1fakeEventsToken0987654321abcdef"
GOOGLE_REFRESH = "1//fake-refresh-conv-secretvalue"
EMAIL, PHONE = "Jo.Doe@Gmail.com", "(512) 555-0142"
_ip = [0]


def client(ip=None):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip or f"10.61.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (u["workspace_id"],))
        conn.commit()
    return c, u


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def run(sql, *args):
    with db.connect() as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def html(r):
    return r.get_data(as_text=True)


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def px_config(page):
    m = re.search(r'<script type="application/json" id="cfy-px" nonce="([^"]+)">(.*?)</script>', page, re.S)
    return (m.group(1), json.loads(m.group(2))) if m else (None, None)


def sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


def flush():
    with db.connect() as conn:
        return conversions.flush_ws(conn)


class Router:
    """One transport for every host: Meta and TikTok from FakeTransport, Google from FakeGoogleAds."""

    def __init__(self, social, google):
        self.social, self.google = social, google

    def __call__(self, method, url, **kw):
        host = urlsplit(url).netloc
        if host in ("graph.facebook.com", "business-api.tiktok.com"):
            return self.social(method, url, **kw)
        return self.google(method, url, **kw)


SOC = FakeTransport()
SOC.on("POST", "/events", "meta/capi_events_ok.json")
SOC.on("POST", "/event/track/", "tiktok/events_ok.json")
G = FakeGoogleAds()
G.customer("1112223334", "Ember Shop")
adhttp.TRANSPORT = Router(SOC, G)

owner, u = signup("conv-owner@example.com")
WS = u["workspace_id"]
other, ou = signup("conv-other@example.com")

# a published page with a form, and a Meta connection with its pixel (as M2.1's connect leaves them)
form_id = run("INSERT INTO forms (workspace_id, code, name, fields_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              WS, "lead1", "Quote", json.dumps([
                  {"key": "name", "label": "Name", "type": "text", "required": False, "options": [], "show_if": None},
                  {"key": "email", "label": "Email", "type": "email", "required": True, "options": [], "show_if": None},
                  {"key": "phone", "label": "Phone", "type": "phone", "required": False, "options": [], "show_if": None}]),
              db.now(), db.now())
page_id = run("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, created_at, updated_at) "
              "VALUES (?,?,?,?,?,?,?,?)", WS, "cool-air", "Cool Air", "", "published",
              json.dumps([{"type": "hero", "headline": "Cool Air AC repair", "subhead": "", "button_text": "", "button_url": "",
                           "media": None}, {"type": "form", "heading": "Get a quote", "form": form_id}]), db.now(), db.now())
meta_conn = run("INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, status, "
                "token_enc, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", WS, "meta", "998877", "Ember Ads", "USD",
                "active", secretsbox.seal("EAAconnectionToken0000000000000", secretsbox.aad_for("ad_connections", WS)),
                db.now(), db.now())
meta_px = run("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) VALUES "
              "(?,?,?,?,?,?)", WS, "meta", meta_conn, "123456789012345", "Ember pixel", db.now())


def visit(path="/p/cool-air", headers=None, ip=None):
    c = client(ip)
    return c, c.get(path, headers=headers if headers is not None else US)


# ================================================================ 1. the flags
print("flags")
c, r = visit()
check(r.status_code == 200 and "cfy-px" not in html(r) and "script-src" not in r.headers["Content-Security-Policy"],
      "CONVERSIONS_API unset (the default): no pixel script, the page keeps the no-scripts CSP")
check("data-measurement" not in html(owner.get(f"/app/pages/{page_id}")), "the page editor shows no measurement box")
os.environ["CONVERSIONS_API"] = "1"
check(owner.get("/app/settings/conversions").status_code == 404, "CONVERSIONS_API on, AD_LAUNCH off: Settings is a 404")
c, r = visit()
check("cfy-px" not in html(r) and not q("SELECT 1 FROM page_events"), "AD_LAUNCH off: nothing on pages either")
os.environ["AD_LAUNCH"] = "on"
s = html(owner.get("/app/settings/conversions"))
check("Conversion tracking" in s and "Ember pixel" in s and "Browser only" in s,
      "both flags: Settings lists the Meta pixel from the connection, browser only without a token")
check("data-conversions-link" in html(owner.get("/app/settings/ad-accounts")), "Connected ad accounts links to it")
check(not re.search(r"[–—]", re.sub(r"<[^>]+>", " ", s)), "no em or en dashes on Settings")

# ================================================================ 2. a measured visit, browser only
print("measured visit")
c, r = visit("/p/cool-air?utm_source=facebook&fbclid=IwAR0fakeClick123&gclid=Cj0fakeGclid456")
page = html(r)
nonce, cfg = px_config(page)
csp = r.headers["Content-Security-Policy"]
check(cfg is not None and cfg["meta"] == ["123456789012345"] and "tiktok" not in cfg and "google" not in cfg,
      "the config has the connected Meta pixel id and nothing else")
check(site_events.clean_event_id(cfg["pv"]) == cfg["pv"], "the page view has a UUID (version 4) event id")
check(f"'nonce-{nonce}'" in csp and "https://connect.facebook.net" in csp and "analytics.tiktok.com" not in csp
      and "googletagmanager" not in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp,
      "the CSP allows this render's nonce and Meta's hosts only, the rest stays strict")
check(f'src="/static/pagepx.js" nonce="{nonce}"' in page, "pagepx.js loads under the nonce")
eid = re.search(r'name="_eid" value="([^"]+)"', page).group(1)
check(site_events.clean_event_id(eid) == eid and eid != cfg["pv"], "the form carries its own lead event id (_eid)")
check('name="_fbclid" value="IwAR0fakeClick123"' in page and 'name="_gclid" value="Cj0fakeGclid456"' in page,
      "the form carries the visit's ad click ids")
ev = q("SELECT * FROM page_events WHERE event_id=?", cfg["pv"])
check(len(ev) == 1 and ev[0]["name"] == "PageView" and ev[0]["page_id"] == page_id and ev[0]["path"] == "/p/cool-air"
      and ev[0]["click_type"] == "gclid", "the collector keeps the page view: id, page, path without query, click id")
check(not q("SELECT 1 FROM ws_conversion_queue"), "no server copy without a Conversions API token")
r2 = c.get("/p/cool-air", headers=US)
_, cfg2 = px_config(html(r2))
check(cfg2["pv"] != cfg["pv"] and len(q("SELECT 1 FROM page_events WHERE name='PageView'")) == 1,
      "a reload gets a new browser id; the server side counts a visitor once per 30 minutes")
js = open(os.path.join(os.path.dirname(HERE), "static", "pagepx.js")).read()
check("{eventID: C.pv}" in js and "{eventID: C.lead}" in js and "{event_id: C.pv}" in js and "{event_id: C.lead}" in js
      and "globalPrivacyControl" in js and "autoConfig" in js,
      "pagepx.js fires each event with the server's id, re-checks GPC / DNT, and turns off Meta's form scraping")

# ================================================================ 3. not measured
print("not measured")
for label, hdrs in (("a visitor outside the US", {"User-Agent": UA, "X-Country": "DE"}),
                    ("no country from the edge", {"User-Agent": UA}),
                    ("Global Privacy Control", dict(US, **{"Sec-GPC": "1"})),
                    ("Do Not Track", dict(US, DNT="1")),
                    ("a bot", {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)", "X-Country": "US"})):
    n = len(q("SELECT 1 FROM page_events"))
    _, r = visit(headers=hdrs)
    check("cfy-px" not in html(r) and "script-src" not in r.headers["Content-Security-Policy"]
          and "_eid" not in html(r) and len(q("SELECT 1 FROM page_events")) == n, f"{label}: no script, no id, no event")
r = owner.get(f"/app/pages/{page_id}/preview", headers=US)
check("cfy-px" not in html(r), "the editor's preview is never measured")
pe = html(owner.get(f"/app/pages/{page_id}"))
check("data-measurement" in pe and "Measure ad results on this page" in pe and "US only" in pe,
      "the page editor shows the measurement box, US only")
r = owner.post(f"/app/pages/{page_id}", data={"csrf": csrf(owner), "action": "save", "title": "Cool Air", "slug": "cool-air",
                                             "b0_type": "hero", "b0_headline": "Cool Air AC repair", "b1_type": "form",
                                             "b1_form": str(form_id), "measure_form": "1"})
check(q("SELECT measurement FROM pages WHERE id=?", page_id)[0][0] == "off", "unticking it saves measurement off")
_, r = visit()
check("cfy-px" not in html(r), "measurement off: no script")
owner.post(f"/app/pages/{page_id}", data={"csrf": csrf(owner), "action": "save", "title": "Cool Air", "slug": "cool-air",
                                          "b0_type": "hero", "b0_headline": "Cool Air AC repair", "b1_type": "form",
                                          "b1_form": str(form_id), "measure_form": "1", "measurement": "us"})
check(q("SELECT measurement FROM pages WHERE id=?", page_id)[0][0] == "us", "ticking it saves measurement on (US)")

# ================================================================ 4. Settings: tokens
print("settings")
member, mu = signup("conv-member@example.com")
run("UPDATE users SET workspace_id=?, role='member' WHERE id=?", WS, mu["id"])
check(member.post(f"/app/settings/conversions/meta/{meta_px}", data={"csrf": csrf(member), "token": META_TOKEN}).status_code == 403,
      "a member can't save a token (403)")
check(other.post(f"/app/settings/conversions/meta/{meta_px}", data={"csrf": csrf(other), "token": META_TOKEN}).status_code == 404,
      "another workspace's pixel is a 404")
owner.post(f"/app/settings/conversions/meta/{meta_px}", data={"csrf": csrf(owner), "token": "short"})
check("doesn't look right" in flashes(owner) and not q("SELECT capi_token_enc FROM pixels WHERE id=?", meta_px)[0][0],
      "a malformed token is refused")
owner.post(f"/app/settings/conversions/meta/{meta_px}", data={"csrf": csrf(owner), "token": META_TOKEN, "test_code": "TEST4242"})
row = q("SELECT * FROM pixels WHERE id=?", meta_px)[0]
check(row["capi_token_enc"].startswith("cfy1:") and META_TOKEN not in row["capi_token_enc"] and row["test_code"] == "TEST4242",
      "the token is kept only in a token box; the Test events code is saved")
s = html(owner.get("/app/settings/conversions"))
check(META_TOKEN not in s and "Server events on" in s and "Remove token" in s, "Settings never shows the token again")
owner.post("/app/settings/conversions/tiktok", data={"csrf": csrf(owner), "pixel_code": "bad code", "token": TIKTOK_TOKEN})
check("pixel code" in flashes(owner) and not q("SELECT 1 FROM pixels WHERE platform='tiktok'"), "a bad TikTok pixel code is refused")
owner.post("/app/settings/conversions/tiktok", data={"csrf": csrf(owner), "pixel_code": "c4ab12cd34ef56gh78ij",
                                                     "token": TIKTOK_TOKEN, "name": "Ember TikTok"})
tt = q("SELECT * FROM pixels WHERE platform='tiktok'")
check(len(tt) == 1 and tt[0]["external_pixel_id"] == "C4AB12CD34EF56GH78IJ" and tt[0]["connection_id"] is None
      and TIKTOK_TOKEN not in (tt[0]["capi_token_enc"] or ""), "a TikTok pixel is added with its token boxed")
TT_ID = tt[0]["id"]

# ================================================================ 5. page view, server side
print("page view, server side")
c, r = visit("/p/cool-air?ttclid=E.C.P.fakeTtclid789", headers=dict(US, Cookie="_fbp=fb.1.1700000000000.42; _ttp=ttpCookie1"),
             ip="10.77.0.1")
c.set_cookie("_fbp", "fb.1.1700000000000.42")
nonce, cfg = px_config(html(r))
check(cfg["meta"] == ["123456789012345"] and cfg["tiktok"] == ["C4AB12CD34EF56GH78IJ"]
      and "https://analytics.tiktok.com" in r.headers["Content-Security-Policy"], "both pixels in the config and the CSP")
rows = q("SELECT * FROM ws_conversion_queue WHERE event='PageView' ORDER BY platform")
check([x["platform"] for x in rows] == ["meta", "tiktok"] and all(x["event_id"] == cfg["pv"] for x in rows),
      "PageView is queued for Meta and TikTok with the browser's id")
st = flush()
check(st["sent"] == 2, "both are sent")
mcall = SOC.find("POST", "/123456789012345/events")[-1]
body = mcall["data"]
check(mcall["params"]["access_token"] == META_TOKEN and mcall["path"] == "/v26.0/123456789012345/events",
      "Meta: /<pixel>/events on the Graph version meta.py uses, with the pixel's own token")
ev0 = body["data"][0] if "data" in body else None
check(ev0 is not None and ev0["event_name"] == "PageView" and ev0["event_id"] == cfg["pv"] and ev0["action_source"] == "website"
      and ev0["event_source_url"] == "http://localhost/p/cool-air" and body.get("test_event_code") == "TEST4242"
      and ev0["user_data"]["client_ip_address"] == "10.77.0.1" and "em" not in ev0["user_data"],
      "Meta PageView: name, the same event id, website, the address without its query, Test events code, no email")
tcall = SOC.find("POST", "/event/track/")[-1]
tb = tcall["data"]
check(tcall["headers"]["Access-Token"] == TIKTOK_TOKEN and tb["event_source"] == "web"
      and tb["event_source_id"] == "C4AB12CD34EF56GH78IJ" and tb["data"][0]["event"] == "ViewContent"
      and tb["data"][0]["event_id"] == cfg["pv"] and tb["data"][0]["user"]["ttclid"] == "E.C.P.fakeTtclid789",
      "TikTok: event/track with the pixel code, ViewContent, the same event id and the ttclid")
sent = q("SELECT payload FROM ws_conversion_queue WHERE status='sent'")
check(all('"ip"' not in x[0] and '"ua"' not in x[0] for x in sent), "a sent row keeps no IP address or user agent")

# ================================================================ 6. a lead
print("lead")
c, r = visit("/p/cool-air?fbclid=IwAR0leadClick", ip="10.77.0.2")
page = html(r)
eid = re.search(r'name="_eid" value="([^"]+)"', page).group(1)
stamp = re.search(r'name="_t" value="([^"]+)"', page).group(1)
time.sleep(2.1)
form = {"_t": stamp, "_page": "cool-air", "_eid": eid, "_fbclid": "IwAR0leadClick", "f_name": "Jo",
        "f_email": EMAIL, "f_phone": PHONE}
r = c.post("/f/lead1", data=form, headers=US)
loc = r.headers.get("Location", "")
check(r.status_code == 303 and f"ev={eid}" in loc and "sent=lead1" in loc, "the lead redirects to the thank-you page with its id")
leads = q("SELECT * FROM ws_conversion_queue WHERE event='Lead' ORDER BY platform")
check([x["platform"] for x in leads] == ["meta", "tiktok"] and all(x["event_id"] == eid for x in leads),
      "the Lead is queued for Meta and TikTok with the form's id")
pm = json.loads(leads[0]["payload"])
pt = json.loads(leads[1]["payload"])
check(pm["user"] == {"email": sha("jo.doe@gmail.com"), "phone": sha("15125550142")},
      "Meta gets SHA-256 of the lower-case email and of the phone's digits with the country code")
check(pt["user"] == {"email": sha("jo.doe@gmail.com"), "phone": sha("+15125550142")}, "TikTok gets the phone in E.164")
check(pm["ctx"]["fbc"].startswith("fb.1.") and pm["ctx"]["fbc"].endswith(".IwAR0leadClick"), "fbc made from the fbclid")
allq = " ".join(x[0] for x in q("SELECT payload FROM ws_conversion_queue")) + " ".join(
    str(tuple(x)) for x in q("SELECT * FROM page_events"))
check(EMAIL.lower() not in allq.lower() and "5125550142" not in allq and "jo.doe" not in allq.lower(),
      "no raw email or phone in the queue or the collector")
flush()
mb = SOC.find("POST", "/123456789012345/events")[-1]["data"]
check(mb["data"][0]["event_name"] == "Lead" and mb["data"][0]["user_data"]["em"] == [sha("jo.doe@gmail.com")]
      and mb["data"][0]["user_data"]["ph"] == [sha("15125550142")] and mb["data"][0]["event_id"] == eid,
      "Meta Lead: em and ph hashed, the form's event id")
tb = SOC.find("POST", "/event/track/")[-1]["data"]
check(tb["data"][0]["event"] == "SubmitForm" and tb["data"][0]["user"]["email"] == sha("jo.doe@gmail.com"),
      "TikTok SubmitForm with the hashed email")
outbound = " ".join(json.dumps(x["data"]) + json.dumps(x["params"]) for x in SOC.calls)
check(EMAIL.lower() not in outbound.lower() and "5125550142" not in outbound, "no raw email or phone ever leaves")
r = c.get(loc, headers=US)
_, cfg = px_config(html(r))
check(cfg["lead"] == eid and "Thanks" in html(r), "the thank-you page fires the browser Lead with the same id")
r = c.get(loc, headers=US)
_, cfg = px_config(html(r))
check("lead" not in cfg, "a reload doesn't fire it again")
_, cfg = px_config(html(client().get(f"/p/cool-air?sent=lead1&ev={site_events.new_event_id()}", headers=US)))
check("lead" not in cfg, "a made-up ?ev= fires nothing")
n = len(q("SELECT 1 FROM ws_conversion_queue WHERE event='Lead'"))
time.sleep(0.1)
c.post("/f/lead1", data=form, headers=US)
check(len(q("SELECT 1 FROM ws_conversion_queue WHERE event='Lead'")) == n and
      len(q("SELECT 1 FROM form_submissions")) == 2, "the same form sent twice: saved twice, counted once")
c3, r = visit(ip="10.77.0.3")
p3 = html(r)
form3 = dict(form, _t=re.search(r'name="_t" value="([^"]+)"', p3).group(1), _eid="not-a-uuid", f_email="sam@example.com")
time.sleep(2.1)
loc3 = c3.post("/f/lead1", data=form3, headers=US).headers["Location"]
new = parse_qs(urlsplit(loc3).query)["ev"][0]
check(site_events.clean_event_id(new) == new and new != eid, "a missing or malformed _eid gets a fresh UUID")
c4, r = visit(headers={"User-Agent": UA, "X-Country": "CA"}, ip="10.77.0.4")
n = len(q("SELECT 1 FROM ws_conversion_queue WHERE event='Lead'"))
c4.post("/f/lead1", data=dict(form, _eid=site_events.new_event_id(), f_email="ca@example.com"),
        headers={"User-Agent": UA, "X-Country": "CA"})
check(len(q("SELECT 1 FROM ws_conversion_queue WHERE event='Lead'")) == n, "a lead from outside the US isn't sent")

# ================================================================ 7. Google: Data Manager for leads
print("google data manager")
G.grant(GOOGLE_REFRESH, "108700000000000000077", access=["1112223334"],
        scope="openid email https://www.googleapis.com/auth/adwords https://www.googleapis.com/auth/datamanager")
gconn = run("INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, status, token_enc, "
            "scopes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", WS, "google", "1112223334", "Ember Shop", "USD",
            "active", secretsbox.seal(GOOGLE_REFRESH, secretsbox.aad_for("ad_connections", WS)),
            "email https://www.googleapis.com/auth/adwords https://www.googleapis.com/auth/datamanager openid", db.now(), db.now())
ca = G.conversion_action("1112223334", "Campaignify lead (server)", typ="UPLOAD_CLICKS", primary=False)
gact = run("INSERT INTO conversion_actions (workspace_id, connection_id, customer_id, kind, external_id, resource_name, name, "
           "status, created_by_us, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", WS, gconn, "1112223334",
           "lead_server", ca["id"], ca["rn"], ca["name"], "ENABLED", 1, db.now(), db.now())
c4, r = visit("/p/cool-air?gclid=Cj0offGclid", ip="10.77.0.44")
p4 = html(r)
time.sleep(2.1)
c4.post("/f/lead1", data=dict(form, _t=re.search(r'name="_t" value="([^"]+)"', p4).group(1),
                               _eid=re.search(r'name="_eid" value="([^"]+)"', p4).group(1), _gclid="Cj0offGclid",
                               f_email="off@example.com"), headers=US)
check(not q("SELECT 1 FROM ws_conversion_queue WHERE platform='google'"), "GOOGLE_ADS off: nothing goes to Google")
os.environ["GOOGLE_ADS"] = "1"
c5, r = visit("/p/cool-air?gclid=Cj0leadGclid", ip="10.77.0.5")
p5 = html(r)
_, cfg5 = px_config(p5)
check("google" not in cfg5 and "googletagmanager" not in r.headers["Content-Security-Policy"],
      "an upload-only Google action adds no browser script")
eid5 = re.search(r'name="_eid" value="([^"]+)"', p5).group(1)
time.sleep(2.1)
c5.post("/f/lead1", data=dict(form, _t=re.search(r'name="_t" value="([^"]+)"', p5).group(1), _eid=eid5,
                               _gclid="Cj0leadGclid", _fbclid=""), headers=US)
gq = q("SELECT * FROM ws_conversion_queue WHERE platform='google'")
check(len(gq) == 1 and gq[0]["event_id"] == eid5 and gq[0]["dest_id"] == gact, "the lead is queued for Google")
flush()
check(len(G.ingested) == 1, "Data Manager accepted it")
b = G.ingested[0]
e = b["events"][0]
check(b["destinations"][0] == {"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": "1112223334"},
                               "productDestinationId": ca["id"]} and b["encoding"] == "HEX",
      "events:ingest to the customer, the lead_server action as productDestinationId, HEX encoding")
check(e["transactionId"] == eid5 and e["adIdentifiers"] == {"gclid": "Cj0leadGclid"} and e["eventSource"] == "WEB"
      and {"emailAddress": sha("jodoe@gmail.com")} in e["userData"]["userIdentifiers"]
      and {"phoneNumber": sha("+15125550142")} in e["userData"]["userIdentifiers"],
      "transactionId is the event id, the gclid, Gmail dots dropped before hashing, the phone in E.164")
dm = G.find("/v1/events:ingest")[-1]
check(dm["headers"]["Authorization"].startswith("Bearer ya29.") and GOOGLE_REFRESH not in json.dumps(dm),
      "an access token minted from the boxed refresh token; the refresh token never goes to Data Manager")
run("UPDATE ad_connections SET scopes=? WHERE id=?", "openid email https://www.googleapis.com/auth/adwords", gconn)
c6, r = visit("/p/cool-air?gclid=Cj0noScope", ip="10.77.0.6")
p6 = html(r)
time.sleep(2.1)
c6.post("/f/lead1", data=dict(form, _t=re.search(r'name="_t" value="([^"]+)"', p6).group(1),
                               _eid=re.search(r'name="_eid" value="([^"]+)"', p6).group(1), _gclid="Cj0noScope",
                               f_email="ns@example.com"), headers=US)
check(len(q("SELECT 1 FROM ws_conversion_queue WHERE platform='google'")) == 1,
      "without the datamanager scope nothing is queued for Google")
os.environ.pop("GOOGLE_ADS")
check("data-google-actions" not in html(owner.get("/app/settings/conversions")), "GOOGLE_ADS off: Settings shows no Google panel")
os.environ["GOOGLE_ADS"] = "1"
s = html(owner.get("/app/settings/conversions"))
check("Reconnect Google Ads to send leads from the server" in s, "GOOGLE_ADS on: Settings says to reconnect for it")
run("UPDATE ad_connections SET scopes=? WHERE id=?",
    "openid email https://www.googleapis.com/auth/adwords https://www.googleapis.com/auth/datamanager", gconn)
G.scope_of["108700000000000000077"] = "openid email https://www.googleapis.com/auth/adwords"
c7, r = visit("/p/cool-air", ip="10.77.0.7")
p7 = html(r)
time.sleep(2.1)
c7.post("/f/lead1", data=dict(form, _t=re.search(r'name="_t" value="([^"]+)"', p7).group(1),
                               _eid=re.search(r'name="_eid" value="([^"]+)"', p7).group(1), _fbclid="",
                               f_email="scope@example.com", f_phone=""), headers=US)
flush()
gfail = q("SELECT * FROM ws_conversion_queue WHERE platform='google' AND status='failed'")
check(len(gfail) == 1 and gfail[0]["error"].startswith("permission:") and "PERMISSION_DENIED" in gfail[0]["error"],
      "Google refusing the scope is recorded")
check("Google refused the lead. Reconnect Google Ads" in html(owner.get("/app/settings/conversions")),
      "Settings says so in plain words")
G.scope_of["108700000000000000077"] += " https://www.googleapis.com/auth/datamanager"

# ================================================================ 8. errors and retries
print("errors")
SOC.on("POST", "/events", "meta/error_190.json")
SOC.on("POST", "/event/track/", "tiktok/events_error_token.json")
c8, r = visit(ip="10.77.0.8")
st = flush()
mf = q("SELECT * FROM ws_conversion_queue WHERE status='failed' AND platform='meta'")
tf = q("SELECT * FROM ws_conversion_queue WHERE status='failed' AND platform='tiktok'")
check(mf and mf[-1]["error"].startswith("token:") and "subcode=463" in mf[-1]["error"] and META_TOKEN not in mf[-1]["error"],
      "Meta 190: failed as a refused token, scrubbed")
check(tf and tf[-1]["error"].startswith("token:") and "40105" in tf[-1]["error"], "TikTok 40105: failed as a refused token")
s = html(owner.get("/app/settings/conversions"))
check("Meta refused the token. Paste a new one" in s and "TikTok refused the token" in s, "Settings explains both")
for _ in range(5):
    flush()
check(all(x["attempts"] == conversions.WS_MAX_ATTEMPTS for x in
          q("SELECT attempts FROM ws_conversion_queue WHERE status='failed' AND platform IN ('meta','tiktok')")),
      "a failing row is tried 4 times, then left")
import requests  # noqa: E402
SOC.on("POST", "/events", requests.ConnectionError("down"))
SOC.on("POST", "/event/track/", "tiktok/events_ok.json")
visit(ip="10.77.0.9")
flush()
check("network" in q("SELECT error FROM ws_conversion_queue WHERE platform='meta' ORDER BY id DESC LIMIT 1")[0][0].lower(),
      "a network error is recorded and retried later")
SOC.on("POST", "/events", "meta/capi_events_ok.json")
flush()
check(q("SELECT status FROM ws_conversion_queue WHERE platform='meta' ORDER BY id DESC LIMIT 1")[0][0] == "sent",
      "the retry goes out")
owner.post(f"/app/settings/conversions/tiktok/{TT_ID}/remove", data={"csrf": csrf(owner)})
check(not q("SELECT 1 FROM pixels WHERE platform='tiktok'"), "removing the TikTok pixel deletes it and its token")
run("UPDATE ad_connections SET status='revoked', token_enc=NULL WHERE id=?", meta_conn)
run("INSERT INTO ws_conversion_queue (workspace_id, platform, dest_id, event, event_id, payload, created_at) VALUES "
    "(?,?,?,?,?,?,?)", WS, "meta", meta_px, "PageView", site_events.new_event_id(),
    json.dumps({"event": "PageView", "event_id": "x", "ts": int(time.time()), "ctx": {}}), db.now())
flush()
check(q("SELECT status FROM ws_conversion_queue ORDER BY id DESC LIMIT 1")[0][0] == "dropped",
      "a destination disconnected meanwhile: its queued events are dropped, not sent")
run("UPDATE ad_connections SET status='active', token_enc=? WHERE id=?",
    secretsbox.seal("EAAconnectionToken0000000000000", secretsbox.aad_for("ad_connections", WS)), meta_conn)

# ================================================================ 9. the hosted form
print("hosted form")
fe = html(owner.get(f"/app/forms/{form_id}"))
check("data-measurement" in fe and "Measure ad results on this form" in fe, "the form editor shows its own measurement box")
c9 = client("10.77.0.10")
r = c9.get("/f/lead1", headers=US)
nonce, cfg = px_config(html(r))
check(cfg and cfg["meta"] == ["123456789012345"] and f"'nonce-{nonce}'" in r.headers["Content-Security-Policy"]
      and q("SELECT 1 FROM page_events WHERE form_id=? AND page_id IS NULL AND name='PageView'", form_id),
      "the hosted form is measured like a page")
h = html(r)
time.sleep(2.1)
r = c9.post("/f/lead1", data={"_t": re.search(r'name="_t" value="([^"]+)"', h).group(1),
                              "_eid": re.search(r'name="_eid" value="([^"]+)"', h).group(1), "f_email": "host@example.com"},
            headers=US)
loc = r.headers["Location"]
check("/f/lead1?" in loc and "sent=1" in loc and "ev=" in loc, "its lead returns to the hosted thank-you page with the id")
_, cfg = px_config(html(c9.get(loc, headers=US)))
check(cfg.get("lead") == parse_qs(urlsplit(loc).query)["ev"][0], "which fires the browser Lead")

# ================================================================ 10. worker, logs, database
print("worker and secrets")
visit(ip="10.77.0.11")
conversions._last_tick[0] = float("-inf")
conversions.tick()
check(not q("SELECT 1 FROM ws_conversion_queue WHERE status='pending'"), "the worker's tick sends what's pending")
os.environ.pop("CONVERSIONS_API")
_, r = visit(ip="10.77.0.12")
check("cfy-px" not in html(r) and "script-src" not in r.headers["Content-Security-Policy"],
      "CONVERSIONS_API off again: pages go back to no scripts")
logs = "\n".join(CAP.lines)
check(META_TOKEN not in logs and TIKTOK_TOKEN not in logs and GOOGLE_REFRESH not in logs and "ya29.fake" not in logs,
      "no token in any log line")
check(EMAIL.lower() not in logs.lower() and "5125550142" not in logs, "no raw email or phone in any log line")
raw = b""
for name in os.listdir(TMP):
    if name.startswith("app.db") or name.endswith(".db"):
        raw += open(os.path.join(TMP, name), "rb").read()
check(raw and META_TOKEN.encode() not in raw and TIKTOK_TOKEN.encode() not in raw and GOOGLE_REFRESH.encode() not in raw,
      "no token in the database file")

print(f"\nall {passed} checks passed")
