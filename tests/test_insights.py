"""Results of launched campaigns (roadmap M2.5): Meta insights pulled into ad_insights_daily and the Ad campaigns
dashboard. Meta is the fake Graph in tests/fakegraph.py (insights rows shaped like tests/fixtures/meta/insights_*.json,
hand-written from Meta's v26.0 reference): no request leaves the machine.

    python tests/test_insights.py      (tests/test_app.py runs it too)

  * a row in plain numbers: spend in minor units (USD, JPY), results by the campaign's goal without double counting
    (purchase, lead, link click, reach), click rate and costs computed from sums
  * the pull: level=ad, one row per ad per day, the launched campaigns only, 14 days in the account's time zone,
    paged; upserts (late purchases update), days Meta drops are removed; every 6 hours, behind AD_LAUNCH
  * Meta's rate limits: our rate budget, the stored usage headers (x-business-use-case-usage,
    x-fb-ads-insights-throttle), 17 and 80000 move the pull later instead of failing; "reduce the amount of data"
    splits the range; every recorded error code in plain words; 190 marks the connection for Reconnect
  * the dashboard: spend, results, cost per result, click rate with plain labels, per campaign and per ad, ranges
    that match Ads Manager's presets; the numbers equal Meta's sums
  * M4.2's inputs: link click rate week over week at the spend of each week
  * another workspace sees nothing; no token in any log line or row
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_insights_")
atexit.register(shutil.rmtree, TMP, True)
KEY = base64.b64encode(b"M" * 32).decode()
APP_SECRET = "fake-app-secret-insights-77"
TOKEN = "EAAinsightsTOKENsecret55555"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=KEY, AD_RATE_TIER="full",
                  META_APP_ID="1000000000000001", META_APP_SECRET=APP_SECRET, META_LOGIN_CONFIG_ID="424242424242")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adinsights  # noqa: E402
import admanage  # noqa: E402
import adplatforms  # noqa: E402
import adpolicy  # noqa: E402
import db  # noqa: E402
import launches  # noqa: E402
import manage_routes  # noqa: E402
from adfake import FakeTransport  # noqa: E402
from adplatforms import meta  # noqa: E402
from fakegraph import FakeGraph  # noqa: E402

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

# The fake clock starts at 15:00 UTC today, not at the real time: the pull test moves it on 6 hours, and from the
# real time that crossed midnight in the ad account's time zone (America/Chicago) between 00:00 and 06:00 UTC, which
# shifted every "n days ago" and failed the suite for those six hours of each day.
T = [int(time.time()) // 86400 * 86400 + 15 * 3600.0]
adhttp.CLOCK = launches.CLOCK = admanage.CLOCK = adinsights.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def later(s=31.0):
    T[0] += s


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.53.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, plan="growth"):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active' WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def ws(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def ex(sql, *args):
    with db.connect() as conn:
        conn.execute(sql, args)
        conn.commit()


def html(r):
    return r.get_data(as_text=True)


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


# ---------------------------------------------------------------- a workspace with a plan, 2 finished ads, a connection
from PIL import Image  # noqa: E402

owner, u = signup("insights-owner@example.com")
WS = u["workspace_id"]
IMG = os.path.join(TMP, "ad1.png")
Image.new("RGB", (864, 1080), (200, 120, 40)).save(IMG)
VID = os.path.join(TMP, "ad2.mp4")
open(VID, "wb").write(b"\x00\x00\x00\x18ftypmp42fake video bytes for the fake graph")
Image.new("RGB", (720, 1280), (40, 120, 200)).save(VID + ".jpg")
OKC = {"ok": True, "warnings": [], "blocks": []}
PLAN = {"angles": [{"name": "Evening ritual"}, {"name": "Lasts the month"}],
        "copy": [{"platform": "meta", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Evening ritual", "ok": True, "compliance": OKC, "fields": [
                {"name": "Primary text", "text": "Light it once. Smell it all week."}, {"name": "Headline", "text": "Ember Candle, 50 hours"}]},
            {"angle": "Lasts the month", "ok": True, "compliance": OKC, "fields": [
                {"name": "Primary text", "text": "One candle, fifty hours."}, {"name": "Headline", "text": "Burns 50 hours"}]}]}],
        "final": {"summary": "Image and video first.", "budget_split": [{"platform": "meta", "share_percent": 100}]}}
BRIEF = {"offer": "Ember Candle", "goal": "sales", "audience": "Renters 25-40", "platforms": ["meta"],
         "link": "https://embercandle.example/shop", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14}


def make_plan(ws_id, user_id, name="Ember Candle"):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, name, json.dumps(BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        for tool, kind, path, angle, params in (("ad_image", "image", IMG, "Evening ritual", {}),
                                                ("ugc_video", "video", VID, "Lasts the month", {"ad_type": "testimonial"})):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (ws_id, user_id, tool, "b", json.dumps(params), "done", 43, db.now(), db.now())).lastrowid
            conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, kind, "", path))
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                         "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, ws_id, angle, "meta", tool, "b", 43, "queued", jid, db.now()))
        conn.commit()
    return cid


def connect(ws_id, user_id, account="111000111", currency="USD"):
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
            "page_id, page_name, instagram_actor_id, instagram_username, external_user_id, token_enc, scopes, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ws_id, "meta", account, "Ember Shop", currency, "America/Chicago", "500000001", "Ember Candle Co",
             "17841400000000001", "embercandle", "10200000000000001", adplatforms.seal_token(ws_id, TOKEN),
             " ".join(meta.SCOPES), "active", user_id, db.now(), db.now())).lastrowid
        conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                     "VALUES (?,?,?,?,?,?)", (ws_id, "meta", cid, "700000002", "Ember site pixel", db.now()))
        conn.commit()
    return cid


PLAN_ID = make_plan(WS, u["id"])
CONN_ID = connect(WS, u["id"])
G = FakeGraph(account="act_111000111")
adhttp.TRANSPORT = G


_graphs = [0]


def launched(name, ws_id=WS, plan_id=None, user=None):
    """A launched (PAUSED) 2-ad campaign of the plan on a fresh fake Graph. Returns (launch id, graph)."""
    global G
    _graphs[0] += 1
    G = FakeGraph(account="act_111000111", first_id=23850000000001 + 1000 * _graphs[0])   # Meta ids never repeat
    adhttp.TRANSPORT = G
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=?", (plan_id or PLAN_ID,)).fetchone()
        crow = launches.connection(conn, ws_id)
        d = launches.draft(conn, ws(ws_id), c, crow)
        d["name"] = name
        spec, issues, card, _ = launches.review(conn, ws(ws_id), d, crow)
        assert spec is not None and not adpolicy.blocking(issues), [i.as_dict() for i in issues]
        cid = launches.save_card(conn, ws_id, (user or u)["id"], c["id"], crow, spec, card, issues)
        lid, _ = launches.approve(conn, ws(ws_id), user or u, conn.execute("SELECT * FROM approval_cards WHERE id=?",
                                                                           (cid,)).fetchone())
    for _ in range(30):
        if launches.run(lid) == "paused":
            break
        later(61)
    assert q("SELECT status FROM launches WHERE id=?", lid)[0]["status"] == "paused"
    return lid, G


def objs(lid):
    out = {"ads": []}
    for s in q("SELECT step_key, kind, external_id FROM launch_steps WHERE launch_id=? AND kind IN ('campaign','adset','ad') "
               "ORDER BY step_no", lid):
        if s["kind"] == "ad":
            out["ads"].append(s["external_id"])
        else:
            out[s["kind"]] = s["external_id"]
    return out


def change(c, lid, **form):
    data = {"csrf": csrf(c)}
    data.update(form)
    return c.post(f"/app/ad-campaigns/{lid}/change", data=data)


def card_of(lid):
    rows = q("SELECT * FROM approval_cards WHERE launch_id=? AND kind IN ('status_change','budget_change') ORDER BY id DESC "
             "LIMIT 1", lid)
    return rows[0] if rows else None


def approve(c, card, **form):
    data = {"csrf": csrf(c), "hash": card["spec_hash"]}
    data.update(form)
    return c.post(f"/app/ad-cards/{card['id']}/approve", data=data)


def fresh(cid):
    return q("SELECT * FROM approval_cards WHERE id=?", cid)[0]


def after(card):
    return json.loads(card["after_json"])


def blocks(card):
    return {i["rule_id"] for i in after(card)["issues"] if i["level"] == "block"}


def warns(card):
    return {i["rule_id"] for i in after(card)["issues"] if i["level"] != "block"}


from datetime import timedelta  # noqa: E402
from fakegraph import fixture  # noqa: E402

TZ = "America/Chicago"


def day(n):
    """The ad account's date n days ago."""
    return (adinsights.today(TZ) - timedelta(days=n)).isoformat()


