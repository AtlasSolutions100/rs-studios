"""Results of Google, TikTok and Snapchat campaigns (roadmap M7 follow-up to M2.5): their adapters' daily rows stored
in ad_insights_daily next to Meta's, and every platform's launched campaigns on the Ad campaigns page. The platforms
are a fake transport (tests/adfake.py): no request leaves the machine.

    python tests/test_insights_platforms.py      (tests/test_app.py runs it too)

  * normalize: spend in minor units, results by the launch's goal (purchases, leads, link clicks or swipes), a
    platform's conversion count standing in where it has no lead or purchase figure, rates from the sums
  * Google's reader: one GAQL search on campaign by segments.date, cost_micros in the account's currency, the
    manager as login-customer-id
  * the pull for each platform: campaign-level rows with the launch and platform, upserts, days dropped inside the
    window removed, the schedule (due, tick, "Update now"), behind AD_LAUNCH
  * errors in plain words with the platform's name: an ended grant marks Reconnect, a rate limit moves the pull
    later, stored usage that says wait sends no request
  * the Ad campaigns page lists every platform's campaigns with their numbers and their own ads manager; only Meta's
    have Manage; another workspace sees nothing; no token in any log line or row
"""
import atexit
import base64
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_insights_pl_")
atexit.register(shutil.rmtree, TMP, True)
KEY = base64.b64encode(b"P" * 32).decode()
SNAP_REFRESH = "snap-refresh-SECRET-0001"
TT_TOKEN = "tiktok-access-SECRET-0002"
G_REFRESH = "1//google-refresh-SECRET-0003"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", TOKEN_KEY=KEY, AD_RATE_TIER="full", AD_LAUNCH="on",
                  SNAPCHAT_CLIENT_ID="snap-client", SNAPCHAT_CLIENT_SECRET="snap-client-secret",
                  GOOGLE_ADS_CLIENT_ID="g-client.apps.googleusercontent.com", GOOGLE_ADS_CLIENT_SECRET="g-client-secret",
                  TIKTOK_APP_ID="7000000000000000001", TIKTOK_APP_SECRET="tiktok-app-secret")
for k in ("AD_LAUNCH_WORKSPACES", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adinsights  # noqa: E402
import admanage  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
import launches  # noqa: E402
from adfake import FakeTransport  # noqa: E402
from adplatforms import google, snapchat, tiktok  # noqa: E402

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
for name in ("", "campaignify", "werkzeug", "urllib3"):
    lg = logging.getLogger(name)
    lg.addHandler(CAP)
    lg.setLevel(logging.DEBUG)

T = [time.time()]
adhttp.CLOCK = launches.CLOCK = admanage.CLOCK = adinsights.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.57.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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
        conn.execute("UPDATE workspaces SET plan='growth', plan_status='active' WHERE id=?", (u["workspace_id"],))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def ex(sql, *args):
    with db.connect() as conn:
        conn.execute(sql, args)
        conn.commit()


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


TZ = "America/Chicago"


def day(n):
    return (adinsights.today(TZ) - timedelta(days=n)).isoformat()


TOKENS = {"snapchat": SNAP_REFRESH, "tiktok": TT_TOKEN, "google": G_REFRESH}


def connect(ws_id, user_id, platform, account, currency="USD", business=None):
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
            "business_id, external_user_id, token_enc, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ws_id, platform, account, f"{platform} account", currency, TZ, business, f"{platform}-user",
             adplatforms.seal_token(ws_id, TOKENS[platform]), "active", user_id, db.now(), db.now())).lastrowid
        conn.commit()
    return cid


_n = [0]


