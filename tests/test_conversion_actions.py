"""Google Ads conversion actions and the Google tag on members' pages (roadmap M3.5: conversion_actions.py, the
Google parts of conversions.py and static/pagepx.js, launch_google.py's warning) on a throwaway database. Google is
the fake REST endpoint in tests/fakegoogleads.py (ConversionActionService, GAQL FROM conversion_action and
customer.conversion_tracking_setting, Data Manager), shaped like tests/fixtures/google/*.json: nothing leaves the machine.

    python tests/test_conversion_actions.py      (tests/test_app.py runs it too)

  * flags: without CONVERSIONS_API the connect asks for the same scopes as before and makes nothing; with it the
    connect also asks for the datamanager scope and makes the actions
  * on connect: one mutate with Campaignify lead (WEBPAGE, primary), Campaignify lead (server) (UPLOAD_CLICKS,
    secondary) and Campaignify purchase (WEBPAGE, account currency); tag id and labels read from tag_snippets
  * on demand: adopts ours by name (no second mutate), makes only what's missing, survives a duplicate-name race,
    stops when a manager owns the conversions, says what went wrong in plain words; a connect never fails over it
  * pages: the Google tag and the lead conversion with transaction_id = the event id, enhanced conversions user
    data as SHA-256 on the thank-you page, Data Manager to the server action, CSP with Google's hosts only then
  * the launch card's "Google bids for the conversions you track" warning clears once actions exist (ours, or the
    account's own primary ones)
  * the Enhanced conversions checklist reads what the API can; the purchase snippet is shown escaped
  * Disconnect forgets the actions; no token in logs
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
TMP = tempfile.mkdtemp(prefix="rs_gconv_", dir=os.environ.get("TMPDIR"))
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0", RS_LOG_JSON="1",
                  RS_DISK_MIN_GB="0.2", RS_CAPI_SYNC="1", TOKEN_KEY=base64.b64encode(b"A" * 32).decode(),
                  GOOGLE_OAUTH_CLIENT_ID="555-fake.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET="GOCSPX-gc-9",
                  AD_LAUNCH="on", GOOGLE_ADS="1")
for k in ("AD_LAUNCH_WORKSPACES", "CONVERSIONS_API", "META_CAPI_USE_CONNECTION_TOKEN", "TOKEN_KEYS_OLD",
          "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_DATAMANAGER_VALIDATE_ONLY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adspec  # noqa: E402
import conversion_actions  # noqa: E402
import conversions  # noqa: E402
import db  # noqa: E402
import launch_google  # noqa: E402
from adfake import fixture  # noqa: E402
from adplatforms import google  # noqa: E402
from fakegoogleads import FakeGoogleAds, Refuse, gerr  # noqa: E402

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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
US = {"User-Agent": UA, "X-Country": "US"}
DM = "https://www.googleapis.com/auth/datamanager"
_ip = [0]


def client(ip=None):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip or f"10.62.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


def sha(v):
    return hashlib.sha256(v.encode()).hexdigest()


def px_config(page):
    m = re.search(r'<script type="application/json" id="cfy-px" nonce="([^"]+)">(.*?)</script>', page, re.S)
    return (m.group(1), json.loads(m.group(2))) if m else (None, None)


G = FakeGoogleAds()
G.customer("1112223334", "Cool Air HVAC")
G.customer("9998887776", "Agency MCC", manager=True)
G.customer("5556667778", "Client West", parent="9998887776", currency="CAD")
G.customer("4443332221", "Has Own Tracking")
G.customer("7776665554", "Managed Client", parent="9998887776")
G.tracking("7776665554", owner="9998887776")
adhttp.TRANSPORT = G
owner, u = signup("gconv-owner@example.com")
WS = u["workspace_id"]


def connect(c, customer, code, scope=None, access=("1112223334", "9998887776", "4443332221")):
    r = c.post("/connect/google", data={"csrf": csrf(c)})
    qs = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["Location"]).query).items()}
    G.code(code, sub="108700000000000000555", access=list(access),
           scope=scope or "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/adwords")
    r = c.get(f"/connect/google/callback?state={qs['state']}&code={code}")
    pick = r.headers["Location"]
    c.post(pick, data={"csrf": csrf(c), "account": customer})
    return qs, q("SELECT * FROM ad_connections WHERE workspace_id=? AND platform='google' AND external_account_id=?",
                 WS, customer)[0]


def mutates():
    return G.find("conversionActions:mutate")


# ================================================================ 1. the flags
print("flags")
qs, row = connect(owner, "4443332221", "code-off")
check(qs["scope"].split() == ["openid", "email", "https://www.googleapis.com/auth/adwords"],
      "CONVERSIONS_API off: connect asks for exactly the scopes it did before")
check(not mutates() and not q("SELECT 1 FROM conversion_actions") and not q("SELECT 1 FROM conversion_setups"),
      "and makes no conversion action")
check(owner.post(f"/app/settings/conversions/google/{row['id']}/sync", data={"csrf": csrf(owner)}).status_code == 404,
      "the on-demand sync is a 404")
owner.post(f"/app/settings/ad-accounts/{row['id']}/disconnect", data={"csrf": csrf(owner)})
os.environ["CONVERSIONS_API"] = "1"

# ================================================================ 2. on connect
print("on connect")
full = "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/adwords " + DM
qs, row = connect(owner, "1112223334", "code-on", scope=full)
CONN = row["id"]
check(qs["scope"].split() == ["openid", "email", "https://www.googleapis.com/auth/adwords", DM],
      "CONVERSIONS_API on: connect also asks for the Data Manager scope")
check(DM in row["scopes"].split(), "the granted scope is kept on the connection")
check(len(mutates()) == 1, "one ConversionActionService mutate")
ops = [o["create"] for o in mutates()[0]["data"]["operations"]]
by = {o["name"]: o for o in ops}
check(set(by) == {"Campaignify lead [cfy]", "Campaignify lead (server) [cfy]", "Campaignify purchase [cfy]"},
      "it makes the lead, the server lead and the purchase actions")
L, S, P = by["Campaignify lead [cfy]"], by["Campaignify lead (server) [cfy]"], by["Campaignify purchase [cfy]"]
check(L["type"] == "WEBPAGE" and L["category"] == "SUBMIT_LEAD_FORM" and L["countingType"] == "ONE_PER_CLICK"
      and L["primaryForGoal"] is True and L["status"] == "ENABLED", "lead: a webpage action, one per click, primary")
check(S["type"] == "UPLOAD_CLICKS" and S["category"] == "SUBMIT_LEAD_FORM" and S["primaryForGoal"] is False,
      "server lead: an import action, secondary (so a lead isn't counted twice)")
check(P["type"] == "WEBPAGE" and P["category"] == "PURCHASE" and P["countingType"] == "MANY_PER_CLICK"
      and P["valueSettings"]["defaultCurrencyCode"] == "USD", "purchase: every conversion, in the account's currency")
acts = {r["kind"]: r for r in q("SELECT * FROM conversion_actions WHERE connection_id=?", CONN)}
tag = f"AW-{G._conv_id('1112223334')}"
check(set(acts) == {"lead", "lead_server", "purchase"} and acts["lead"]["tag_id"] == tag
      and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", acts["lead"]["label"] or "") and acts["lead_server"]["tag_id"] is None
      and all(a["created_by_us"] == 1 and a["status"] == "ENABLED" for a in acts.values()),
      "the actions are kept with the Google tag id and each tag label from tag_snippets")
su = q("SELECT * FROM conversion_setups WHERE connection_id=?", CONN)[0]
check(su["status"] == "ready" and su["tag_id"] == tag and su["accepted_terms"] == 0 and su["ec_leads"] == 0
      and "gtag('config'" in su["purchase_snippet"], "the setup: ready, the tag, terms and enhanced conversions as Google says")
check("Lead and purchase conversion actions are set up in Google Ads." in flashes(owner), "the connect says so")

# ================================================================ 3. on demand
print("on demand")
owner.post(f"/app/settings/conversions/google/{CONN}/sync", data={"csrf": csrf(owner)})
check(len(mutates()) == 1 and "Conversion actions are set up" in flashes(owner), "checking again adopts ours: no new mutate")
rn = acts["purchase"]["resource_name"]
G.store[rn]["status"] = "REMOVED"
owner.post(f"/app/settings/conversions/google/{CONN}/sync", data={"csrf": csrf(owner)})
check(len(mutates()) == 2 and [o["create"]["name"] for o in mutates()[-1]["data"]["operations"]] == ["Campaignify purchase [cfy]"],
      "one removed in Google Ads: only that one is made again")
G.store[q("SELECT resource_name FROM conversion_actions WHERE connection_id=? AND kind='purchase'", CONN)[0][0]]["status"] = "REMOVED"
_orig = G._conversion_actions


def _racing(cid, body):
    """Someone makes the purchase action between our read and our mutate: Google answers DUPLICATE_NAME."""
    G._conversion_actions = _orig
    G.conversion_action(cid, "Campaignify purchase [cfy]", category="PURCHASE")
    raise Refuse(gerr(400, "INVALID_ARGUMENT", "conversionActionError", "DUPLICATE_NAME",
                      "Conversion action names must be unique.", 0))


G._conversion_actions = _racing
with db.connect() as conn:
    res = conversion_actions.sync(conn, conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN,)).fetchone())
check(res["status"] == "ready" and G._conversion_actions == _orig and
      q("SELECT external_id FROM conversion_actions WHERE connection_id=? AND kind='purchase'", CONN)[0][0]
      == [a for a in G.objects("conversion_action") if a["name"] == "Campaignify purchase [cfy]"][-1]["id"],
      "a duplicate name (made in between) is read back and adopted")
member, mu = signup("gconv-member@example.com")
run("UPDATE users SET workspace_id=?, role='member' WHERE id=?", WS, mu["id"])
check(member.post(f"/app/settings/conversions/google/{CONN}/sync", data={"csrf": csrf(member)}).status_code == 403,
      "a member can't run it (403)")
other, _ = signup("gconv-other@example.com")
check(other.post(f"/app/settings/conversions/google/{CONN}/sync", data={"csrf": csrf(other)}).status_code == 404,
      "another workspace's connection is a 404")

# ================================================================ 4. managed elsewhere, errors
print("managed elsewhere and errors")
n = len(mutates())
_, mrow = connect(owner, "7776665554", "code-managed", scope=full)
ms = q("SELECT * FROM conversion_setups WHERE connection_id=?", mrow["id"])[0]
check(len(mutates()) == n and ms["status"] == "managed_elsewhere" and ms["managed_by"] == "9998887776",
      "a client whose manager owns its conversions: nothing is made")
check("managed in manager account 999-888-7776" in flashes(owner), "the connect says where to make them")
s = html(owner.get("/app/settings/conversions"))
check("Managed in manager account 999-888-7776" in s, "Settings says so too")
with db.connect() as conn:
    check(conversion_actions.ready(conn, mrow), "and Google's bidding can use the manager's actions (no warning)")
G.fail("conversionActions:mutate", "google/error_user_permission_denied.json")
_, erow = connect(owner, "5556667778", "code-err", scope=full)
es = q("SELECT * FROM conversion_setups WHERE connection_id=?", erow["id"])[0]
check(erow["status"] == "active" and es["status"] == "error" and "Standard or Admin access" in es["error"],
      f"a refused mutate: the connection still works, the reason is kept in plain words")
check("Conversion actions weren't made" in flashes(owner), "the connect says they weren't made")
check(not q("SELECT 1 FROM conversion_actions WHERE connection_id=?", erow["id"]), "nothing half-made is kept")
s = html(owner.get("/app/settings/conversions"))
check("Not set up" in s and "Set up conversion actions" in s, "Settings offers to try again")
owner.post(f"/app/settings/conversions/google/{erow['id']}/sync", data={"csrf": csrf(owner)})
check(len(q("SELECT 1 FROM conversion_actions WHERE connection_id=?", erow["id"])) == 3
      and [o["create"]["valueSettings"]["defaultCurrencyCode"] for o in mutates()[-1]["data"]["operations"]
           if "valueSettings" in o["create"]] == ["CAD"], "trying again works, the purchase value in CAD")
with db.connect() as conn:
    crow = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN,)).fetchone()
    bad = dict(crow)
    bad["token_enc"] = None
    check(conversion_actions.sync(conn, bad)["status"] == "error", "no token: an error, never an exception")

# ================================================================ 5. parse the snippets Google sends
print("snippets")
row0 = fixture("google/search_conversion_actions.json")["results"][0]["conversionAction"]
check(conversion_actions.parse_snippets(row0["tagSnippets"]) == ("AW-987654321", "AbC-D_efG-h12_34-567"),
      "Google's HTML snippet gives the tag id and the label")
check(conversion_actions.parse_snippets([{"type": "WEBPAGE", "pageFormat": "HTML", "globalSiteTag": "x",
                                          "eventSnippet": "'send_to': 'AW-1/<script>'"}]) == (None, None),
      "anything that isn't a plain id and label is ignored")

# ================================================================ 6. pages: the Google tag
print("pages")
for cid in (mrow["id"], erow["id"]):
    owner.post(f"/app/settings/ad-accounts/{cid}/disconnect", data={"csrf": csrf(owner)})
form_id = run("INSERT INTO forms (workspace_id, code, name, fields_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
              WS, "gq1", "Quote", json.dumps([
                  {"key": "email", "label": "Email", "type": "email", "required": True, "options": [], "show_if": None},
                  {"key": "phone", "label": "Phone", "type": "phone", "required": False, "options": [], "show_if": None}]),
              db.now(), db.now())
run("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, created_at, updated_at) VALUES "
    "(?,?,?,?,?,?,?,?)", WS, "cool-air-g", "Cool Air", "", "published",
    json.dumps([{"type": "hero", "headline": "AC repair", "subhead": "", "button_text": "", "button_url": "", "media": None},
                {"type": "form", "heading": "Quote", "form": form_id}]), db.now(), db.now())
lead = q("SELECT * FROM conversion_actions WHERE connection_id=? AND kind='lead'", CONN)[0]
c = client("10.88.0.1")
r = c.get("/p/cool-air-g?gclid=Cj0gtag", headers=US)
page = html(r)
nonce, cfg = px_config(page)
csp = r.headers["Content-Security-Policy"]
check(cfg["google"] == {"tag": lead["tag_id"], "lead": f"{lead['tag_id']}/{lead['label']}"} and "meta" not in cfg,
      "the page config carries the Google tag id and the lead's send_to, built from the stored id and label")
check("https://www.googletagmanager.com" in csp and "https://googleads.g.doubleclick.net" in csp
      and "connect.facebook.net" not in csp and f"'nonce-{nonce}'" in csp, "the CSP adds Google's hosts only")
check("gtag(" not in page and "googletagmanager.com/gtag" not in page, "no Google HTML is pasted into the page")
js = open(os.path.join(os.path.dirname(HERE), "static", "pagepx.js")).read()
check("transaction_id: C.lead" in js and "'user_data', C.google.user" in js and "send_to: C.google.lead" in js,
      "pagepx.js sends the lead conversion with transaction_id = the event id, and user_data when given")
eid = re.search(r'name="_eid" value="([^"]+)"', page).group(1)
time.sleep(2.1)
r = c.post("/f/gq1", data={"_t": re.search(r'name="_t" value="([^"]+)"', page).group(1), "_page": "cool-air-g",
                           "_eid": eid, "_gclid": "Cj0gtag", "f_email": "Pat.Lee@gmail.com", "f_phone": "512 555 0199"},
           headers=US)
loc = r.headers["Location"]
_, cfg = px_config(html(c.get(loc, headers=US)))
check(cfg["lead"] == eid and cfg["google"]["user"] == {"sha256_email_address": sha("patlee@gmail.com"),
                                                        "sha256_phone_number": sha("+15125550199")},
      "the thank-you page: the lead with its id, and enhanced conversions data as SHA-256 only")
gq = q("SELECT * FROM ws_conversion_queue WHERE platform='google'")
srv = q("SELECT * FROM conversion_actions WHERE connection_id=? AND kind='lead_server'", CONN)[0]
check(len(gq) == 1 and gq[0]["dest_id"] == srv["id"] and gq[0]["event_id"] == eid,
      "the server copy goes to the server lead action, with the same id")
with db.connect() as conn:
    conversions.flush_ws(conn)
check(G.ingested and G.ingested[-1]["destinations"][0]["productDestinationId"] == srv["external_id"]
      and G.ingested[-1]["events"][0]["transactionId"] == eid, "Data Manager accepts it")
check("Pat.Lee" not in " ".join(str(tuple(x)) for x in q("SELECT * FROM page_events")) and
      "patlee@gmail.com" not in json.dumps(G.ingested), "no raw email in the collector or at Google")

# ================================================================ 7. the launch warning
print("launch warning")
with db.connect() as conn:
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (WS,)).fetchone()
    crow = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN,)).fetchone()
d = {"objective": "leads", "platform": "google", "name": "Cool Air", "daily_budget_minor": 3000, "currency": "USD",
     "start_at": "2026-11-20", "end_at": None, "geo": {"countries": ["US"]},
     "audience": {"age_min": 18, "age_max": 65, "genders": ["all"]}, "placements": "auto", "conversion": None,
     "creatives": [], "special_ad_categories": [], "ai_disclosure": False, "vertical": "general",
     "claims_substantiated": False,
     "google": {"type": "search", "final_url": "https://coolair.example/", "keywords": [{"term": "ac repair", "match": "phrase"}],
                "negative_keywords": [], "headlines": ["A", "B", "C"], "headline_pins": [0, 0, 0],
                "descriptions": ["D one", "D two"], "description_pins": [0, 0], "path1": "", "path2": "",
                "bidding": "conversions", "language": "en", "search_partners": True, "long_headlines": [],
                "business_name": "Cool Air", "logos": []}}
spec, _ = adspec.from_dict(d)


def warned(conn_row):
    with db.connect() as conn:
        return "google.conversions_needed" in {i.rule_id for i in launch_google.issues(conn, ws, spec, conn_row, {})}


check(not warned(crow), "Maximize conversions on an account with our conversion actions: no warning")
run("UPDATE conversion_actions SET status='REMOVED' WHERE connection_id=? AND kind IN ('lead','purchase')", CONN)
check(warned(crow), "without them, the warning is back")
_, orow = connect(owner, "4443332221", "code-own", scope=full)
G.conversion_action("4443332221", "Phone calls", category="PHONE_CALL_LEAD")
with db.connect() as conn:
    run("DELETE FROM conversion_actions WHERE connection_id=?", orow["id"])
    run("UPDATE conversion_setups SET other_actions=0 WHERE connection_id=?", orow["id"])
    check(warned(orow), "a fresh account with nothing tracked: the warning shows")
    conversion_actions.sync(conn, conn.execute("SELECT * FROM ad_connections WHERE id=?", (orow["id"],)).fetchone(),
                            create=False)
check(q("SELECT other_actions FROM conversion_setups WHERE connection_id=?", orow["id"])[0][0] >= 1,
      "the account's own primary conversion actions are counted")
run("DELETE FROM conversion_actions WHERE connection_id=?", orow["id"])
check(not warned(orow), "so its own tracking clears the warning too")
run("UPDATE conversion_actions SET status='ENABLED' WHERE connection_id=?", CONN)

# ================================================================ 8. the checklist and the purchase snippet
print("checklist")
s = html(owner.get("/app/settings/conversions"))
block = s[s.index("data-ec-checklist"):]
check('data-step="tag"' in block and 'data-step="terms"' in block and 'data-step="ec_web"' in block
      and "Check in Google Ads" in block and "To do" in block, "the Enhanced conversions checklist, what Google says, what to check")
check("https://ads.google.com/aw/conversions/customersettings" in block, "with links into Google Ads")
check("&lt;!-- Google tag (gtag.js) --&gt;" in s and "gtag(&#39;config&#39;" in s, "the purchase snippet is shown escaped, to copy")
G.tracking("1112223334", terms=True, ec_leads=True)
owner.post(f"/app/settings/conversions/google/{CONN}/sync", data={"csrf": csrf(owner)})
with db.connect() as conn:
    items = {i["key"]: i["state"] for i in conversion_actions.checklist(
        conn, conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN,)).fetchone())}
check(items == {"tag": True, "terms": True, "ec_web": None, "ec_leads": True, "dm": True},
      "terms and enhanced conversions for leads read from the API; web can't be read")
check(not re.search(r"[–—]", re.sub(r"<[^>]+>", " ", s)), "no em or en dashes on Settings")

# ================================================================ 9. disconnect, secrets
print("disconnect")
owner.post(f"/app/settings/ad-accounts/{CONN}/disconnect", data={"csrf": csrf(owner)})
check(not q("SELECT 1 FROM conversion_actions WHERE connection_id=?", CONN)
      and not q("SELECT 1 FROM conversion_setups WHERE connection_id=?", CONN), "Disconnect forgets the actions and the setup")
_, cfg = px_config(html(client("10.88.0.9").get("/p/cool-air-g", headers=US)))
check(cfg is None or "google" not in cfg, "and the page stops loading the Google tag")
logs = "\n".join(CAP.lines)
check("fake-refresh" not in logs and "ya29.fake" not in logs and "GOCSPX" not in logs, "no token or secret in any log line")

print(f"\nall {passed} checks passed")