def due_now(conn_id=CONN_ID):
    ex("UPDATE ad_connections SET insights_next_at=NULL, insights_error=NULL WHERE id=?", conn_id)


def rows(lid=None):
    return q("SELECT * FROM ad_insights_daily" + (" WHERE launch_id=?" if lid else "") + " ORDER BY day, external_object_id",
             *((lid,) if lid else ()))


def insight_calls(g):
    return [c for c in g.calls if c["path"].endswith("/insights")]


SALES = {"objective": "sales", "conversion": {"pixel_id": "1", "event": "Purchase"}}

# ================================================================ 1. one row
print("one row")
raw = fixture("meta/insights_ad_page1.json")["data"][0]
r = adinsights.parse(raw, SALES, "USD")
check(r["spend_minor"] == 1842 and r["impressions"] == 2310 and r["link_clicks"] == 44 and r["clicks"] == 61,
      "spend in cents, times shown, link clicks and all clicks")
check(r["results"] == 2 and r["result_kind"] == "purchase" and r["purchases"] == 2 and r["purchase_value_minor"] == 5800,
      "Purchase goal: 2 purchases (fb_pixel_purchase, purchase and omni_purchase are the same 2, not 6), $58 value")
check(r["ctr"] == round(44 * 100 / 2310, 4) and abs(r["ctr_all"] - 2.640693) < 1e-6 and r["cpc_minor"] == round(1842 / 44)
      and r["cost_per_result_minor"] == 921, "link click rate, Meta's all-clicks rate, cost per click, cost per purchase")