def launched(ws_id, user_id, conn_id, platform, camp_id, goal="sales", name="Ember", status="paused"):
    """A launch row as the saga leaves it (status paused, the campaign step done, the campaign object kept)."""
    _n[0] += 1
    spec = {"objective": goal, "platform": platform, "name": name, "daily_budget_minor": 2500, "currency": "USD"}
    with db.connect() as conn:
        lid = conn.execute("INSERT INTO launches (workspace_id, platform, connection_id, spec_json, status, "
                           "external_campaign_id, idempotency_key, created_by, approved_by, approved_at, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (ws_id, platform, conn_id, json.dumps(spec), status, camp_id, f"key-{_n[0]}", user_id, user_id,
                            day(10) + "T15:00:00+00:00", db.now(), db.now())).lastrowid
        conn.execute("INSERT INTO launch_steps (launch_id, step_no, step_key, kind, name, external_id, status) "
                     "VALUES (?,?,?,?,?,?,?)", (lid, 1, "campaign", "campaign", name, camp_id, "done"))
        conn.execute("INSERT INTO launch_steps (launch_id, step_no, step_key, kind, name, external_id, status) "
                     "VALUES (?,?,?,?,?,?,?)", (lid, 2, "ad:1", "ad", name + " ad 1", camp_id + "-ad1", "done"))
        for kind, ext in (("campaign", camp_id), ("ad", camp_id + "-ad1")):
            conn.execute("INSERT INTO ad_objects (launch_id, workspace_id, platform, kind, external_id, name, status, "
                         "created_at) VALUES (?,?,?,?,?,?,?,?)", (lid, ws_id, platform, kind, ext, name, "PAUSED", db.now()))
        conn.commit()
    return lid


# ---------------------------------------------------------------- the fake platforms

SNAP_STATS = {}      # campaign id -> [(day, stats)]
TT_REPORT = []       # TikTok report rows
G_ROWS = []          # GAQL result rows


def snap_stats(method, url, params, data, headers):
    camp = urlsplit(url).path.split("/")[-2]
    assert headers.get("Authorization") == "Bearer snap-access-1", "the stats call uses the minted access token"
    return {"request_status": "SUCCESS", "timeseries_stats": [{"sub_request_status": "SUCCESS", "timeseries_stat": {
        "id": camp, "type": "CAMPAIGN", "granularity": "DAY",
        "timeseries": [{"start_time": f"{d}T00:00:00.000-06:00", "end_time": f"{d}T00:00:00.000-06:00", "stats": st}
                       for d, st in SNAP_STATS.get(camp, [])]}}]}


def tt_report(method, url, params, data, headers):
    return {"code": 0, "message": "OK", "data": {"list": list(TT_REPORT), "page_info": {"page": 1, "total_page": 1}}}


def g_search(method, url, params, data, headers):
    return {"results": list(G_ROWS), "fieldMask": "campaign.id,segments.date,metrics.cost_micros"} if G_ROWS else \
        {"fieldMask": "x"}


def transport():
    F = FakeTransport()
    F.on("POST", "/login/oauth2/access_token", {"access_token": "snap-access-1", "expires_in": 3600})
    F.on("GET", "/stats", snap_stats)
    F.on("GET", "/report/integrated/get/", tt_report)
    F.on("POST", "/token", {"access_token": "ya29.google-access-1", "expires_in": 3600})
    F.on("POST", "googleAds:search", g_search)
    return F


F = transport()
adhttp.TRANSPORT = F

owner, u = signup("platforms-owner@example.com")
WS = u["workspace_id"]
SNAP_C = connect(WS, u["id"], "snapchat", "snap-acc-1")
TT_C = connect(WS, u["id"], "tiktok", "7100000000000000001")
G_C = connect(WS, u["id"], "google", "1234567890", business="9876543210")
SNAP_L = launched(WS, u["id"], SNAP_C, "snapchat", "snap-camp-1", goal="sales", name="Ember on Snap")
TT_L = launched(WS, u["id"], TT_C, "tiktok", "1800000000000001", goal="leads", name="Ember on TikTok")
G_L = launched(WS, u["id"], G_C, "google", "22000000001", goal="sales", name="Ember on Google")
G_L2 = launched(WS, u["id"], G_C, "google", "22000000002", goal="traffic", name="Ember Search")
QUEUED = launched(WS, u["id"], SNAP_C, "snapchat", "snap-camp-q", status="queued", name="Not made yet")


def rows(lid):
    return q("SELECT * FROM ad_insights_daily WHERE launch_id=? ORDER BY day", lid)


def due_now(*ids):
    for i in ids:
        ex("UPDATE ad_connections SET insights_next_at=NULL, insights_error=NULL, status='active' WHERE id=?", i)


