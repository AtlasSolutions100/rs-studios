"""Launching a plan on Google Ads (roadmap M3.2 Search with a responsive search ad, M3.3 Performance Max) on a
throwaway database. Google is the fake REST endpoint in tests/fakegoogleads.py (answers shaped like
tests/fixtures/google/*.json, hand-written from the v25 REST reference, to re-record with
tools/google_sandbox_launch.py): no request leaves the machine.

    python tests/test_google_launch.py      (tests/test_app.py runs it too)

  * GOOGLE_ADS off: the Google Launch page, its approve and the kit button are 404 / absent; on: they show
  * the draft from the plan: the planner's keyword list with match types and negatives, the copy studio's Google
    headlines and descriptions, the budget share for Google, the account's currency
  * the approval card says exactly what will be made (1 campaign PAUSED, bidding, where, keywords, the ad, pins)
    and the launch makes exactly that in ONE GoogleAdsService.Mutate with temporary ids; a GAQL check reads it back
  * the rules: D2 paid plan, D3 ceiling in the account's currency (zero-decimal too), RSA counts and lengths, pins
    that leave positions 1 and 2 empty, keywords, countries Campaignify can target, political ads, members can't
    approve, another workspace's card is a 404
  * idempotency: approving twice is one launch, a finished launch run again makes nothing, a mutate whose answer
    was lost is found by its tagged name (and DUPLICATE_CAMPAIGN_NAME is read as "already made")
  * every error code a launch can meet: plain words, retry, wait or fail; Reconnect marks the connection;
    a failure after the mutate keeps the campaign PAUSED and "Try again" reuses it
  * no token in any log line, page or launch row
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
TMP = tempfile.mkdtemp(prefix="rs_glaunch_")
atexit.register(shutil.rmtree, TMP, True)
CLIENT_SECRET = "GOCSPX-fake-client-secret-2718"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=base64.b64encode(b"H" * 32).decode(),
                  GOOGLE_OAUTH_CLIENT_ID="555-fake.apps.googleusercontent.com", GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET)
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "GOOGLE_ADS", "AD_RATE_TIER", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED",
          "RS_PLAN_PAYWALL", "META_APP_ID", "META_APP_SECRET", "META_LOGIN_CONFIG_ID"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import adspec  # noqa: E402
import db  # noqa: E402
import launch_google  # noqa: E402
import launch_routes  # noqa: E402
import launches  # noqa: E402
from adplatforms import google  # noqa: E402
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
            self.lines.append(record.getMessage() + (logging.Formatter().formatException(record.exc_info)
                                                     if record.exc_info else ""))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


CAP = Capture()
for name in ("", "campaignify", "rs", "werkzeug", "urllib3"):
    lg = logging.getLogger(name)
    lg.addHandler(CAP)
    lg.setLevel(logging.DEBUG)
T = [time.time()]
adhttp.CLOCK = launches.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
launch_routes.APPROVALS_PER_HOUR = launch_routes.UPDATES_PER_HOUR = 10 ** 6    # many launches here; the limit is test_launches'
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.61.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


def html(r):
    return r.get_data(as_text=True)


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


# ---------------------------------------------------------------- a workspace with a plan and a Google connection
OKC = {"ok": True, "warnings": [], "blocks": []}
GCOPY = [{"platform": "google", "kind": "ad", "angle": "all", "variants": [
    {"angle": "Fast help", "ok": True, "compliance": OKC, "fields": [
        {"name": "Headline 1", "text": "Same-Day AC Repair"}, {"name": "Headline 2", "text": "Austin HVAC Pros"},
        {"name": "Headline 3", "text": "Call For A Free Quote"},
        {"name": "Description 1", "text": "Licensed techs fix most AC problems on the first visit."},
        {"name": "Description 2", "text": "Upfront prices. Book online in 2 minutes."}]},
    {"angle": "Local", "ok": True, "compliance": OKC, "fields": [
        {"name": "Headline 1", "text": "Austin AC Repair"}, {"name": "Headline 2", "text": "Same-Day AC Repair"},
        {"name": "Headline 3", "text": "Family Owned Since 1998"},
        {"name": "Description 1", "text": "Serving Austin and Round Rock with repairs and tune-ups."},
        {"name": "Description 2", "text": "This headline is far too long to ever fit in a Google description field "
                                          "because it keeps going."}]}]}]
KEYWORDS = {"items": [{"term": "ac repair austin", "match": "exact"}, {"term": "ac repair near me", "match": "phrase"},
                      {"term": "air conditioner repair", "match": "broad"}],
            "source": "model", "negatives": ["jobs", "diy"]}
PLAN = {"angles": [{"name": "Fast help"}], "copy": GCOPY,
        "final": {"summary": "Search first.", "keywords": KEYWORDS,
                  "budget_split": [{"platform": "google", "share_percent": 60}, {"platform": "meta", "share_percent": 40}],
                  "checklist": []}}
BRIEF = {"offer": "AC repair in Austin", "goal": "leads", "audience": "Homeowners", "platforms": ["google", "meta"],
         "link": "https://coolair.example/quote", "budget_amount": 50, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 30, "budget": "$50 a day for 30 days"}

owner, u = signup("glaunch-owner@example.com")
WS = u["workspace_id"]


def make_plan(ws_id, user_id, name="Cool Air AC repair", plan=PLAN, brief=BRIEF, product_id=None):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "product_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, name, json.dumps(brief), "ready", json.dumps(plan), "[]", product_id,
                            db.now(), db.now())).lastrowid
        conn.commit()
    return cid


SUB = "108700000000000000077"
REFRESH = "1//fake-refresh-launch-secretvalue-0001"
F = FakeGoogleAds()
F.customer("9998887776", "Cool Air MCC", manager=True)
F.customer("5556667778", "Cool Air", parent="9998887776")
F.refresh[REFRESH] = SUB
F.access_of[SUB] = ["9998887776"]
adhttp.TRANSPORT = F


def connect(ws_id, user_id, account="5556667778", login="9998887776", currency="USD", refresh=REFRESH, name="Cool Air"):
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
            "business_id, external_user_id, token_enc, scopes, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ws_id, "google", account, name, currency, "America/Chicago", login, SUB, adplatforms.seal_token(ws_id, refresh),
             " ".join(google.SCOPES), "active", user_id, db.now(), db.now())).lastrowid
        conn.commit()
    return cid


PLAN_ID = make_plan(WS, u["id"])
CONN_ID = connect(WS, u["id"])
URL = f"/app/campaigns/{PLAN_ID}/launch/google"


def card_page(c, cid=PLAN_ID, **form):
    if form:
        data = {"csrf": csrf(c), "form": "1"}
        data.update(form)
        r = c.post(f"/app/campaigns/{cid}/launch/google", data=data)
        assert r.status_code == 302, (r.status_code, html(r)[:300])
        card_id = int(re.search(r"card=(\d+)", r.headers["Location"]).group(1))
        page = html(c.get(r.headers["Location"]))
    else:
        page = html(c.get(f"/app/campaigns/{cid}/launch/google"))
        card_id = int(re.search(r'name="card" value="(\d+)"', page).group(1))
    return q("SELECT * FROM approval_cards WHERE id=?", card_id)[0], page


def approve(c, card, cid=PLAN_ID):
    r = c.post(f"/app/campaigns/{cid}/launch/google/approve", data={"csrf": csrf(c), "card": card["id"],
                                                                    "hash": card["spec_hash"]})
    m = re.search(r"/app/launches/(\d+)", r.headers.get("Location", ""))
    return r, (int(m.group(1)) if m else None)


def drive(lid, max_runs=60, step=61.0):
    for _ in range(max_runs):
        launches.run(lid)
        st = q("SELECT status FROM launches WHERE id=?", lid)[0]["status"]
        if st in launches.FINISHED:
            return st
        T[0] += step
    return st


def mutate_ops(call):
    return call["data"]["mutateOperations"]


# ================================================================ 1. the flags
print("flags")
check(owner.get(URL).status_code == 404, "AD_LAUNCH unset: the Google Launch page is a 404")
os.environ["AD_LAUNCH"] = "on"
check(owner.get(URL).status_code == 404 and owner.post(URL + "/approve", data={"csrf": csrf(owner)}).status_code == 404,
      "AD_LAUNCH on, GOOGLE_ADS unset (the default): the Google page and its approve are 404")
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
gblock = plan_page.split('id="kit-google"', 1)[1].split("</article>", 1)[0] if 'id="kit-google"' in plan_page else ""
check(gblock and "Create paused campaign" not in gblock, "GOOGLE_ADS unset: the kit's Google block has no Create button")
os.environ["GOOGLE_ADS"] = "1"
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
gblock = plan_page.split('id="kit-google"', 1)[1].split("</article>", 1)[0]
check("Create paused campaign" in gblock and URL in gblock and "Google Ads account, PAUSED until you turn it on" in gblock,
      "both flags on: the kit's Google block has the button and one line why")
check(owner.get(URL).status_code == 200, "the Google Launch page opens")

# ================================================================ 2. the draft and the card (Search)
print("draft and approval card")
card, page = card_page(owner)
spec0, c0 = json.loads(card["spec_json"]), json.loads(card["after_json"])
g0 = spec0["google"]
check(spec0["platform"] == "google" and g0["type"] == "search" and spec0["objective"] == "leads"
      and spec0["creatives"] == [], "a plan with a keyword list drafts a Search campaign for its Leads goal")
check(spec0["daily_budget_minor"] == 3000 and spec0["currency"] == "USD",
      "the budget is the plan's share for Google: 60% of $50 a day = $30, in the account's currency")
check(g0["keywords"] == KEYWORDS["items"] and g0["negative_keywords"] == [{"term": "jobs", "match": "phrase"},
                                                                       {"term": "diy", "match": "phrase"}],
      "the planner's keywords with their match types, and its negatives")
check(g0["headlines"] == ["Same-Day AC Repair", "Austin HVAC Pros", "Call For A Free Quote", "Austin AC Repair",
                          "Family Owned Since 1998"] and len(g0["descriptions"]) == 3,
      "the copy studio's Google headlines and descriptions, duplicates and over-long ones dropped")
check(g0["final_url"] == "https://coolair.example/quote" and g0["bidding"] == "conversions",
      "the landing page from the brief; Leads bids for conversions")
lines = c0["lines"]
check(lines[0] == "1 campaign: Cool Air AC repair. Search, $30 a day, Maximize conversions, PAUSED.",
      "card: 1 campaign, type, budget, bidding, PAUSED")
check(lines[1] == "Shows on Google Search and its search partners to people in US, searching in English.",
      "card: where it shows")
check(lines[2] == '1 ad group with 3 keywords: [ac repair austin], "ac repair near me", air conditioner repair. '
      '2 negative keywords: "jobs", "diy".', "card: the ad group and every keyword the way Google writes them")
check(lines[3] == "1 responsive search ad: 5 headlines, 3 descriptions, to https://coolair.example/quote.",
      "card: the ad, its counts and the landing page")
check("Cool Air (555-666-7778)" in page and "through manager 999-888-7776" in page and
      "What Campaignify will create on Google" in page, "the page shows the card with the Google Ads account")
check("Nothing runs until you turn the campaign on." in c0["footer"], "card: nothing runs until the campaign is turned on")
warns = {i["rule_id"] for i in c0["issues"]}
check("google.conversions_needed" in warns and not c0["blocking"],
      "a warning that Google bids for tracked conversions, and nothing blocks")
check("<button class=\"btn\">Create paused campaign</button>" in page, "the owner can approve")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the Google Launch page")
check(card_page(owner)[0]["id"] == card["id"], "opening the page again reuses the same undecided card")

# the member's edits: keywords in Google's notation, pins, paths, bidding
form = {"card": card["id"], "keywords": '[ac repair austin]\n"emergency ac repair"\nhvac service\n',
        "negatives": "jobs\n", "head_n": "5", "h_0": "Cool Air Austin", "hpin_0": "1", "h_1": "Same-Day AC Repair",
        "h_2": "Call For A Free Quote", "h_3": "Austin AC Repair", "h_4": "", "desc_n": "3",
        "d_0": "Licensed techs fix most AC problems on the first visit.", "d_1": "Upfront prices. Book online in 2 minutes.",
        "d_2": "", "path1": "ac repair", "path2": "austin", "bidding": "clicks", "partners": "1", "language": "en",
        "landing": "https://coolair.example/quote", "countries": "US", "daily_budget": "25"}
card, page = card_page(owner, **form)
g1 = json.loads(card["spec_json"])["google"]
check(g1["keywords"] == [{"term": "ac repair austin", "match": "exact"}, {"term": "emergency ac repair", "match": "phrase"},
                         {"term": "hvac service", "match": "broad"}], "keywords edited as [exact], \"phrase\" and broad")
check(g1["headlines"][0] == "Cool Air Austin" and g1["headline_pins"] == [1, 0, 0, 0] and len(g1["descriptions"]) == 2
      and g1["path1"] == "ac-repair" and g1["bidding"] == "clicks", "a pinned headline, display path, Maximize clicks")
c1 = json.loads(card["after_json"])
check('Pinned: "Cool Air Austin" in headline position 1.' in c1["lines"][3] and "Maximize clicks" in c1["lines"][0]
      and "$25 a day" in c1["lines"][0], "the card shows the pin, the bidding and the new budget")
check("google.pinned" in {i["rule_id"] for i in c1["issues"]}, "pinning gets a gentle warning")


def review_with(plan=None, **changes):
    if plan:
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET plan=?, plan_status='active', plan_override=NULL, comp_plan=NULL WHERE id=?",
                         (plan, WS))
            conn.commit()
    d = json.loads(card["spec_json"])
    gch = changes.pop("g", None)
    if gch:
        d["google"] = dict(d["google"], **gch)
    d.update(changes)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
        spec, issues, cd, steps = launches.review(conn, ws(WS), d, row)
    return {i.rule_id for i in issues if i.level == "block"}, issues, cd, steps


print("rules")
b, _, _, _ = review_with(plan="free")
check("launch.paid_plan" in b, "D2: a free workspace can't launch on Google either")
for plan, ok_minor, over_minor in (("starter", 20000, 20100), ("growth", 100000, 100100)):
    b_ok, _, _, _ = review_with(plan=plan, daily_budget_minor=ok_minor)
    b_over, _, _, _ = review_with(plan=plan, daily_budget_minor=over_minor)
    check("launch.budget_ceiling" not in b_ok and "launch.budget_ceiling" in b_over, f"D3: the {plan} ceiling holds on Google")
for g, rule, what in ((dict(headlines=["One", "Two"], headline_pins=[0, 0]), "google.rsa_headlines", "2 headlines"),
                      (dict(headlines=["Same-Day AC Repair In All Of Austin", "B", "C"], headline_pins=[0, 0, 0]),
                       "google.rsa_headline_long", "a headline over 30 characters"),
                      (dict(descriptions=["Only one."], description_pins=[0]), "google.rsa_descriptions", "1 description"),
                      (dict(headline_pins=[3, 3, 3, 3]), "google.pin_fill", "every headline pinned to position 3"),
                      (dict(headline_pins=[1, 1, 1, 1]), "google.pin_fill", "every headline pinned to position 1"),
                      (dict(keywords=[]), "google.keywords", "no keywords"),
                      (dict(keywords=[{"term": "x " * 11, "match": "exact"}]), "google.keyword_long", "an 11-word keyword"),
                      (dict(path1="a-very-long-path-part"), "google.path_long", "a 21-character display path"),
                      (dict(final_url="http://coolair.example"), "spec.destination_url", "a landing page without https"),
                      (dict(language="xx"), "google.language", "a language Campaignify doesn't set")):
    b, _, _, _ = review_with(plan="growth", g=g)
    check(rule in b, f"blocks {what} ({rule})")
b, _, _, _ = review_with(geo={"countries": ["US", "KP"]})
check("google.country_unsupported" in b, "a country Campaignify can't target on Google blocks")
b, _, _, _ = review_with(geo={"countries": ["US"], "zips": ["78701"]})
check("google.geo_detail" in b, "zips block for now (whole countries only; narrow it in Google Ads)")
b, _, _, _ = review_with(special_ad_categories=["social_issues"])
check("google.political" in b, "political ads block (Google's own verification first)")
_, iss, _, _ = review_with(audience={"age_min": 30, "age_max": 65, "genders": ["women"]})
check("google.audience_open" in {i.rule_id for i in iss}, "ages and gender say they're left open on Google")
b, _, _, _ = review_with(g={"headlines": ["Cures your diabetes fast", "B2", "C3"], "headline_pins": [0, 0, 0]},
                         vertical="health")
check(any(r.startswith("policy.") for r in b), "the policy linter runs on Google headlines")
with db.connect() as conn:
    meta_row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
    _, iss, _, steps = launches.review(conn, ws(WS), dict(json.loads(card["spec_json"]), currency="EUR"), meta_row)
check("launch.currency" in {i.rule_id for i in iss}, "a spec in another currency than the account is refused")
member, mu = signup("glaunch-member@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET workspace_id=?, role='member' WHERE id=?", (WS, mu["id"]))
    conn.commit()
mpage = html(member.get(URL + f"?card={card['id']}"))
check("Only the workspace owner can approve" in mpage, "a member sees the card with the approve button off")
r, lid_m = approve(member, card)
check(r.status_code == 403 and lid_m is None, "a member's approve is refused (403)")
other, ou = signup("glaunch-other@example.com")
r = other.post(URL + "/approve", data={"csrf": csrf(other), "card": card["id"], "hash": card["spec_hash"]})
check(r.status_code == 404, "another workspace can't approve this plan's card (404)")
check(owner.get(f"/app/campaigns/{PLAN_ID}/launch/meta?card={card['id']}").status_code == 404,
      "a Google card can't be opened on the Meta page")

# ================================================================ 3. the launch: one mutate, PAUSED
print("search launch")
card, _ = card_page(owner, **form)
F.calls.clear()
r, lid = approve(owner, card)
check(r.status_code == 302 and lid and "creating the paused campaign on Google" in flashes(owner),
      "approving the card queues the launch")
L = q("SELECT * FROM launches WHERE id=?", lid)[0]
steps = q("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", lid)
check(L["platform"] == "google" and L["connection_id"] == CONN_ID and [s["step_key"] for s in steps] == ["campaign", "check"],
      "the ledger: one campaign step (the whole mutate) and a check")
check(not F.calls, "nothing is sent to Google at approval")
st = drive(lid)
check(st == "paused", "the saga runs to paused")
muts = F.find("/googleAds:mutate")
check(len(muts) == 1 and F.mutates == 1, "exactly one GoogleAdsService.Mutate")
ops = mutate_ops(muts[0])
kinds = [next(iter(o)) for o in ops]
check(kinds[:2] == ["campaignBudgetOperation", "campaignOperation"] and kinds[-1] == "adGroupAdOperation"
      and kinds.index("adGroupOperation") < kinds.index("adGroupCriterionOperation"),
      "in order: budget, campaign, criteria, ad group, keywords, the ad")
camp = ops[1]["campaignOperation"]["create"]
check(camp["resourceName"] == "customers/5556667778/campaigns/-2" and camp["campaignBudget"] == "customers/5556667778/campaignBudgets/-1"
      and ops[0]["campaignBudgetOperation"]["create"]["resourceName"].endswith("/-1"), "temporary ids tie them together")
check(camp["status"] == "PAUSED" and camp["advertisingChannelType"] == "SEARCH" and "targetSpend" in camp
      and camp["networkSettings"]["targetContentNetwork"] is False and camp["containsEuPoliticalAdvertising"]
      == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", "the campaign: PAUSED, Search, Maximize clicks, no Display, EU field set")
check(camp["name"] == f"Cool Air AC repair [cfy:{lid}:campaign]" and camp["startDateTime"].endswith(" 00:00:00")
      and camp["endDateTime"].endswith(" 23:59:59"), "the name carries the cfy tag; start and end dates")
check(ops[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "25000000", "$25 a day as 25,000,000 micros")
crit = [o["campaignCriterionOperation"]["create"] for o in ops if "campaignCriterionOperation" in o]
check({"geoTargetConstant": "geoTargetConstants/2840"} in [c.get("location") for c in crit]
      and {"languageConstant": "languageConstants/1000"} in [c.get("language") for c in crit]
      and any(c.get("negative") and c["keyword"] == {"text": "jobs", "matchType": "BROAD"} for c in crit),
      "the US, English and the negative keyword at campaign level")
kws = [o["adGroupCriterionOperation"]["create"]["keyword"] for o in ops if "adGroupCriterionOperation" in o]
check(kws == [{"text": "ac repair austin", "matchType": "EXACT"}, {"text": "emergency ac repair", "matchType": "PHRASE"},
              {"text": "hvac service", "matchType": "BROAD"}], "3 keywords with their match types")
ad = ops[-1]["adGroupAdOperation"]["create"]["ad"]
check(ad["finalUrls"] == ["https://coolair.example/quote"] and ad["responsiveSearchAd"]["headlines"][0] ==
      {"text": "Cool Air Austin", "pinnedField": "HEADLINE_1"} and len(ad["responsiveSearchAd"]["headlines"]) == 4
      and len(ad["responsiveSearchAd"]["descriptions"]) == 2 and ad["responsiveSearchAd"]["path1"] == "ac-repair",
      "the responsive search ad: headlines with the pin, descriptions, the path and the landing page")
check(muts[0]["headers"]["login-customer-id"] == "9998887776", "the mutate goes through the manager (login-customer-id)")
made = F.objects("campaign")
check(len(made) == 1 and made[0]["status"] == "PAUSED" and len(F.objects("ad_group")) == 1 and len(F.objects("keyword")) == 3
      and len(F.objects("ad")) == 1, "Google holds exactly 1 campaign (PAUSED), 1 ad group, 3 keywords, 1 ad")
L = q("SELECT * FROM launches WHERE id=?", lid)[0]
objs = q("SELECT * FROM ad_objects WHERE launch_id=? ORDER BY id", lid)
check(L["external_campaign_id"] == made[0]["id"] and [o["kind"] for o in objs] == ["campaign", "adgroup", "ad"]
      and objs[1]["parent_external_id"] == made[0]["id"] and objs[2]["parent_external_id"] == objs[1]["external_id"],
      "ad_objects: the campaign, its ad group and the ad, linked")
chk = json.loads(q("SELECT result_json FROM launch_steps WHERE launch_id=? AND step_key='check'", lid)[0]["result_json"])
gaql = [c for c in F.find("/googleAds:search") if "FROM ad_group_ad" in c["data"]["query"]]
check(chk["campaign_status"] == "PAUSED" and chk["review"] == "REVIEW_IN_PROGRESS" and gaql,
      "the check reads it back with GAQL: PAUSED, the ad in review")
c_appr = json.loads(q("SELECT after_json FROM approval_cards WHERE id=?", card["id"])[0]["after_json"])
check(f"{len(kws)} keywords" in c_appr["lines"][2] and "4 headlines, 2 descriptions" in c_appr["lines"][3],
      "the card's text matches what was made")
spage = html(owner.get(f"/app/launches/{lid}"))
check("Paused campaign created" in spage and "Google Ads account 5556667778, PAUSED" in spage and "Open Google Ads" in spage
      and "Google reviews the ad" in spage and "on, inside the paused campaign" in spage,
      "the status page: on Google, PAUSED, the review, the ad group on inside the paused campaign")
check(not re.search(r"[–—]", text(spage)), "no em or en dashes on the status page")

# ================================================================ 4. idempotency
print("idempotency")
r, lid2 = approve(owner, card)
check(lid2 == lid and len(q("SELECT * FROM launches")) == 1, "approving the same card again is the same launch")
F.calls.clear()
with db.connect() as conn:
    conn.execute("UPDATE launches SET status='queued' WHERE id=?", (lid,))
    conn.commit()
drive(lid)
check(not F.find("/googleAds:mutate") and len(F.objects("campaign")) == 1, "running a finished launch again makes nothing")


def new_launch(name, extra=None, cur_conn=CONN_ID):
    pid = make_plan(WS, u["id"], name=name)
    cd, _ = card_page(owner, cid=pid, **dict(form, **(extra or {})))
    _, lid_ = approve(owner, cd, cid=pid)
    return pid, lid_


_, lid_lost = new_launch("Lost answer")
F.fail("googleAds:mutate", "lost")
F.calls.clear()
st = drive(lid_lost)
camps = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid_lost}:campaign]")]
check(st == "paused" and len(camps) == 1 and F.mutates == 2,
      "a mutate whose answer was lost: the retry finds the campaign by its tag and makes nothing new")
check(len([o for o in q("SELECT * FROM ad_objects WHERE launch_id=?", lid_lost)]) == 3,
      "and still records the campaign, ad group and ad (read back with GAQL)")
check(any("LIKE" in c["data"]["query"] and f"[[]cfy:{lid_lost}:campaign[]]" in c["data"]["query"]
          for c in F.find("/googleAds:search")), "the lookup is GAQL on the tagged name (brackets escaped for LIKE)")
# DUPLICATE_CAMPAIGN_NAME: made by a run we never heard back from, while the step had no tries counted yet
_, lid_dup = new_launch("Duplicate answer")
with db.connect() as conn:
    L_ = conn.execute("SELECT * FROM launches WHERE id=?", (lid_dup,)).fetchone()
    s_ = conn.execute("SELECT * FROM launch_steps WHERE launch_id=? AND step_key='campaign'", (lid_dup,)).fetchone()
ad_ = adplatforms.adapter("google")
ctx = {"launch_id": lid_dup, "account": "5556667778", "token": REFRESH, "login": "9998887776", "results": {}, "lookup": False}
ad_.execute_step(launches._step_dict(s_), ctx)               # made once, outside the saga (a crashed worker)
st = drive(lid_dup)
camps = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid_dup}:campaign]")]
check(st == "paused" and len(camps) == 1, "Google's DUPLICATE_CAMPAIGN_NAME is read as already made: found, not doubled")

# ================================================================ 5. errors
print("errors")
WORDS = {
    "error_policy_finding.json": ("Google's ad policies flagged some of the ad text", "failed"),
    "error_policy_violation_keyword.json": ("Google's ad policies flagged a keyword", "failed"),
    "error_budget_too_small.json": ("under what Google takes", "failed"),
    "error_user_permission_denied.json": ("can't make campaigns in that Google Ads account", "failed"),
    "error_customer_not_enabled.json": ("isn't active", "failed"),
    "error_cloud_project_not_approved.json": ("approved for test accounts only", "failed"),
    "error_developer_token_not_approved.json": ("approved for test accounts only", "failed"),
    "error_action_not_permitted.json": ("access level", "failed"),
    "error_invalid_login_customer.json": ("manager account this Google Ads account was reached through", "failed"),
    "error_not_allowlisted.json": ("hasn't opened this feature", "failed"),
    "error_image_aspect.json": ("image's shape", "failed"),
    "error_field_required.json": ("Google said: The required field was not present.", "failed"),
    "error_oauth_token_expired.json": ("Reconnect it in Settings", "reconnect"),
    "error_oauth_token_revoked.json": ("Reconnect it in Settings", "reconnect"),
    "error_not_ads_user.json": ("no Google Ads account", "failed"),
}
for name, (words, kind) in WORDS.items():
    with db.connect() as conn:
        conn.execute("UPDATE ad_connections SET status='active' WHERE id=?", (CONN_ID,))
        conn.commit()
    _, lidx = new_launch(f"Err {name[6:-5]}")
    before = F.mutates
    F.fail("googleAds:mutate", f"google/{name}")
    st = drive(lidx)
    L_ = q("SELECT * FROM launches WHERE id=?", lidx)[0]
    s_ = q("SELECT * FROM launch_steps WHERE launch_id=? AND step_key='campaign'", lidx)[0]
    conn_st = q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"]
    check(st == "rolled_back" and words in (L_["error"] or "") and F.mutates == before and s_["status"] == "failed"
          and "Google code" in (s_["error"] or "") and "request id" in (s_["error"] or "")
          and (conn_st == "expired") == (kind == "reconnect"),
          f"{name[6:-5]}: stops with nothing made, in plain words" + (", the connection needs Reconnect" if kind == "reconnect" else ""))
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='active' WHERE id=?", (CONN_ID,))
    conn.commit()
for name in ("error_internal.json", "error_transient.json", "error_concurrent_modification.json"):
    _, lidx = new_launch(f"Retry {name[6:-5]}")
    F.fail("googleAds:mutate", f"google/{name}", times=1)
    st = drive(lidx)
    check(st == "paused" and len([o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lidx}:campaign]")]) == 1,
          f"{name[6:-5]}: tried again (after a lookup) and made once")
_, lidx = new_launch("Retry internal x3")
F.fail("googleAds:mutate", "google/error_internal.json", times=10)
st = drive(lidx)
err_ = q("SELECT error FROM launches WHERE id=?", lidx)[0]["error"]
check(st == "rolled_back" and ("Google refused" in err_ or "Google said" in err_), "a 5xx that keeps coming stops after the step's tries")
F.failures.clear()
_, lidx = new_launch("Quota")
F.fail("googleAds:mutate", "google/error_quota.json", times=1)
launches.run(lidx)
L_ = q("SELECT * FROM launches WHERE id=?", lidx)[0]
check(L_["status"] == "launching" and L_["next_at"], "a quota error makes the launch wait, not fail")
check(drive(lidx) == "paused", "and it goes through after the wait")
_, lidx = new_launch("Quota forever")
F.fail("googleAds:mutate", "google/error_quota.json", times=1000)
st = drive(lidx, max_runs=200, step=600)
check(st == "rolled_back" and "Google kept asking us to slow down" in q("SELECT error FROM launches WHERE id=?", lidx)[0]["error"],
      "a rate limit that lasts over 2 hours stops the launch in plain words")
F.failures.clear()
_, lidx = new_launch("Network")
import requests  # noqa: E402
F.fail("googleAds:mutate", requests.ConnectionError("boom"), times=10)
st = drive(lidx)
check(st == "rolled_back" and q("SELECT error FROM launches WHERE id=?", lidx)[0]["error"] == google.NETWORK_WORDS,
      "a network failure that persists: we couldn't reach Google")
F.failures.clear()
# a revoked refresh token: invalid_grant at the token endpoint
_, lidx = new_launch("Revoked grant")
google._ACCESS.clear()
F.refresh.pop(REFRESH)
st = drive(lidx)
check(st == "rolled_back" and "Reconnect" in q("SELECT error FROM launches WHERE id=?", lidx)[0]["error"]
      and q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"] == "expired",
      "a revoked Google grant (invalid_grant): stop, and the connection needs Reconnect")
F.refresh[REFRESH] = SUB
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='active', last_error=NULL WHERE id=?", (CONN_ID,))
    conn.commit()

# a failure after the mutate: the campaign stays PAUSED, "Try again" reuses it
_, lidx = new_launch("Check fails")
orig = F._search
calls_ = {"n": 0}


def flaky(cid, query):
    if "FROM ad_group_ad" in query and "policy_summary" in query and calls_["n"] == 0:
        calls_["n"] += 1
        from fakegoogleads import Refuse
        raise Refuse("google/error_field_required.json")
    return orig(cid, query)


F.failures.clear()
F._search = flaky
st = drive(lidx)
F._search = orig
steps_ = {s["step_key"]: s for s in q("SELECT * FROM launch_steps WHERE launch_id=?", lidx)}
camps = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lidx}:campaign]")]
check(st == "rolled_back" and steps_["campaign"]["status"] == "kept" and len(camps) == 1 and camps[0]["status"] == "PAUSED",
      "a failure after the mutate keeps the campaign, PAUSED (reported, not deleted)")
spage = html(owner.get(f"/app/launches/{lidx}"))
check("the campaign stays PAUSED, so trying again reuses it" in spage, "the status page says so")
before = F.mutates
r = owner.post(f"/app/launches/{lidx}/retry", data={"csrf": csrf(owner)})
check("still has from the last try is reused" in flashes(owner) and "Google" in flashes(owner), "Try again says it reuses")
st = drive(lidx)
check(st == "paused" and F.mutates == before and len([o for o in F.objects("campaign")
                                                       if o["name"].endswith(f"[cfy:{lidx}:campaign]")]) == 1,
      "Try again checks the kept campaign is still there and reuses it: no second mutate")

# zero-decimal currency
check(google.micros(2000, "USD") == "20000000" and google.micros(5000, "JPY") == "5000000000",
      "budgets go in micros: 20.00 USD is 20,000,000; 5,000 JPY is 5,000,000,000")
JPY_CONN = connect(WS, u["id"], account="1112223334", login=None, currency="JPY", name="Cool Air Japan")
F.customer("1112223334", "Cool Air Japan", currency="JPY")
F.access_of[SUB].append("1112223334")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='revoked' WHERE id=?", (CONN_ID,))
    conn.commit()
pid = make_plan(WS, u["id"], name="Yen plan")
cdj, _ = card_page(owner, cid=pid, **dict(form, daily_budget="3000"))
sj = json.loads(cdj["spec_json"])
check(sj["currency"] == "JPY" and sj["daily_budget_minor"] == 3000, "a JPY account: 3,000 yen is 3000 (no minor unit)")
_, lidj = approve(owner, cdj, cid=pid)
F.calls.clear()
drive(lidj)
mj = mutate_ops(F.find("/googleAds:mutate")[0])
check(mj[0]["campaignBudgetOperation"]["create"]["amountMicros"] == "3000000000" and
      "login-customer-id" not in F.find("/googleAds:mutate")[0]["headers"],
      "sent as 3,000,000,000 micros, straight to the customer (no manager)")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='revoked' WHERE id=?", (JPY_CONN,))
    conn.execute("UPDATE ad_connections SET status='active' WHERE id=?", (CONN_ID,))
    conn.commit()

# ================================================================ 6. Performance Max (M3.3)
print("performance max")
from PIL import Image  # noqa: E402
with db.connect() as conn:
    bid = conn.execute("INSERT INTO brands (workspace_id, brand_name, created_at) VALUES (?,?,?)",
                       (WS, "Cool Air Heating and Cooling Co", db.now())).lastrowid
    prod = conn.execute("INSERT INTO products (workspace_id, brand_id, name, description, category, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)", (WS, bid, "AC repair", "AC repair in Austin", "app_service", db.now(),
                                                   db.now())).lastrowid
    photos = {}
    for fname, size in (("logo.png", (300, 100)), ("square-logo.png", (400, 400)), ("tiny.png", (50, 50))):
        path = os.path.join(TMP, fname)
        Image.new("RGB", size, (20, 90, 160)).save(path)
        photos[fname] = conn.execute("INSERT INTO photos (workspace_id, product_id, path, filename, content_type, created_at) "
                                     "VALUES (?,?,?,?,?,?)", (WS, prod, path, fname, "image/png", db.now())).lastrowid
    conn.execute("UPDATE products SET import_json=? WHERE id=?",
                 (json.dumps({"sells": "service", "site": {"logo_photo_id": photos["logo.png"]}}), prod))
    conn.commit()
NOKW = json.loads(json.dumps(PLAN))
NOKW["final"].pop("keywords")
PM_PLAN = make_plan(WS, u["id"], name="Cool Air PMax", plan=NOKW, product_id=prod)
imgs = []
with db.connect() as conn:
    for i, (size, angle) in enumerate((((1080, 1080), "Square ad"), ((864, 1080), "Portrait ad"), ((1080, 1920), "Story ad"))):
        path = os.path.join(TMP, f"pm{i}.png")
        Image.new("RGB", size, (200, 120 + i * 30, 40)).save(path)
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                           "updated_at, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (WS, u["id"], "ad_image", "b", "{}", "done", 10, db.now(), db.now(), prod)).lastrowid
        imgs.append(conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, "image", "", path)).lastrowid)
        conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                     "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (PM_PLAN, WS, angle, "meta", "ad_image", "b", 10, "queued", jid, db.now()))
    conn.commit()
PURL = f"/app/campaigns/{PM_PLAN}/launch/google"
pcard, ppage = card_page(owner, cid=PM_PLAN)
ps = json.loads(pcard["spec_json"])
pg = ps["google"]
check(pg["type"] == "pmax" and [c["asset_id"] for c in ps["creatives"]] == imgs and pg["bidding"] == "conversions",
      "a plan with images and no keyword list drafts Performance Max with its finished image ads")
check(pg["logos"] == [photos["logo.png"]] and pg["business_name"] == "Cool Air Heating and Cooling Co"[:25],
      "the site logo is picked as the logo; the brand name (cut to 25) as the business name")
pform = {"card": pcard["id"], "gtype": "pmax", "head_n": "4", "h_0": "Same-Day AC Repair", "h_1": "Austin HVAC Pros",
         "h_2": "Free Quotes", "h_3": "", "long_n": "1", "lh_0": "Licensed Austin techs fix most AC problems on the first visit",
         "desc_n": "2", "d_0": "Book online in 2 minutes.", "d_1": "Serving Austin and Round Rock with repairs and tune-ups.",
         "business": "Cool Air", "logos_form": "1", "logo": [str(photos["logo.png"]), str(photos["square-logo.png"])],
         f"use_{imgs[0]}": "1", f"use_{imgs[1]}": "1", "landing": "https://coolair.example/quote", "countries": "US, CA",
         "daily_budget": "40", "language": "en"}
pcard, ppage = card_page(owner, cid=PM_PLAN, **pform)
pc = json.loads(pcard["after_json"])
check(not pc["blocking"], "the Performance Max card has nothing blocking")
check(pc["lines"][0] == "1 campaign: Cool Air PMax. Performance Max, $40 a day, Maximize conversions, PAUSED.",
      "card: 1 campaign, Performance Max, budget, Maximize conversions, PAUSED")
check(pc["lines"][1] == "Brand: Cool Air, 2 logos (logo 1 padded to a square on white).",
      "card: the brand guidelines (business name and logos, the 3:1 logo padded)")
check("1 asset group: 3 headlines, 1 long headline, 2 descriptions and 5 images" in pc["lines"][3],
      "card: one asset group with its counts (2 crops of the square ad, 3 of the 4:5 ad)")
check("Google won't write extra text from your site" in pc["lines"][4] and "can make one from these images" in pc["lines"][4],
      "card: what Google may still do on its own, said plainly")
check(pc["disclosure"].startswith("AI disclosure: images 1 and 2 are AI-made media."), "card: the AI-made images are named")
check("What Campaignify will create on Google" in ppage and "Business name" in ppage and "(padded)" in ppage
      and "(too small)" in ppage, "the page shows the brand fields, logos padded or too small")
F.calls.clear()
r, plid = approve(owner, pcard, cid=PM_PLAN)
psteps = q("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", plid)
check([s["step_key"] for s in psteps] == ["upload:1", "upload:2", "upload:3", "upload:4", "upload:5", "upload:6", "upload:7",
                                          "campaign", "check"],
      "the ledger: 7 image uploads (5 crops and 2 logos), the one mutate, the check")
st = drive(plid)
check(st == "paused", "the Performance Max saga runs to paused")
ups = F.find("/assets:mutate")
sizes = sorted(o["size"] for o in F.objects("asset") if o.get("type") == "IMAGE")
check(len(ups) == 7 and sizes == sorted([(1200, 628), (1200, 1200), (1200, 628), (1200, 1200), (960, 1200), (300, 300),
                                         (400, 400)]), "uploads: landscape 1200x628, square 1200x1200, portrait 960x1200 and the logos")
check(all(c["data"]["operations"][0]["create"]["name"].endswith(f":upload:{i + 1}]") for i, c in enumerate(ups)),
      "each image's name carries the cfy tag")
muts = F.find("/googleAds:mutate")
check(len(muts) == 1, "then exactly one mutate")
pops = mutate_ops(muts[0])
pc_ = pops[1]["campaignOperation"]["create"]
check(pc_["advertisingChannelType"] == "PERFORMANCE_MAX" and pc_["status"] == "PAUSED" and pc_["brandGuidelinesEnabled"] is True
      and "maximizeConversions" in pc_ and {a["assetAutomationType"] for a in pc_["assetAutomationSettings"]}
      == {"TEXT_ASSET_AUTOMATION", "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"}
      and all(a["assetAutomationStatus"] == "OPTED_OUT" for a in pc_["assetAutomationSettings"]),
      "the campaign: Performance Max, PAUSED, brand guidelines on, Maximize conversions, Google's own text off")
links = [o["campaignAssetOperation"]["create"]["fieldType"] for o in pops if "campaignAssetOperation" in o]
check(links.count("BUSINESS_NAME") == 1 and links.count("LOGO") == 2, "1 business name and 2 logos linked at campaign level, same mutate")
agl = [o["assetGroupAssetOperation"]["create"]["fieldType"] for o in pops if "assetGroupAssetOperation" in o]
check(sorted(agl) == sorted(["HEADLINE"] * 3 + ["LONG_HEADLINE", "DESCRIPTION", "DESCRIPTION", "MARKETING_IMAGE",
                                                "SQUARE_MARKETING_IMAGE", "MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE",
                                                "PORTRAIT_MARKETING_IMAGE"]),
      "the asset group links 3 headlines, 1 long headline, 2 descriptions and the 5 images")
agi = [i for i, o in enumerate(pops) if "assetGroupOperation" in o][0]
check(all(i > agi for i, o in enumerate(pops) if "assetGroupAssetOperation" in o)
      and pops[agi]["assetGroupOperation"]["create"]["finalUrls"] == ["https://coolair.example/quote"],
      "the asset group comes before its links and has the landing page")
geos = [o["campaignCriterionOperation"]["create"]["location"]["geoTargetConstant"] for o in pops
        if "campaignCriterionOperation" in o and "location" in o["campaignCriterionOperation"]["create"]]
check(geos == ["geoTargetConstants/2840", "geoTargetConstants/2124"], "US and Canada")
check(len(F.objects("campaign")) and any(o["kind"] == "asset_group" for o in F.objects()),
      "Google holds the PAUSED campaign and its asset group")
pobjs = [o["kind"] for o in q("SELECT * FROM ad_objects WHERE launch_id=?", plid)]
check(pobjs == ["campaign", "assetgroup"], "ad_objects: the campaign and the asset group")
chk = json.loads(q("SELECT result_json FROM launch_steps WHERE launch_id=? AND step_key='check'", plid)[0]["result_json"])
check(chk["campaign_status"] == "PAUSED" and chk["approval"] == "PENDING", "the GAQL check reads the asset group's status")
spage = html(owner.get(f"/app/launches/{plid}"))
check("Upload the landscape image from ad" in spage and "Upload the logo" in spage
      and "its asset group and assets in one go" in spage, "the status page lists each upload and the one mutate")

# rules
def pm_review(**g):
    d = json.loads(pcard["spec_json"])
    d["google"] = dict(d["google"], **g.pop("g", {}))
    d.update(g)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
        return {i.rule_id for i in launches.review(conn, ws(WS), d, row)[1] if i.level == "block"}


for g, rule, what in ((dict(headlines=["A", "B"]), "google.pmax_headlines", "2 headlines"),
                      (dict(long_headlines=[]), "google.pmax_long_headlines", "no long headline"),
                      (dict(long_headlines=["x" * 91]), "google.pmax_long_headlines_long", "a 91-character long headline"),
                      (dict(descriptions=["Only one."]), "google.pmax_descriptions", "1 description"),
                      (dict(descriptions=["x" * 70, "y" * 80]), "google.pmax_short_description", "no description of 60 or fewer"),
                      (dict(business_name=""), "google.business_name", "no business name"),
                      (dict(business_name="A business name over 25 chars"), "google.business_name", "a 29-character business name"),
                      (dict(logos=[]), "google.logos", "no logo"),
                      (dict(logos=[photos["tiny.png"]]), "google.logo_small", "a 50 pixel logo"),
                      (dict(logos=[999999]), "google.logo_unknown", "a logo that isn't this workspace's photo")):
    check(rule in pm_review(g=g), f"Performance Max blocks {what} ({rule})")
check("google.pmax_images" in pm_review(creatives=[]), "Performance Max blocks no image")

# the errors only a Performance Max mutate can meet
for name, words in (("error_pmax_business_name.json", "needs the business name"), ("error_pmax_logo.json", "at least 1 logo"),
                    ("error_not_enough_headline.json", "at least 3 headlines"),
                    ("error_not_enough_long_headline.json", "at least 1 long headline"),
                    ("error_not_enough_description.json", "at least 2 descriptions"),
                    ("error_not_enough_marketing_image.json", "at least 1 landscape image"),
                    ("error_not_enough_square_marketing_image.json", "at least 1 square image")):
    pid = make_plan(WS, u["id"], name=f"PMax {name[6:-5]}", plan=NOKW, product_id=prod)
    with db.connect() as conn:
        conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (pid, PM_PLAN))
        conn.commit()
    cd, _ = card_page(owner, cid=pid, **dict(pform, card=""))
    _, lx = approve(owner, cd, cid=pid)
    before = F.mutates
    F.fail("googleAds:mutate", f"google/{name}")
    st = drive(lx)
    L_ = q("SELECT * FROM launches WHERE id=?", lx)[0]
    kept = [s["status"] for s in q("SELECT * FROM launch_steps WHERE launch_id=? AND kind='upload'", lx)]
    check(st == "rolled_back" and words in L_["error"] and F.mutates == before and set(kept) == {"kept"},
          f"{name[6:-5]}: nothing made, the uploads kept (Google can't delete images), in plain words")
    with db.connect() as conn:
        conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (PM_PLAN, pid))
        conn.commit()
# the fake's own rules agree: brand assets missing from the mutate fail there too
F.calls.clear()
before = len(F.objects("campaign"))
try:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
        sp, _ = adspec.from_dict(dict(json.loads(pcard["spec_json"]), name="No logo mutate"))
    ops_ = [o for o in google.pmax_operations(sp, "5556667778", []) if "campaignAssetOperation" not in o]
    google.mutate({"account": "5556667778", "login": "9998887776"}, google.access_token(REFRESH),
                  google._fill(ops_, {}, 1))
    e = None
except adhttp.AdHttpError as err:
    e = err
check(e is not None and e.code == "campaignError.REQUIRED_BUSINESS_NAME_ASSET_NOT_LINKED" and len(F.objects("campaign")) == before,
      "a Performance Max mutate without its brand assets is refused whole (atomic)")

# a failure at an upload: earlier uploads kept, Try again reuses them, nothing doubled
pid = make_plan(WS, u["id"], name="PMax upload fails", plan=NOKW, product_id=prod)
with db.connect() as conn:
    conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (pid, PM_PLAN))
    conn.commit()
cd, _ = card_page(owner, cid=pid, **dict(pform, card=""))
_, lx = approve(owner, cd, cid=pid)
nassets = len([o for o in F.objects("asset") if o.get("type") == "IMAGE"])
calls_up = {"n": 0}
orig_assets = F._assets


def third_fails(cid, body):
    calls_up["n"] += 1
    if calls_up["n"] == 3:
        from fakegoogleads import Refuse
        raise Refuse("google/error_image_aspect.json")
    return orig_assets(cid, body)


F._assets = third_fails
st = drive(lx)
F._assets = orig_assets
stp = {s["step_key"]: s["status"] for s in q("SELECT * FROM launch_steps WHERE launch_id=?", lx)}
check(st == "rolled_back" and stp["upload:1"] == "kept" and stp["upload:2"] == "kept" and stp["upload:3"] == "failed"
      and "image's shape" in q("SELECT error FROM launches WHERE id=?", lx)[0]["error"],
      "an image Google refuses stops the launch; the 2 uploaded before it are kept")
owner.post(f"/app/launches/{lx}/retry", data={"csrf": csrf(owner)})
st = drive(lx)
check(st == "paused" and len([o for o in F.objects("asset") if o.get("type") == "IMAGE"]) - nassets == 7,
      "Try again reuses the 2 kept images and uploads the other 5: 7 images, no duplicates")
with db.connect() as conn:
    conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (PM_PLAN, pid))
    conn.commit()
# a lost upload answer is found by name, not uploaded twice
pid = make_plan(WS, u["id"], name="PMax upload lost", plan=NOKW, product_id=prod)
with db.connect() as conn:
    conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (pid, PM_PLAN))
    conn.commit()
cd, _ = card_page(owner, cid=pid, **dict(pform, card=""))
_, lx = approve(owner, cd, cid=pid)
nassets = len([o for o in F.objects("asset") if o.get("type") == "IMAGE"])
F.fail("assets:mutate", "lost")
st = drive(lx)
check(st == "paused" and len([o for o in F.objects("asset") if o.get("type") == "IMAGE"]) - nassets == 7,
      "an upload whose answer was lost is found by its tagged name: still 7 images")
with db.connect() as conn:
    conn.execute("UPDATE campaign_ads SET campaign_id=? WHERE campaign_id=?", (PM_PLAN, pid))
    conn.commit()
# switching the type on the page
scard, spg = card_page(owner, cid=PM_PLAN, **dict(pform, gtype="search"))
check(json.loads(scard["spec_json"])["google"]["type"] == "search" and json.loads(scard["spec_json"])["creatives"] == []
      and "Keywords" in spg, "switching to Search drops the images and shows the keyword box")

# the flag off: queued Google launches wait in the worker; the page and the status are 404
_, lidw = new_launch("Waits for the flag")
os.environ.pop("GOOGLE_ADS")
F.calls.clear()
launches.tick(limit=50)
check(not F.find("/googleAds:mutate") and q("SELECT status FROM launches WHERE id=?", lidw)[0]["status"] == "queued",
      "GOOGLE_ADS off: the worker leaves a queued Google launch waiting")
check(owner.get(f"/app/launches/{lidw}").status_code == 404 and owner.get(URL).status_code == 404,
      "GOOGLE_ADS off: the Google launch and Launch pages are 404")
os.environ["GOOGLE_ADS"] = "1"
launches.tick(limit=50)
check(q("SELECT status FROM launches WHERE id=?", lidw)[0]["status"] == "paused", "back on: it runs")

# the sandbox tool plans the same calls (its --dry-run needs no network)
import subprocess  # noqa: E402
out = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "tools", "google_sandbox_launch.py"),
                      "--customer", "123-456-7890", "--dry-run"], capture_output=True, text=True,
                     env=dict(os.environ, RS_DATA=os.path.join(TMP, "sandbox")))
check(out.returncode == 0 and "DRY RUN: 2 steps, 0 uploads, 1 mutate" in out.stdout and '"PAUSED"' in out.stdout,
      "tools/google_sandbox_launch.py --dry-run plans a PAUSED Search launch in one mutate")

# ================================================================ 9. no secrets anywhere
print("secrets")
logs = "\n".join(CAP.lines)
access = list(F.tokens)
check(REFRESH not in logs and not any(a in logs for a in access) and CLIENT_SECRET not in logs,
      "no refresh token, access token or client secret in any log line")
rows = json.dumps([dict(r) for r in q("SELECT * FROM launches")] + [dict(r) for r in q("SELECT * FROM launch_steps")])
check(REFRESH not in rows and not any(a in rows for a in access), "no token in any launch or step row")
raw = open(db.DB_PATH, "rb").read()
check(REFRESH.encode() not in raw, "no refresh token in the database file")
print(f"\nall {passed} checks passed")