check(r["day"] == "2026-11-02" and r["external_object_id"] == "23850000000005" and r["campaign_external_id"] == "23850000000003"
      and json.loads(r["raw_json"])["spend"] == "18.42", "the day, the ad and campaign ids, Meta's row kept")
t = adinsights.parse(raw, {"objective": "traffic"}, "USD")
check(t["results"] == 44 and t["result_kind"] == "link_click", "Traffic goal: results are link clicks")
a_ = adinsights.parse(raw, {"objective": "awareness"}, "USD")
check(a_["results"] == 1984 and a_["result_kind"] == "reach" and a_["cost_per_result_minor"] == round(1842 * 1000 / 1984),
      "Awareness goal: people reached, cost per 1,000 reached")
lead = adinsights.parse(raw, {"objective": "leads", "conversion": {"pixel_id": "1", "event": "Lead"}}, "USD")
check(lead["results"] == 0 and lead["cost_per_result_minor"] is None and lead["result_kind"] == "lead",
      "Lead goal with no leads: 0 results, no cost per result")
check(adinsights.goal_of({"objective": "sales", "conversion": {"pixel_id": "1", "event": "AddToCart"}})[0] == "add_to_cart",
      "other pixel events are their own kind of result")
jp = adinsights.parse(dict(raw, account_currency="JPY", spend="2755"), SALES, "JPY")
check(jp["spend_minor"] == 2755 and jp["currency"] == "JPY", "JPY: whole yen (zero-decimal)")
check(adinsights.parse(dict(raw, spend="0.005"), SALES, "USD")["spend_minor"] == 1, "half a cent rounds up, not to even")
check(adinsights.parse({"ad_id": "1", "date_start": "2026-11-01", "spend": "0", "impressions": "0"}, SALES, "USD")["ctr"] is None,
      "no impressions: no click rate (not a division by zero)")

# the call itself, from the recorded pages
ft = FakeTransport()
ft.on("GET", "/act_111000111/insights", "meta/insights_ad_page1.json", "meta/insights_ad_page2.json")
adhttp.TRANSPORT = ft
got = meta.pull_insights(TOKEN, "111000111", ["23850000000003"], "2026-11-01", "2026-11-02")
p0 = ft.calls[0]["params"]
check(len(got) == 2 and len(ft.calls) == 2 and ft.calls[1]["params"]["after"] == "MQZDZD", "follows Meta's paging")
check(p0["level"] == "ad" and p0["time_increment"] == "1" and json.loads(p0["time_range"]) == {"since": "2026-11-01",
      "until": "2026-11-02"} and json.loads(p0["filtering"]) == [{"field": "campaign.id", "operator": "IN",
                                                                   "value": ["23850000000003"]}],
      "level=ad, one row per day, the date range, only the launched campaigns")