# ================================================================ 1. normalize
print("normalize")
r = adinsights.normalize({"external_object_id": "c1", "day": "2026-11-07", "spend_minor": 1234, "impressions": 1000,
                          "clicks": 40, "link_clicks": 40, "purchases": 2, "purchase_value_minor": 9000, "leads": 0},
                         {"objective": "sales"}, "USD", "snapchat")
check(r["results"] == 2 and r["result_kind"] == "purchase" and r["cost_per_result_minor"] == 617,
      "a sales campaign's results are its purchases; cost per purchase from the sums")
check(r["ctr"] == 4.0 and r["cpc_minor"] == 31, "click rate and cost per click come from the sums")
r = adinsights.normalize({"external_object_id": "c2", "day": "2026-11-07", "spend_minor": 5000, "impressions": 2000,
                          "clicks": 10, "leads": 0, "purchases": 0, "conversions": 4}, {"objective": "leads"}, "USD", "tiktok")
check(r["results"] == 4 and r["leads"] == 4 and r["cost_per_result_minor"] == 1250,
      "TikTok's conversion count stands in for leads when it reports no lead figure")
r = adinsights.normalize({"external_object_id": "c3", "day": "2026-11-07", "spend_minor": 800, "impressions": 500,
                          "clicks": 25}, {"objective": "traffic"}, "USD", "google")
check(r["results"] == 25 and r["result_kind"] == "link_click" and r["link_clicks"] == 25,
      "a traffic campaign's results are its clicks")
r = adinsights.normalize({"external_object_id": "c4", "day": "2026-11-07", "spend_minor": 0, "impressions": 0},
                         {"objective": "awareness"}, "USD", "snapchat")
check(r["results"] == 0 and r["cost_per_result_minor"] is None and r["ctr"] is None,
      "no delivery: no rates, no division by zero")
r = adinsights.normalize({"external_object_id": "c5", "day": "2026-11-07", "spend_minor": 300, "conversions": 3,
                          "conversion_value_minor": 4500}, {"objective": "sales"}, "jpy", "google")
check(r["purchases"] == 3 and r["purchase_value_minor"] == 4500 and r["currency"] == "JPY",
      "Google's conversions and their value count as purchases for a sales campaign")
check(adinsights.words("snapchat", "auth").startswith("Snapchat ended Campaignify's access")
      and adinsights.words("meta", "auth") == adinsights.WORDS["auth"], "pull problems are worded with the platform's name")

# ================================================================ 2. Google's reader
print("Google's GAQL reader")
G_ROWS[:] = [{"campaign": {"resourceName": "customers/1234567890/campaigns/22000000001", "id": "22000000001"},
              "segments": {"date": day(2)}, "metrics": {"costMicros": "12345678", "impressions": "1500", "clicks": "60",
                                                       "conversions": 3.0, "conversionsValue": 120.5}},
             {"campaign": {"resourceName": "customers/1234567890/campaigns/22000000001", "id": "22000000001"},
              "segments": {"date": day(1)}, "metrics": {"costMicros": "2000000", "impressions": "300", "clicks": "9"}}]
got = google.ADAPTER.pull_insights({"token": G_REFRESH, "account": "1234567890", "login": "9876543210", "currency": "USD"},
                                   [{"kind": "campaign", "external_id": "22000000001"}, {"kind": "ad", "external_id": "x"}],
                                   (day(14), day(0)))
call = F.find("POST", "googleAds:search")[-1]
query = call["data"]["query"]
check("FROM campaign" in query and "campaign.id IN (22000000001)" in query
      and f"segments.date BETWEEN '{day(14)}' AND '{day(0)}'" in query and "metrics.cost_micros" in query,
      "one GAQL search on campaign by day, for the launched campaigns and the window")
check(call["headers"].get("login-customer-id") == "9876543210" and call["path"].endswith("/customers/1234567890/googleAds:search"),
      "on the client customer, through its manager (login-customer-id)")
check(got[0]["spend_minor"] == 1234 and got[0]["impressions"] == 1500 and got[0]["conversions"] == 3
      and got[0]["conversion_value_minor"] == 12050 and got[1]["conversions"] == 0,
      "cost_micros in cents, conversions and their value as numbers")
try:
    google.pull_insights({"token": G_REFRESH, "account": "1234567890"}, [{"kind": "campaign", "external_id": "1"}],
                         ("11' OR '1", "2026-11-02"))
    check(False, "a bad date is refused")