check(p0["use_unified_attribution_setting"] == "true" and "actions" in p0["fields"] and "inline_link_clicks" in p0["fields"],
      "Ads Manager's attribution setting, the fields results come from")
check(ft.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}" and "access_token" not in p0 and "appsecret_proof" in p0,
      "the token in the header, with appsecret_proof")
check(meta.pull_insights(TOKEN, "111000111", [], "2026-11-01", "2026-11-02") == [] and len(ft.calls) == 2,
      "no campaigns: no call")

# ================================================================ 2. the pull
print("the pull")
LID, G1 = launched("Ember Candle")
O = objs(LID)
ex("UPDATE launches SET approved_at=?, created_at=? WHERE id=?", day(20) + "T12:00:00+00:00", day(20) + "T12:00:00+00:00", LID)
DATA = {}
for n in range(0, 18):            # 18 days of delivery, today included
    for i, ad in enumerate(O["ads"]):
        DATA[(ad, day(n))] = {"spend": f"{10 + i * 5 + n % 3}.25", "impressions": 1000 + 100 * n + i * 50,
                              "link_clicks": 20 + n + i, "clicks": 30 + n + i, "purchases": (n + i) % 3,
                              "value": f"{((n + i) % 3) * 29}.00", "reach": 800 + n}
G1.daily = dict(DATA)
check(adinsights.tick() == 0 and not insight_calls(G1), "AD_LAUNCH unset: the worker pulls nothing")
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES="999999")
check(adinsights.tick() == 0 and not insight_calls(G1), "beta, not on the allowlist: nothing")
os.environ["AD_LAUNCH_WORKSPACES"] = str(WS)
check(adinsights.tick() == 1, "beta, on the allowlist: the connection is pulled")
calls = insight_calls(G1)
tr = json.loads(calls[0]["params"]["time_range"])
check(tr == {"since": day(14), "until": day(0)} and len(calls) == 1, "one insights call: today and the 14 days before, "
      "in the ad account's time zone")
got = rows(LID)
check(len(got) == 30 and {r_["external_object_id"] for r_ in got} == set(O["ads"]), "30 rows: 2 ads x 15 days")
want = sum(int(round(float(DATA[(ad, day(n))]["spend"]) * 100)) for ad in O["ads"] for n in range(15))
check(sum(r_["spend_minor"] for r_ in got) == want, "spend adds up to Meta's, to the cent")
conn_row = q("SELECT * FROM ad_connections WHERE id=?", CONN_ID)[0]
check(conn_row["insights_at"] and conn_row["insights_error"] is None and conn_row["insights_next_at"] > adinsights._now_iso(
    6 * 3600 - 60), "the pull is recorded and the next one is in 6 hours")
check(adinsights.tick() == 0, "nothing is due before then")
os.environ["AD_INSIGHTS_HOURS"] = "3"
check(adinsights.every_s() == 3 * 3600, "AD_INSIGHTS_HOURS changes the interval")
os.environ.pop("AD_INSIGHTS_HOURS")
check(any(c["params"].get("ids") for c in G1.calls), "the pull also reads the campaign's objects (statuses)")
# late purchases update a day; a day Meta drops is removed; nothing is duplicated
G1.daily[(O["ads"][0], day(3))] = dict(DATA[(O["ads"][0], day(3))], purchases=5, value="145.00")
del G1.daily[(O["ads"][1], day(4))]
G1.store[O["ads"][1]]["status"] = "PAUSED"          # paused in Ads Manager
later(6 * 3600 + 1)
check(adinsights.tick() == 1, "6 hours later it pulls again")
got = rows(LID)
r3 = [r_ for r_ in got if r_["external_object_id"] == O["ads"][0] and r_["day"] == day(3)][0]
check(len(got) == 29 and r3["purchases"] == 5 and r3["purchase_value_minor"] == 14500,
      "late purchases update the day; the day Meta no longer reports is removed; no duplicates")
check(q("SELECT status FROM ad_objects WHERE external_id=?", O["ads"][1])[0]["status"] == "PAUSED",
      "an ad paused in Ads Manager shows as paused")