except ValueError:
    check(True, "a day range that isn't two plain dates never reaches GAQL")
check(google.ADAPTER.pull_insights({"token": G_REFRESH, "account": "1"}, [{"kind": "ad", "external_id": "1"}],
                                   (day(1), day(0))) == [], "no campaign: no call")

# ================================================================ 3. the pull, per platform
print("the pull for Snapchat, TikTok and Google")
SNAP_STATS["snap-camp-1"] = [(day(3), {"impressions": 4000, "swipes": 80, "spend": 25_000_000, "conversion_purchases": 4,
                                       "conversion_sign_ups": 1, "conversion_purchases_value": 180_000_000}),
                             (day(2), {"impressions": 1000, "swipes": 20, "spend": 5_000_000, "conversion_purchases": 1})]
TT_REPORT[:] = [{"dimensions": {"campaign_id": "1800000000000001", "stat_time_day": f"{day(2)} 00:00:00"},
                 "metrics": {"spend": "30.50", "impressions": "6000", "clicks": "120", "conversion": "6",
                             "complete_payment": "0"}},
                {"dimensions": {"campaign_id": "1800000000000001", "stat_time_day": f"{day(1)} 00:00:00"},
                 "metrics": {"spend": "10.00", "impressions": "2000", "clicks": "30", "conversion": "2",
                             "complete_payment": "0"}},
                {"dimensions": {"campaign_id": "1899999999999999", "stat_time_day": f"{day(1)} 00:00:00"},
                 "metrics": {"spend": "99.00", "impressions": "1", "clicks": "1"}}]
G_ROWS[:] = [{"campaign": {"id": "22000000001"}, "segments": {"date": day(2)},
              "metrics": {"costMicros": "12345678", "impressions": "1500", "clicks": "60", "conversions": 3.0,
                          "conversionsValue": 120.5}},
             {"campaign": {"id": "22000000002"}, "segments": {"date": day(2)},
              "metrics": {"costMicros": "4000000", "impressions": "800", "clicks": "40"}}]
check(adinsights.pull(SNAP_C) == "ok", "Snapchat: pulled")
sr = rows(SNAP_L)
check(len(sr) == 2 and all(x["platform"] == "snapchat" and x["level"] == "campaign" and x["connection_id"] == SNAP_C
                           for x in sr), "one row per campaign per day, with its launch, connection and platform")
check(sr[0]["spend_minor"] == 2500 and sr[0]["link_clicks"] == 80 and sr[0]["results"] == 4 and sr[0]["purchases"] == 4
      and sr[0]["purchase_value_minor"] == 18000 and sr[0]["cost_per_result_minor"] == 625,
      "Snap's micro-currency spend in cents; swipes as link clicks; purchases are the results of a sales campaign")
stats_call = F.find("GET", "/stats")[-1]
check(stats_call["params"].get("granularity") == "DAY" and stats_call["path"].endswith("/campaigns/snap-camp-1/stats"),
      "Snapchat: the launched campaign's stats by day")
check(adinsights.pull(TT_C) == "ok", "TikTok: pulled")
tr = rows(TT_L)
check(len(tr) == 2 and tr[0]["spend_minor"] == 3050 and tr[0]["results"] == 6 and tr[0]["result_kind"] == "lead"
      and tr[0]["cost_per_result_minor"] == 508, "TikTok: spend in cents; a leads campaign counts its conversions")
check(not q("SELECT 1 FROM ad_insights_daily WHERE external_object_id='1899999999999999'"),
      "a campaign Campaignify didn't launch is never stored")
rq = F.find("GET", "/report/integrated/get/")[-1]["params"]
check(rq.get("advertiser_id") == "7100000000000000001" and "1800000000000001" in rq.get("filtering", ""),
      "TikTok: one report for the advertiser, filtered to the launched campaigns")
check(adinsights.pull(G_C) == "ok", "Google: pulled")
gr, gr2 = rows(G_L), rows(G_L2)
check(len(gr) == 1 and gr[0]["spend_minor"] == 1234 and gr[0]["purchases"] == 3 and gr[0]["results"] == 3
      and len(gr2) == 1 and gr2[0]["results"] == 40 and gr2[0]["result_kind"] == "link_click",
      "Google: each launched campaign's day, results by its goal")
check("22000000001" in F.find("POST", "googleAds:search")[-1]["data"]["query"]
      and "22000000002" in F.find("POST", "googleAds:search")[-1]["data"]["query"], "one search covers both campaigns")
check(not rows(QUEUED), "a launch that isn't made yet is never pulled")
conns = {r_["id"]: r_ for r_ in q("SELECT * FROM ad_connections WHERE workspace_id=?", WS)}
check(all(conns[i]["insights_at"] and conns[i]["insights_next_at"] and not conns[i]["insights_error"]
          for i in (SNAP_C, TT_C, G_C)), "each connection records the pull and the next one")

print("re-pulls update and remove")
SNAP_STATS["snap-camp-1"] = [(day(3), {"impressions": 4000, "swipes": 80, "spend": 25_000_000, "conversion_purchases": 6})]
due_now(SNAP_C)
adinsights.pull(SNAP_C)
sr = rows(SNAP_L)
check(len(sr) == 1 and sr[0]["purchases"] == 6, "a late purchase updates the day; a day the platform dropped is removed")
check(q("SELECT COUNT(*) n FROM ad_insights_daily WHERE platform='tiktok'")[0]["n"] == 2,
      "a pull of one platform leaves the others' rows alone")

print("the schedule")
due_now(SNAP_C, TT_C, G_C)
d = {i for i, _ in adinsights.due(db.connect(), 10)}
check({SNAP_C, TT_C, G_C} <= d, "every platform's connection with a launched campaign is due")
n = adinsights.tick(limit=10)
check(n >= 3, "the worker's pass pulls all of them")
check(not {SNAP_C, TT_C, G_C} & {i for i, _ in adinsights.due(db.connect(), 10)}, "and none is due again until later")
os.environ["AD_LAUNCH"] = "off"
due_now(SNAP_C)
before = len(F.calls)
adinsights.tick(limit=10)
check(len(F.calls) == before, "AD_LAUNCH off: nothing is pulled")
os.environ["AD_LAUNCH"] = "on"
ex("UPDATE ad_connections SET insights_at=? WHERE workspace_id=?", "2000-01-01T00:00:00+00:00", WS)
with db.connect() as conn:
    check(adinsights.refresh(conn, WS), "Update now")
check(len({i for i, _ in adinsights.due(db.connect(), 10)} & {SNAP_C, TT_C, G_C}) == 3,
      "Update now makes every platform's connection due")

# ================================================================ 4. errors
print("errors in plain words")
F.on("GET", "/stats", (401, {"request_status": "ERROR", "debug_message": "invalid token", "error_code": "E1003"}))
F.on("POST", "/login/oauth2/access_token", (400, {"error": "invalid_grant"}))
snapchat.forget_access(SNAP_REFRESH)
due_now(SNAP_C)
check(adinsights.pull(SNAP_C) == "failed", "an ended Snapchat grant fails the pull")
c_ = q("SELECT * FROM ad_connections WHERE id=?", SNAP_C)[0]
check(c_["status"] == "expired" and c_["insights_error"] == adinsights.words("snapchat", "auth")
      and "Reconnect" in c_["insights_error"], "and marks the connection for Reconnect, in plain words")
check(rows(SNAP_L), "the numbers from the last good pull stay")
F.on("POST", "/login/oauth2/access_token", {"access_token": "snap-access-1", "expires_in": 3600})
F.on("GET", "/stats", snap_stats)

F.on("GET", "/report/integrated/get/", {"code": 40100, "message": "Too many requests", "data": {}})
due_now(TT_C)
res = adinsights.pull(TT_C)
c_ = q("SELECT * FROM ad_connections WHERE id=?", TT_C)[0]
check(res == "waiting" and c_["insights_error"] == adinsights.words("tiktok", "rate") and c_["status"] == "active"
      and c_["insights_next_at"] > adinsights._now_iso(30), "TikTok says slow down: the pull moves later, the connection stays")
F.on("GET", "/report/integrated/get/", tt_report)