G1.insight_page = 5
due_now()
G1.calls.clear()
adinsights.tick()
check(len(insight_calls(G1)) == 6 and len(rows(LID)) == 29, "small pages: every page is followed (29 rows in 6 calls)")
G1.insight_page = 500

# ================================================================ 3. Meta's rate limits and the stored headers
print("rate limits")
for fx in ("meta/error_17.json", "meta/error_80000_insights.json"):
    due_now()
    G1.fail("GET", "insights", fx, times=adhttp.RETRIES)
    st = adinsights.pull(CONN_ID)
    c_ = q("SELECT * FROM ad_connections WHERE id=?", CONN_ID)[0]
    check(st == "waiting" and c_["insights_error"] == adinsights.WORDS["rate"] and c_["insights_next_at"] > adinsights._now_iso(50)
          and len(rows(LID)) == 29, f"{fx.split('/')[1]}: the pull waits (next try later), the stored numbers stay")
    G1.failures.clear()
    ex("DELETE FROM api_budget")
due_now()
ex("INSERT OR REPLACE INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,?,?,?)",
   "meta", "act_111000111:insights", 0, T[0], T[0] + 200)
G1.calls.clear()
check(adinsights.pull(CONN_ID) == "waiting" and not G1.calls, "insights throttled by a stored header: Meta isn't even called")
ex("DELETE FROM api_budget")
due_now()
G1.headers = {"x-fb-ads-insights-throttle": json.dumps({"app_id_util_pct": 97, "acc_id_util_pct": 12})}
adinsights.pull(CONN_ID)
G1.headers = {}
check(adhttp.blocked_for("meta", "act_111000111:insights") > 200 and adhttp.blocked_for("meta", "act_111000111") == 0,
      "x-fb-ads-insights-throttle at 97%: insights wait 5 minutes; launches and changes on the account don't")
due_now()
G1.calls.clear()
check(adinsights.pull(CONN_ID) == "waiting" and not G1.calls, "and the next pull waits for it")
ex("DELETE FROM api_budget")
due_now()
G1.headers = {"x-business-use-case-usage": json.dumps({"111000111": [{"type": "ads_insights", "call_count": 98,
                                                                     "total_cputime": 20, "total_time": 20,
                                                                     "estimated_time_to_regain_access": 4}]})}
adinsights.pull(CONN_ID)
G1.headers = {}
check(adhttp.blocked_for("meta", "act_111000111") > 200, "x-business-use-case-usage says wait 4 minutes: the account waits")
ex("DELETE FROM api_budget")
os.environ["AD_RATE_TIER"] = "limited"
ex("INSERT OR REPLACE INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,?,?,0)",
   "meta", "act_111000111", 54.0, T[0])
due_now()
G1.calls.clear()
check(adinsights.pull(CONN_ID) == "waiting" and not insight_calls(G1), "our rate budget spent: the pull waits, no call")
os.environ["AD_RATE_TIER"] = "full"
ex("DELETE FROM api_budget")
# "reduce the amount of data": the range is split
due_now()
G1.calls.clear()
G1.fail("GET", "insights", "meta/error_1_reduce_data.json", times=adhttp.RETRIES)
st = adinsights.pull(CONN_ID)
ranges = [json.loads(c["params"]["time_range"]) for c in insight_calls(G1)][adhttp.RETRIES:]
check(st == "ok" and ranges == [{"since": day(14), "until": day(7)}, {"since": day(6), "until": day(0)}]
      and len(rows(LID)) == 29, "Meta asks for less data: the 15 days are pulled as 2 halves, nothing lost")

# ================================================================ 4. every recorded error code
print("error codes")
CASES = [("meta/error_190.json", "failed", "auth"), ("meta/error_200.json", "failed", "permission"),
         ("meta/error_10_permission.json", "failed", "permission"), ("meta/error_100_invalid.json", "failed", "other"),
         ("meta/error_3018_range.json", "failed", "other"), ("meta/error_2635_version.json", "failed", "other"),
         ("meta/error_2.json", "failed", "other"), ("meta/error_1_unknown.json", "failed", "other")]