F.on("POST", "googleAds:search", (500, {"error": {"code": 500, "status": "INTERNAL", "message": "backend error"}}))
due_now(G_C)
res = adinsights.pull(G_C)
c_ = q("SELECT * FROM ad_connections WHERE id=?", G_C)[0]
check(res == "failed" and c_["insights_error"] == adinsights.words("google", "other")
      and c_["insights_next_at"] < adinsights._now_iso(16 * 60 + 5), "Google 5xx: plain words, tried again in 15 minutes")
F.on("POST", "googleAds:search", g_search)

with db.connect() as conn:
    adhttp.block("google", "1234567890", 600)
due_now(G_C)
before = len(F.calls)
check(adinsights.pull(G_C) == "waiting" and len(F.calls) == before,
      "stored usage that says wait: no request is sent")
T[0] += 700
due_now(G_C)
check(adinsights.pull(G_C) == "ok", "after the wait it pulls again")

# ================================================================ 5. the Ad campaigns page
print("the Ad campaigns page")
r = owner.get("/app/ad-campaigns")
page = r.get_data(as_text=True)
flat = text(page)
check(r.status_code == 200 and "Ember on Snap" in page and "Ember on TikTok" in page and "Ember on Google" in page
      and "Ember Search" in page, "every platform's launched campaigns are listed")
check("Snapchat ·" in flat and "TikTok ·" in flat and "Google ·" in flat, "each row says its platform")
check(flat.count("Created paused") >= 4, "a campaign made paused on another platform says so (its status isn't read back)")
check(snapchat.ADS_MANAGER in page and "aadvid=7100000000000000001" in page and google.ads_url("1234567890") in page,
      "each links to its own ads manager")
check("Snapchat Ads Manager ↗" in page and "TikTok Ads Manager ↗" in page and "Google Ads ↗" in page,
      "with the platform's own name")
check(">Manage</a>" not in page, "only Meta campaigns have Manage (the others change in their own ads manager)")
check("Turn it on, pause it or change its budget in Snapchat Ads Manager" in flat, "and the row says where to turn it on")
check("$25.00" in flat or "$25" in flat, "a row shows its spend")
check("Each ad platform reports a campaign once it has run" not in flat and "Results, all campaigns" in flat,
      "the totals panel shows")
check("Numbers from each ad platform" in flat, "the update note names no single platform")
check("Link clicks Mixed goals" not in flat and "Results Mixed goals" in flat,
      "totals over campaigns with different goals say Results, not one goal's name")
check("Not made yet" in page, "a launch still being made is listed too")
r = owner.get(f"/app/ad-campaigns/{SNAP_L}")
check(r.status_code == 404, "the Meta manage page isn't offered for a Snapchat campaign")
r = owner.post("/app/ad-campaigns/refresh", data={"csrf": csrf(owner)})
with owner.session_transaction() as s:
    fl = " ".join(m for _, m in s.get("_flashes", []))
check(r.status_code == 302 and "your ad accounts" in fl, "Update now says it updates every ad account")

other, ou = signup("platforms-other@example.com")
page = other.get("/app/ad-campaigns").get_data(as_text=True)
check("Ember on Snap" not in page and "No campaigns yet" in page, "another workspace sees none of them")
with db.connect() as conn:
    check(not admanage.listed(conn, ou["workspace_id"]) and not adinsights.workspace_totals(conn, ou["workspace_id"], "14"),
          "and no totals")
with db.connect() as conn:
    tot = adinsights.workspace_totals(conn, WS, "14")
check(len(tot) == 1 and tot[0]["cur"] == "USD" and tot[0]["spend"] == sum(x["spend_minor"] for x in q(
    "SELECT spend_minor FROM ad_insights_daily WHERE workspace_id=? AND day BETWEEN ? AND ?", WS, day(14), day(1))),
      "the workspace totals add every platform's spend")

# ================================================================ 6. no secrets anywhere
dump = json.dumps([dict(r_) for r_ in q("SELECT * FROM ad_insights_daily")] + [dict(r_) for r_ in q(
    "SELECT insights_error, last_error FROM ad_connections")])
for secret in (SNAP_REFRESH, TT_TOKEN, G_REFRESH, "snap-access-1", "ya29.google-access-1"):
    check(secret not in dump and not any(secret in ln for ln in CAP.lines), f"no {secret[:10]}... in rows or logs")

print(f"\nall {passed} checks passed")