for fx, want, words in CASES:
    due_now()
    G1.fail("GET", "insights", fx, times=adhttp.RETRIES)
    st = adinsights.pull(CONN_ID)
    G1.failures.clear()
    c_ = q("SELECT * FROM ad_connections WHERE id=?", CONN_ID)[0]
    check(st == want and c_["insights_error"] == adinsights.WORDS[words] and len(rows(LID)) == 29,
          f"{fx.split('/')[1]}: \"{adinsights.WORDS[words][:40]}...\", the stored numbers stay")
    if fx.endswith("190.json"):
        check(c_["status"] == "expired", "190 marks the connection for Reconnect")
        ex("UPDATE ad_connections SET status='active', last_error=NULL WHERE id=?", CONN_ID)
    if fx.endswith("error_2.json"):
        check(c_["insights_next_at"] < adinsights._now_iso(16 * 60), "a Meta 5xx: tried again in 15 minutes")
    if fx.endswith("100_invalid.json"):
        check(c_["insights_next_at"] > adinsights._now_iso(59 * 60), "a refusal: tried again in an hour")
import requests  # noqa: E402
due_now()
G1.fail("GET", "insights", requests.ConnectionError(f"https://graph.facebook.com/x?access_token={TOKEN}"), times=5)
check(adinsights.pull(CONN_ID) == "failed" and q("SELECT insights_error FROM ad_connections WHERE id=?", CONN_ID)[0][
    "insights_error"] == adinsights.WORDS["network"], "the network down: plain words, the stored numbers stay")
G1.failures.clear()
due_now()
check(adinsights.pull(CONN_ID) == "ok" and q("SELECT insights_error FROM ad_connections WHERE id=?", CONN_ID)[0]["insights_error"]
      is None, "the next good pull clears the message")

# ================================================================ 5. the dashboard
print("dashboard")
page = html(owner.get("/app/ad-campaigns"))
ws_ = ws(WS)
d7 = [r_ for r_ in rows(LID) if day(7) <= r_["day"] <= day(1)]
spend7 = sum(r_["spend_minor"] for r_ in d7)
res7 = sum(r_["results"] for r_ in d7)
row = page.split(f'href="/app/ad-campaigns/{LID}"', 1)[1].split("</li>", 1)[0]
check(launches.money(spend7, "USD") in row and f"<dd>{res7:,}</dd>" in row and "Purchases" in row and "Cost per purchase" in row
      and launches.money(int(round(spend7 / res7)), "USD") in row, "each campaign: last 7 days spent, purchases, cost per purchase")
check("Results, all campaigns" in page and "Last 7 days" in page and "Updated" in page and "Update now" in page,
      "the page has the totals, the range, when it was updated and Update now")
with db.connect() as conn:
    L = conn.execute("SELECT * FROM launches WHERE id=?", (LID,)).fetchone()
    f7 = adinsights.launch_figures(conn, L, "7", TZ)
    f14 = adinsights.launch_figures(conn, L, "14", TZ)
    ftd = adinsights.launch_figures(conn, L, "today", TZ)
check(f7["days"] == 7 and f7["spend"] == spend7 and f14["days"] == 14 and ftd["days"] == 1,
      "Last 7 and 14 days are complete days before today (Ads Manager's presets); Today is today")
imp7, link7 = sum(r_["impressions"] for r_ in d7), sum(r_["link_clicks"] for r_ in d7)
check(f7["ctr"] == round(link7 * 100 / imp7, 2) and f7["cpc"] == int(round(spend7 / link7)),
      "click rate and cost per click are computed from the sums, not averaged")
dpage = html(owner.get(f"/app/ad-campaigns/{LID}?range=14"))
check('aria-current=page class="on">Last 14 days' in dpage and launches.money(f14["spend"], "USD") in dpage,
      "the campaign page: the range picked, its spend")
for label in ("Spent", "Times shown", "Link clicks", "Click rate", "Cost per click", "Purchases", "Cost per purchase",
              "Purchase value", "Spent per day, last 14 days"):
    check(label in dpage, f"plain label: {label}")
tbl = dpage.split('class="mc-table"', 1)[1].split("</table>", 1)[0]
check(tbl.count("<tr>") == 3 and "Ad 1" in tbl and "Ad 2" in tbl, "a row per ad")
check(not re.search(r"[–—]", text(dpage)) and not re.search(r"[–—]", text(page)), "no em or en dashes on the dashboard")
check(owner.get(f"/app/ad-campaigns/{LID}?range=bogus").status_code == 200, "an unknown range falls back to 7 days")
# an awareness campaign reads "People reached"
ex("UPDATE launches SET spec_json=json_set(spec_json, '$.objective', 'awareness') WHERE id=?", LID)
due_now()
adinsights.pull(CONN_ID)
check("People reached" in html(owner.get(f"/app/ad-campaigns/{LID}")), "an awareness campaign reads People reached")
ex("UPDATE launches SET spec_json=json_set(spec_json, '$.objective', 'sales') WHERE id=?", LID)
due_now()
adinsights.pull(CONN_ID)
# Update now
ex("UPDATE ad_connections SET insights_next_at=? WHERE id=?", adinsights._now_iso(3600), CONN_ID)
owner.post("/app/ad-campaigns/refresh", data={"csrf": csrf(owner)})
check("in the last 15 minutes" in flashes(owner) and q("SELECT insights_next_at FROM ad_connections WHERE id=?", CONN_ID)[0][
    "insights_next_at"], "Update now within 15 minutes of an update: not again yet")
ex("UPDATE ad_connections SET insights_at=? WHERE id=?", "2020-01-01T00:00:00+00:00", CONN_ID)
r = owner.post("/app/ad-campaigns/refresh", data={"csrf": csrf(owner), "next": f"/app/ad-campaigns/{LID}?range=7#results"})
check(r.headers["Location"].endswith(f"/app/ad-campaigns/{LID}?range=7#results") and q(
    "SELECT insights_next_at FROM ad_connections WHERE id=?", CONN_ID)[0]["insights_next_at"] is None,
      "Update now makes the pull due and returns to the page")
r = owner.post("/app/ad-campaigns/refresh", data={"csrf": csrf(owner), "next": "https://evil.example/"})
check(r.headers["Location"].endswith("/app/ad-campaigns"), "Update now never redirects off the page")
adinsights.tick()

# ================================================================ 6. M4.2's inputs: click rate week over week
print("week over week")
ad = O["ads"][0]
for n in range(1, 15):
    G1.daily[(ad, day(n))] = {"spend": "20.00", "impressions": 2000, "link_clicks": 30 if n <= 7 else 50, "clicks": 60}
due_now()
adinsights.pull(CONN_ID)
with db.connect() as conn:
    w = adinsights.week_over_week(conn, ad, TZ)
check(w["complete"] and w["this"]["spend"] == 14000 and w["last"]["spend"] == 14000 and w["spend_change"] == 0.0,
      "two full weeks, the same spend")
check(w["this"]["ctr"] == 1.5 and w["last"]["ctr"] == 2.5 and w["ctr_change"] == -40.0,
      "link click rate 2.5% then 1.5%: down 40% week over week (the M4.2 flag fires at 30%)")
with db.connect() as conn:
    w2 = adinsights.week_over_week(conn, "999999", TZ)
check(not w2["complete"] and w2["ctr_change"] is None, "an ad without two weeks of delivery isn't complete")

# ================================================================ 7. scoping and secrets
print("scoping and secrets")
other, ou = signup("insights-other@example.com")
os.environ["AD_LAUNCH_WORKSPACES"] = f"{WS},{ou['workspace_id']}"
opage = html(other.get("/app/ad-campaigns"))
check("Ember Candle" not in opage and launches.money(spend7, "USD") not in opage, "another workspace sees none of it")
check(other.post("/app/ad-campaigns/refresh", data={"csrf": csrf(other)}).status_code == 302
      and q("SELECT insights_next_at FROM ad_connections WHERE id=?", CONN_ID)[0]["insights_next_at"],
      "its Update now doesn't touch this workspace's connection")
os.environ["AD_LAUNCH"] = "off"
check(owner.post("/app/ad-campaigns/refresh", data={"csrf": csrf(owner)}).status_code == 404, "AD_LAUNCH off: Update now is a 404")
os.environ["AD_LAUNCH"] = "beta"
logs = "\n".join(CAP.lines)
check(TOKEN not in logs and APP_SECRET not in logs, "no token or app secret in any log line")
dump = json.dumps([dict(r_) for r_ in q("SELECT * FROM ad_insights_daily")] + [dict(r_) for r_ in q(
    "SELECT insights_at, insights_error, insights_next_at FROM ad_connections")])
check(TOKEN not in dump and APP_SECRET not in dump, "no token or app secret in any insights row")

print(f"all {passed} checks passed")
