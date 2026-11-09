"""The launch saga, the approval card and the Meta launch steps (roadmap M2.3) on a throwaway database. Meta is the
fake Graph in tests/fakegraph.py (answers shaped like tests/fixtures/meta/launch_*.json and error_*.json, hand-written
from Meta's v26.0 reference, to re-record on the sandbox account): no request leaves the machine.

    python tests/test_launches.py      (tests/test_app.py runs it too)

  * AD_LAUNCH off / beta (allowlist) / on: the Launch page, the kit button, the worker
  * the approval card: what will be made (campaign, ad set, ads, budget, schedule, AI disclosure), the
    policy linter (adpolicy.lint as it stands) and the launch rules block it: paid plan (D2), the daily
    ceilings per plan (D3), library assets only (I-7), the connection, the pixel; members can't approve
  * a 2-ad launch makes exactly the expected objects, all PAUSED, with the cfy tag, promoted_object and
    object_story_spec; the card's text matches what was made
  * a retried launch makes nothing new: approving the same card again, running a finished launch, and a
    create whose answer was lost (a timeout after Meta made it) at every step
  * a forced failure at every step compensates: ads and creatives deleted, campaign and ad set kept PAUSED,
    uploads kept; "Try again" then reuses what was kept and makes the rest, with no duplicates
  * every recorded error code: plain words, retry or wait or fail, 190 marks the connection for Reconnect
  * adhttp: the rate budget makes a big launch wait instead of fail, the 30 s edit lock on changes,
    creates are never retried blindly; a video still processing waits
  * L-14: a reconnect by another Meta user revokes the old grant unless another connection uses it
  * no token in any log line, page or launch row; another workspace's launch and card are 404
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
TMP = tempfile.mkdtemp(prefix="rs_launch_")
atexit.register(shutil.rmtree, TMP, True)
KEY = base64.b64encode(b"L" * 32).decode()
APP_SECRET = "fake-app-secret-launch-5150"
TOKEN = "EAAlaunchTOKENsecret123456789"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=KEY,
                  META_APP_ID="1000000000000001", META_APP_SECRET=APP_SECRET, META_LOGIN_CONFIG_ID="424242424242")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "AD_RATE_TIER", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
import launch_routes  # noqa: E402
import launches  # noqa: E402
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
            self.lines.append(record.getMessage() + (logging.Formatter().formatException(record.exc_info)
                                                     if record.exc_info else ""))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


CAP = Capture()
for name in ("", "campaignify", "rs", "werkzeug", "urllib3"):
    lg = logging.getLogger(name)
    lg.addHandler(CAP)
    lg.setLevel(logging.DEBUG)

# one fake clock for adhttp (budget, edit lock, sleeps) and the saga (next_at, leases)
T = [time.time()]
adhttp.CLOCK = launches.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.41.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
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


# ---------------------------------------------------------------- a workspace with a plan, 2 finished Meta ads and a connection
from PIL import Image  # noqa: E402

owner, u = signup("launch-owner@example.com")
WS = u["workspace_id"]
IMG = os.path.join(TMP, "ad1.png")
Image.new("RGB", (864, 1080), (200, 120, 40)).save(IMG)
VID = os.path.join(TMP, "ad2.mp4")
open(VID, "wb").write(b"\x00\x00\x00\x18ftypmp42fake video bytes for the fake graph")
Image.new("RGB", (720, 1280), (40, 120, 200)).save(VID + ".jpg")

OKC = {"ok": True, "warnings": [], "blocks": []}
PLAN = {"angles": [{"name": "Evening ritual"}, {"name": "Lasts the month"}],
        "copy": [{"platform": "meta", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Evening ritual", "ok": True, "compliance": OKC, "fields": [{"name": "Primary text", "text": "Light it once. Smell it all week."},
                                                               {"name": "Headline", "text": "Ember Candle, 50 hours"}]},
            {"angle": "Lasts the month", "ok": True, "compliance": OKC, "fields": [{"name": "Primary text", "text": "One candle, fifty hours."},
                                                                {"name": "Headline", "text": "Burns 50 hours"}]}]}],
        "final": {"summary": "Image and video first.", "budget_split": [{"platform": "meta", "share_percent": 100}],
                  "checklist": [{"title": "Set up the Meta pixel", "platform": "meta"}]}}
BRIEF = {"offer": "Ember Candle", "goal": "sales", "audience": "Renters 25-40", "platforms": ["meta"],
         "link": "https://embercandle.example/shop", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14, "budget": "$40 a day for 14 days ($560 total)"}


def make_plan(ws_id, user_id, name="Ember Candle"):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, name, json.dumps(BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        oids = []
        for tool, kind, path, angle, params in (("ad_image", "image", IMG, "Evening ritual", {}),
                                                ("ugc_video", "video", VID, "Lasts the month", {"ad_type": "testimonial"})):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (ws_id, user_id, tool, "b", json.dumps(params), "done", 43, db.now(), db.now())).lastrowid
            oids.append(conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                                     (jid, kind, "", path)).lastrowid)
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                         "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, ws_id, angle, "meta", tool, "b", 43, "queued", jid, db.now()))
        conn.commit()
    return cid, oids


def connect(ws_id, user_id, account="111000111", meta_user="10200000000000001", page="500000001"):
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
            "page_id, page_name, instagram_actor_id, instagram_username, external_user_id, token_enc, scopes, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ws_id, "meta", account, "Ember Shop", "USD", "America/Chicago", page, "Ember Candle Co",
             "17841400000000001", "embercandle", meta_user, adplatforms.seal_token(ws_id, TOKEN),
             " ".join(meta.SCOPES), "active", user_id, db.now(), db.now())).lastrowid
        conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                     "VALUES (?,?,?,?,?,?)", (ws_id, "meta", cid, "700000002", "Ember site pixel", db.now()))
        conn.commit()
    return cid


PLAN_ID, (OID_IMG, OID_VID) = make_plan(WS, u["id"])
CONN_ID = connect(WS, u["id"])
G = FakeGraph(account="act_111000111")
adhttp.TRANSPORT = G


def card_page(c, cid=PLAN_ID, **form):
    """POST the Launch form (Update the card) and return (card row, page html)."""
    if form:
        data = {"csrf": csrf(c), "form": "1"}
        data.update(form)
        r = c.post(f"/app/campaigns/{cid}/launch/meta", data=data)
        assert r.status_code == 302, (r.status_code, html(r)[:500])
        card_id = int(re.search(r"card=(\d+)", r.headers["Location"]).group(1))
        page = html(c.get(r.headers["Location"]))
    else:
        page = html(c.get(f"/app/campaigns/{cid}/launch/meta"))
        card_id = int(re.search(r'name="card" value="(\d+)"', page).group(1))
    return q("SELECT * FROM approval_cards WHERE id=?", card_id)[0], page


BOTH = {f"use_{OID_IMG}": "1", f"use_{OID_VID}": "1"}


def drive(lid, max_runs=60, step=61.0):
    """Run a launch until it finishes (moving the fake clock past each wait). Returns its final status."""
    for _ in range(max_runs):
        launches.run(lid)
        st = q("SELECT status FROM launches WHERE id=?", lid)[0]["status"]
        if st in launches.FINISHED:
            return st
        T[0] += step
    return st


def approve(c, card, cid=PLAN_ID):
    r = c.post(f"/app/campaigns/{cid}/launch/meta/approve", data={"csrf": csrf(c), "card": card["id"], "hash": card["spec_hash"]})
    m = re.search(r"/app/launches/(\d+)", r.headers.get("Location", ""))
    return r, (int(m.group(1)) if m else None)


def strip_tag(name):
    return re.sub(r" \[cfy:\d+:[a-z]+(:\d+)?\]$", "", name)


# ================================================================ 1. the flag
print("AD_LAUNCH")
check(owner.get(f"/app/campaigns/{PLAN_ID}/launch/meta").status_code == 404, "AD_LAUNCH unset: the Launch page is a 404")
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
check('id="kit-meta"' in plan_page and "Create paused campaign" not in plan_page,
      "AD_LAUNCH unset: the launch kit shows, without the Create paused campaign button")
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES="999999")
check(owner.get(f"/app/campaigns/{PLAN_ID}/launch/meta").status_code == 404, "beta, not on the allowlist: 404")
os.environ["AD_LAUNCH_WORKSPACES"] = str(WS)
check(owner.get(f"/app/campaigns/{PLAN_ID}/launch/meta").status_code == 200, "beta, on the allowlist: the Launch page opens")
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
meta_block = plan_page.split('id="kit-meta"', 1)[1].split("</article>", 1)[0]
check("Create paused campaign" in meta_block and f"/app/campaigns/{PLAN_ID}/launch/meta" in meta_block
      and "PAUSED until you turn it on" in meta_block, "beta: the Meta block of the launch kit has the button and one line why")

# ================================================================ 2. the approval card
print("approval card")
card, page = card_page(owner)
c0 = json.loads(card["after_json"])
spec0 = json.loads(card["spec_json"])
check(card["kind"] == "launch" and card["decision"] is None and card["created_by"] == u["id"],
      "opening the Launch page stores an undecided launch card")
check(spec0["daily_budget_minor"] == 4000 and spec0["currency"] == "USD" and spec0["objective"] == "sales"
      and spec0["conversion"]["pixel_id"] == "700000002" and spec0["conversion"]["event"] == "Purchase",
      "the draft comes from the plan: $40 a day (100% to Meta), Sales, the connection's pixel, Purchase")
check(len(spec0["creatives"]) == 2 and spec0["creatives"][0]["primary_text"] == "Light it once. Smell it all week."
      and spec0["creatives"][1]["headline"] == "Burns 50 hours" and spec0["creatives"][1]["aspect"] == "9:16",
      "both finished Meta ads, with the plan's copy for each angle and the aspect from the file")
check(spec0["creatives"][1]["ai_presenter"] and spec0["creatives"][1]["synthetic_voice"] and spec0["ai_disclosure"],
      "the AI presenter video is flagged from the library (provenance), so the AI disclosure is on")
lines = " ".join(c0["lines"])
check("1 campaign: Ember Candle. Sales, $40 a day, PAUSED." in c0["lines"], "card: 1 campaign, objective, budget, PAUSED")
check(c0["lines"][1].startswith("1 ad set: US, ages 18 to 65, all genders, automatic placements, optimizes for Purchase on "
                                "Ember site pixel"), "card: 1 ad set with location, ages, placements and the pixel event")
check("2 ads, PAUSED: Ember Candle · ad 1 Evening ritual; Ember Candle · ad 2 Lasts the month." in c0["lines"],
      "card: the 2 ads by name")
check(c0["schedule"].startswith("Starts ") and ", ends " in c0["schedule"], "card: the schedule (start tomorrow, 14 days)")
check(c0["disclosure"].startswith("AI disclosure: ad 2 has an AI presenter or AI voice; ad 1 is AI-made media."),
      "card: the AI disclosure names each AI-made ad and what kind (presenter, or AI-made media)")
check("tick Meta's AI label on those ads in Ads Manager." in c0["disclosure"] and c0["footer"] == "Nothing runs until you turn it on.",
      "card: tells the member to tick Meta's AI label before turning it on, and that nothing runs until then")
check("What Campaignify will create on Meta" in page and "Ember Shop (act_111000111)" in page and "Create paused campaign" in page,
      "the page shows the card with the ad account")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the Launch page")
check("<button class=\"btn\">Create paused campaign</button>" in page, "no blocking finding: the owner can approve")
card_again, _ = card_page(owner)
check(card_again["id"] == card["id"], "opening the page again reuses the same undecided card (same spec)")

# the product type is prefilled from the product (adpolicy.derive_vertical), so a supplement isn't a vertical mismatch
with db.connect() as conn:
    sup = conn.execute("INSERT INTO products (workspace_id, brand_id, name, description, category, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (WS, 0, "Calm Gummies", "Magnesium gummies", "supplement", db.now(),
                                                  db.now())).lastrowid
    conn.commit()
SUP_PLAN, (SUP_IMG, SUP_VID) = make_plan(WS, u["id"], name="Calm Gummies")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET product_id=? WHERE id IN (SELECT job_id FROM outputs WHERE id IN (?,?))", (sup, SUP_IMG, SUP_VID))
    conn.commit()
    crow = conn.execute("SELECT * FROM campaigns WHERE id=?", (SUP_PLAN,)).fetchone()
    srow = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
    sdraft = launches.draft(conn, ws(WS), crow, srow)
    _, sissues, _, _ = launches.review(conn, ws(WS), sdraft, srow)
check(sdraft["vertical"] == "supplements" and "policy.vertical_mismatch" not in {i.rule_id for i in sissues},
      "a supplement product's draft starts as Supplements (derive_vertical), so no vertical mismatch on the first try")
spage = html(owner.get(f"/app/campaigns/{SUP_PLAN}/launch/meta"))
check('<option value="supplements" selected>' in spage, "the Launch form shows the prefilled category")

# a member can draft but not approve
member, mu = signup("launch-member@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET workspace_id=?, role='member' WHERE id=?", (WS, mu["id"]))
    conn.commit()
mpage = html(member.get(f"/app/campaigns/{PLAN_ID}/launch/meta"))
check("Only the workspace owner can approve" in mpage and 'disabled aria-describedby="ln-own"' in mpage,
      "a member sees the card with the approve button off")
r, lid_m = approve(member, card)
check(r.status_code == 403 and lid_m is None and not q("SELECT * FROM launches"), "a member's approve is refused (403)")

# the launch rules
spec_ok = dict(spec0)


def review_with(ws_id=WS, plan=None, **changes):
    if plan:
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, ws_id))
            conn.commit()
    d = json.loads(json.dumps(spec_ok))
    d.update(changes)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
        spec, issues, card, steps = launches.review(conn, ws(ws_id), d, row)
    return {i.rule_id for i in issues if i.level == "block"}, issues, card, steps


for plan, ok_minor, over_minor in (("starter", 20000, 20100), ("growth", 100000, 100100), ("scale", 500000, 500100)):
    b_ok, _, _, _ = review_with(plan=plan, daily_budget_minor=ok_minor)
    b_over, iss, _, _ = review_with(plan=plan, daily_budget_minor=over_minor)
    msg = next(i for i in iss if i.rule_id == "launch.budget_ceiling")
    check("launch.budget_ceiling" not in b_ok and "launch.budget_ceiling" in b_over and "hello@campaignify.ai" in msg.fix,
          f"D3: {plan} allows {ok_minor // 100:,} a day and blocks more, with the ask-us path")
b_free, _, _, _ = review_with(plan="free")
check("launch.paid_plan" in b_free, "D2: a free workspace can't launch (paid plan needed)")
review_with(plan="growth")
d_noasset = json.loads(json.dumps(spec_ok))
d_noasset["creatives"][0]["asset_id"] = None
b_na, _, _, _ = review_with(creatives=d_noasset["creatives"])
check("launch.no_asset" in b_na, "I-7: an ad without a library asset is refused (the AI disclosure must be known)")
other, ou = signup("launch-other@example.com")
_, (OTHER_IMG, _) = make_plan(ou["workspace_id"], ou["id"], name="Not yours")
d_foreign = json.loads(json.dumps(spec_ok))
d_foreign["creatives"][0]["asset_id"] = OTHER_IMG
b_fa, _, _, _ = review_with(creatives=d_foreign["creatives"])
check("launch.asset_unknown" in b_fa, "another workspace's ad is refused")
d_cure = json.loads(json.dumps(spec_ok))
d_cure["creatives"][0]["primary_text"] = "This candle cures insomnia and anxiety."
b_cure, _, _, _ = review_with(creatives=d_cure["creatives"], vertical="health")
check(any(r.startswith("policy.") for r in b_cure), "the policy linter (adpolicy.lint) blocks a cure claim")
b_px, _, _, _ = review_with(conversion={"pixel_id": "123", "event": "Purchase", "landing_url": ""})
check("launch.pixel_unknown" in b_px, "a pixel that isn't the connection's is refused")
cpage = html(owner.post(f"/app/campaigns/{PLAN_ID}/launch/meta", data={
    "csrf": csrf(owner), "form": "1", "card": card["id"], **BOTH, "daily_budget": "1500"}, follow_redirects=True))
check("Fix before you can approve" in cpage and "Growth plan creates campaigns up to $1,000 a day" in cpage
      and 'disabled aria-describedby="ln-fix"' in cpage, "the page shows a blocking finding and keeps the button off")
bad_card = q("SELECT * FROM approval_cards WHERE workspace_id=? ORDER BY id DESC LIMIT 1", WS)[0]
r, lid_bad = approve(owner, bad_card)
check(lid_bad is None and not q("SELECT * FROM launches"), "approving a blocked card is refused on the server too")
r = owner.post(f"/app/campaigns/{PLAN_ID}/launch/meta/approve", data={"csrf": csrf(owner), "card": card["id"], "hash": "x" * 64})
check(r.status_code == 302 and not q("SELECT * FROM launches"), "an approve with a stale spec hash is refused")
r = other.post(f"/app/campaigns/{PLAN_ID}/launch/meta/approve", data={"csrf": csrf(other), "card": card["id"],
                                                                      "hash": card["spec_hash"]})
check(r.status_code == 404, "another workspace can't approve this plan's card (404)")

# ================================================================ 3. a 2-ad launch
print("2-ad launch")
card, _ = card_page(owner, **BOTH)
c1 = json.loads(card["after_json"])
r, LID = approve(owner, card)
L = q("SELECT * FROM launches WHERE id=?", LID)[0]
steps = q("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", LID)
check(L["status"] == "queued" and L["approved_by"] == u["id"] and L["idempotency_key"] ==
      launches.idempotency_key(WS, PLAN_ID, card["spec_hash"]), "approve makes a queued launch with the idempotency key")
check([s["step_key"] for s in steps] == ["upload:1", "upload:2", "creative:1", "creative:2", "campaign", "adset", "ad:1", "ad:2"],
      "the ledger: uploads, creatives, campaign, ad set, ads (Meta's order)")
check(q("SELECT decision, launch_id FROM approval_cards WHERE id=?", card["id"])[0]["decision"] == "approved",
      "the card is marked approved, by whom and when")
check(G.calls == [], "approving calls nothing on Meta: the worker does")
status_page = html(owner.get(f"/app/launches/{LID}"))
check("Creating your paused campaign" in status_page and "data-busy=\"1\"" in status_page, "the status page polls while it runs")
os.environ["AD_LAUNCH"] = "off"
check(launches.tick() == 0 and q("SELECT status FROM launches WHERE id=?", LID)[0]["status"] == "queued",
      "AD_LAUNCH off: the worker starts nothing")
os.environ["AD_LAUNCH"] = "beta"
check(launches.tick() == 1, "the worker's tick runs the queued launch")
st = drive(LID)
check(st == "paused", "the launch finishes: status paused")
objs = {k: G.objects(k) for k in ("video", "creative", "campaign", "adset", "ad")}
check(len(objs["campaign"]) == 1 and len(objs["adset"]) == 1 and len(objs["creative"]) == 2 and len(objs["ad"]) == 2
      and len(objs["video"]) == 1 and len(G.images) == 2, "exactly 1 campaign, 1 ad set, 2 creatives, 2 ads, 1 video, 2 images")
allobj = [o for k in ("creative", "campaign", "adset", "ad") for o in objs[k]]
check(all(o["status"] == "PAUSED" for o in objs["campaign"] + objs["adset"] + objs["ad"]), "everything is PAUSED")
check(all(re.search(rf"\[cfy:{LID}:[a-z]+(:\d)?\]$", o["name"]) for o in allobj + objs["video"]),
      "every name ends with the cfy:<launch_id>:<step> tag")
camp = objs["campaign"][0]["data"]
check(camp["objective"] == "OUTCOME_SALES" and camp["daily_budget"] == "4000" and camp["special_ad_categories"] == "[]",
      "the campaign: OUTCOME_SALES, the daily budget in cents, special_ad_categories sent")
aset = objs["adset"][0]["data"]
po, tg = json.loads(aset["promoted_object"]), json.loads(aset["targeting"])
check(po == {"pixel_id": "700000002", "custom_event_type": "PURCHASE"} and aset["optimization_goal"] == "OFFSITE_CONVERSIONS"
      and aset["campaign_id"] == objs["campaign"][0]["id"], "the ad set: promoted_object (pixel, PURCHASE) under the campaign")
check(tg["geo_locations"] == {"countries": ["US"]} and tg["age_min"] == 18 and tg["targeting_automation"]["advantage_audience"] == 0
      and aset["start_time"].endswith(("-06:00", "-05:00")), "the ad set's targeting and a start time in the account's time zone")
cr = {o["name"]: json.loads(o["data"]["object_story_spec"]) for o in objs["creative"]}
img_cr = next(v for k, v in cr.items() if "ad 1" in k)
vid_cr = next(v for k, v in cr.items() if "ad 2" in k)
check(img_cr["page_id"] == "500000001" and img_cr["instagram_user_id"] == "17841400000000001"
      and img_cr["link_data"]["image_hash"] in G.images and img_cr["link_data"]["link"] == BRIEF["link"]
      and img_cr["link_data"]["call_to_action"]["type"] == "SHOP_NOW", "the image creative: object_story_spec link_data")
check(vid_cr["video_data"]["video_id"] == objs["video"][0]["id"] and vid_cr["video_data"]["image_hash"] in G.images,
      "the video creative: video_data with the uploaded video and its thumbnail")
check(all(json.loads(o["data"]["creative"])["creative_id"] in {c["id"] for c in objs["creative"]} for o in objs["ad"]),
      "each ad points at its creative")
# the card matches what was made
made = sorted((o["kind"], strip_tag(o["name"])) for o in allobj)
check(made == sorted((o["kind"], o["name"]) for o in c1["objects"]), "the approved card lists exactly the objects made")
ads_line = next(line for line in c1["lines"] if line.startswith("2 ads"))
check(all(strip_tag(o["name"]) in ads_line for o in objs["ad"]), "the card's ads line names each ad made")
check(str(int(camp["daily_budget"]) // 100) in c1["lines"][0], "the card's budget is the budget sent")
db_objs = q("SELECT kind, external_id, status FROM ad_objects WHERE launch_id=?", LID)
check(sorted((r["kind"], r["external_id"]) for r in db_objs) == sorted([(o["kind"], o["id"]) for o in allobj + objs["video"]]),
      "ad_objects has every object with its Meta id")
L = q("SELECT * FROM launches WHERE id=?", LID)[0]
check(L["external_campaign_id"] == objs["campaign"][0]["id"] and q("SELECT applied_at FROM approval_cards WHERE id=?",
                                                                    card["id"])[0]["applied_at"],
      "the launch records the campaign id; the card is marked applied")
status_page = html(owner.get(f"/app/launches/{LID}"))
check("Paused campaign created" in status_page and "Open it in Ads Manager" in status_page
      and f"selected_campaign_ids={L['external_campaign_id']}" in status_page and "tick Meta" in status_page,
      "the status page: created, an Ads Manager link, and the AI label reminder")
js = owner.get(f"/app/launches/{LID}.json").get_json()
check(js["status"] == "paused" and not js["busy"] and len(js["steps"]) == 8, "the JSON for polling")
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
check("Last launch: created, PAUSED" in plan_page, "the kit links to the last launch")
creates_all = [c for c in G.calls if c["method"] == "POST"]
check(all("files" in c for c in creates_all) and not [c for c in creates_all if c["data"].get("status") not in (None, "PAUSED")],
      "no call ever asks Meta for anything but PAUSED")
check(all(c["headers"].get("Authorization") == f"Bearer {TOKEN}" and "access_token" not in c["params"]
          and len(c["params"].get("appsecret_proof", "")) == 64 for c in G.calls),
      "every launch call carries the token in the header with appsecret_proof, never in the URL")

FIRST_LOOKUPS = [c for c in G.calls if c["method"] == "GET" and c["path"].endswith(
    ("/campaigns", "/adsets", "/ads", "/adcreatives", "/advideos"))]

# ================================================================ 4. a retried launch creates nothing new
print("idempotency")
n_calls, n_creates = len(G.calls), len(G.creates())
r, lid2 = approve(owner, card)
check(lid2 == LID and len(q("SELECT * FROM launches")) == 1, "approving the same card again gives the same launch")
check(launches.run(LID) is None and drive(LID) == "paused" and len(G.calls) == n_calls,
      "running a finished launch again calls nothing")
with db.connect() as conn:   # a double click that races: the INSERT is OR IGNORE on the key
    card_row = conn.execute("SELECT * FROM approval_cards WHERE id=?", (card["id"],)).fetchone()
    lid3, new = launches.approve(conn, ws(WS), u, card_row)
check(lid3 == LID and not new and len(G.creates()) == n_creates, "approve() on the same spec is the same launch, nothing new")


launch_routes.APPROVALS_PER_HOUR = 10 ** 6     # this file approves many launches; the cap is checked at the end


def fresh_launch(name, graph=None, **form):
    """A new launch of the same plan (a different spec, so a new idempotency key), approved, not run."""
    global G
    G = graph or FakeGraph(account="act_111000111")
    adhttp.TRANSPORT = G
    c_, _ = card_page(owner, **BOTH, name=name, **form)
    _, lid = approve(owner, c_)
    assert lid, name
    return lid, c_


EXPECTED = {"video": 1, "creative": 2, "campaign": 1, "adset": 1, "ad": 2}


def counts(g):
    return {k: len(g.objects(k)) for k in EXPECTED}


STEP_EDGES = [("upload:1", "POST", "adimages"), ("upload:2", "POST", "advideos"), ("creative:1", "POST", "adcreatives"),
              ("creative:2", "POST", "adcreatives"), ("campaign", "POST", "campaigns"), ("adset", "POST", "adsets"),
              ("ad:1", "POST", "ads"), ("ad:2", "POST", "ads")]


def nth_fail(g, step_key, edge, response):
    """Fail the call of this step (the 2nd adcreatives / ads call for creative:2 / ad:2)."""
    if step_key.endswith(":2") and edge in ("adcreatives", "ads"):
        orig = g.__call__
        seen = [0]

        def hook(method, url, params=None, data=None, headers=None, timeout=None, files=None):
            if method == "POST" and url.endswith("/" + edge):
                seen[0] += 1
                if seen[0] == 2:
                    g.fail("POST", edge, response)
            return orig(method, url, params=params, data=data, headers=headers, timeout=timeout, files=files)
        return hook
    g.fail("POST", edge, response)
    return g


for key, method, edge in STEP_EDGES:
    g = FakeGraph(account="act_111000111")
    lid, _ = fresh_launch(f"Lost {key}", g)
    adhttp.TRANSPORT = nth_fail(g, key, edge, "lost")
    st = drive(lid)
    lost_step = q("SELECT * FROM launch_steps WHERE launch_id=? AND step_key=?", lid, key)[0]
    kind = {"upload:1": "image", "upload:2": "video"}.get(key, key.split(":")[0])
    want = {"image": 2, "video": 1, "creative": 2, "campaign": 1, "adset": 1, "ad": 2}[kind]
    check(st == "paused" and counts(g) == EXPECTED and lost_step["attempts"] >= 2 and len(g.creates(kind)) == want
          + (1 if kind == "image" else 0) and json.loads(lost_step["result_json"]).get("found", kind in ("image", "video")),
          f"a lost answer at {key} (Meta made it, we timed out): the re-run finds it by its tag, nothing doubled")
check(len(G.creates("campaign")) == 1 and len([c for c in G.calls if c["method"] == "GET" and c["path"].endswith("/ads")]) >= 1,
      "the lookup is a filtered list on Meta's side (name CONTAIN the tag)")
lookups = [c for c in G.calls if c["method"] == "GET" and c["path"].endswith(("/campaigns", "/adsets", "/ads"))]
check(all(json.loads(c["params"]["filtering"])[0]["operator"] == "CONTAIN" for c in lookups), "lookups filter by name")
check(not FIRST_LOOKUPS, "a launch's first run makes no lookup (nothing of it can exist yet): no extra calls")

# ================================================================ 5. a forced failure at each step compensates
print("compensation")
FAIL = "meta/error_100_invalid.json"
for idx, (key, method, edge) in enumerate(STEP_EDGES):
    g = FakeGraph(account="act_111000111")
    lid, _ = fresh_launch(f"Fail {key}", g)
    adhttp.TRANSPORT = nth_fail(g, key, edge, FAIL)
    st = drive(lid)
    L = q("SELECT * FROM launches WHERE id=?", lid)[0]
    rows = {s["step_key"]: s for s in q("SELECT * FROM launch_steps WHERE launch_id=?", lid)}
    before = [k for k, _, _ in STEP_EDGES[:idx]]
    ok = st == "rolled_back" and rows[key]["status"] == "failed"
    ok = ok and not g.objects("creative") and not g.objects("ad")
    ok = ok and all(rows[k]["status"] == ("compensated" if k.startswith(("creative", "ad:")) else "kept") for k in before)
    ok = ok and all(rows[k]["status"] == "pending" for k, _, _ in STEP_EDGES[idx + 1:])
    ok = ok and len(g.objects("campaign")) == (1 if "campaign" in before else 0) and \
        all(o["status"] == "PAUSED" for o in g.objects("campaign") + g.objects("adset"))
    deleted = {o["kind"] for o in g.objects(include_deleted=True) if o["status"] == "DELETED"}
    ok = ok and deleted <= {"creative", "ad"}
    check(ok, f"a failure at {key}: rolled back (creatives and ads deleted; campaign, ad set, uploads kept PAUSED)")
    if key == "adset":
        check(L["error"].startswith("Meta said: The image you selected is too small"),
              "the error is in plain words (Meta's own user message) with the raw one kept on the step")
        check("fbtrace_id AFAKEtrace100" in rows[key]["error"] and "Meta code 100, subcode 1487390" in rows[key]["error"]
              and TOKEN not in rows[key]["error"],
              "the step keeps Meta's raw error and trace id, scrubbed")
        page = html(owner.get(f"/app/launches/{lid}"))
        check("The launch stopped" in page and "Try again" in page and "stay PAUSED" in page, "the status page explains it")
    # Try again: what was kept is reused, the rest made; nothing doubled
    r = owner.post(f"/app/launches/{lid}/retry", data={"csrf": csrf(owner)})
    st2 = drive(lid)
    check(st2 == "paused" and counts(g) == EXPECTED and len(g.creates("campaign")) == 1 and len(g.creates("adset")) <= 1 + 0,
          f"after the failure at {key}, Try again finishes with exactly the expected objects")

# a delete Meta refuses: the launch is "failed" and lists what's left
g = FakeGraph(account="act_111000111")
lid, _ = fresh_launch("Undeletable", g)
g.fail("POST", "ads", FAIL)
g.fail("DELETE", "object", "meta/error_368_policy.json", times=5)
st = drive(lid)
v_left = q("SELECT * FROM launch_steps WHERE launch_id=? AND step_key LIKE 'creative:%'", lid)
check(st == "failed" and all(s["status"] == "done" and "Couldn't remove it on Meta" in s["error"] for s in v_left),
      "a creative Meta won't delete: status failed, the step says it couldn't be removed")
page = html(owner.get(f"/app/launches/{lid}"))
check("Some items couldn't be removed" in page and "Ad creative" in page, "the page lists what is left, all PAUSED")
check(q("SELECT COUNT(*) AS n FROM api_budget WHERE account LIKE 'edit:%'")[0]["n"] >= 2,
      "every delete took the object's 30 s edit slot first")

# ================================================================ 6. every recorded error code
print("error codes")
cases = [("meta/error_190.json", "rolled_back", "Reconnect it in Settings"),
         ("meta/error_200.json", "rolled_back", "missing a Meta permission"),
         ("meta/error_1885183_dev_mode.json", "rolled_back", "switched to Live"),
         ("meta/error_368_policy.json", "rolled_back", "against its advertising policies"),
         ("meta/error_2635_version.json", "rolled_back", "API version"),
         ("meta/error_100_invalid.json", "rolled_back", "Meta said:"),
         ("meta/error_17.json", "paused", None), ("meta/error_edit_lock.json", "paused", None),
         ("meta/error_2.json", "paused", None), ("meta/error_1_unknown.json", "paused", None),
         ("meta/error_80004_rate.json", "paused", None)]
for fx, want, words in cases:
    g = FakeGraph(account="act_111000111")
    lid, _ = fresh_launch(f"Err {fx}", g)
    g.fail("POST", "campaigns", fx)
    st = drive(lid)
    L = q("SELECT * FROM launches WHERE id=?", lid)[0]
    good = st == want and (words is None or words in (L["error"] or "")) and counts(g)["campaign"] == (1 if want == "paused" else 0)
    check(good, f"{fx.split('/')[1]}: " + (f"stops with \"{words}\"" if words else "waits or retries, then finishes"))
    if fx.endswith("190.json"):
        cstat = q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"]
        check(cstat == "expired" and L["error"].startswith("Meta ended Campaignify's access"),
              "190 marks the connection for Reconnect")
        with db.connect() as conn:
            conn.execute("UPDATE ad_connections SET status='active', last_error=NULL WHERE id=?", (CONN_ID,))
            conn.commit()
g = FakeGraph(account="act_111000111")
lid, _ = fresh_launch("Always 500", g)
g.fail("POST", "campaigns", "meta/error_1_unknown.json", times=10)
st = drive(lid)
row = q("SELECT * FROM launch_steps WHERE launch_id=? AND step_key='campaign'", lid)[0]
check(st == "rolled_back" and row["attempts"] == launches.STEP_ATTEMPTS and counts(g)["campaign"] == 0,
      f"a step that keeps failing with a retryable error stops after {launches.STEP_ATTEMPTS} tries")
creates = [c for c in g.calls if c["method"] == "POST" and c["path"].endswith("/campaigns")]
check(len(creates) == launches.STEP_ATTEMPTS, "a create is sent once per try (adhttp doesn't resend it blindly)")
import requests  # noqa: E402
g = FakeGraph(account="act_111000111")
lid, _ = fresh_launch("Network down", g)
g.fail("POST", "ads", requests.ConnectionError(f"https://graph.facebook.com/v26.0/act_1/ads?access_token={TOKEN}"), times=10)
st = drive(lid)
L = q("SELECT * FROM launches WHERE id=?", lid)[0]
check(st == "rolled_back" and L["error"] == launches.NETWORK_WORDS and TOKEN not in json.dumps([dict(r) for r in q(
    "SELECT * FROM launch_steps WHERE launch_id=?", lid)]), "a network that stays down: plain words, no token in the ledger")

# ================================================================ 7. rate budget, video processing, edit lock
print("adhttp and waits")
g = FakeGraph(account="act_111000111", video_processing=3)
lid, _ = fresh_launch("Slow video", g)
seen = []
for _ in range(10):
    seen.append(launches.run(lid))
    if q("SELECT status FROM launches WHERE id=?", lid)[0]["status"] == "paused":
        break
    T[0] += 16
check(seen.count("waiting") >= 3 and seen[-1] == "paused" and counts(g) == EXPECTED,
      "a video still processing makes the creative step wait, then the launch finishes")
os.environ["AD_RATE_TIER"] = "limited"
with db.connect() as conn:
    conn.execute("DELETE FROM api_budget")
    conn.commit()
g = FakeGraph(account="act_111000111")
lid, _ = fresh_launch("Budget", g)
with db.connect() as conn:    # most of the Limited tier's points already spent this minute
    conn.execute("INSERT OR REPLACE INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,?,?,0)",
                 ("meta", "act_111000111", 50.0, T[0]))
    conn.commit()
r1 = launches.run(lid)
check(r1 == "waiting" and q("SELECT next_at FROM launches WHERE id=?", lid)[0]["next_at"],
      "a launch that would pass the rate budget waits (next_at) instead of failing")
check(drive(lid) == "paused" and counts(g) == EXPECTED, "then finishes once the points decay")
g.store["999000111"] = {"id": "999000111", "kind": "campaign", "name": "x", "status": "PAUSED", "effective_status": "PAUSED"}
adhttp.edit_lock("meta", "999000111")
t0 = T[0]
adhttp.call("meta", "POST", meta.GRAPH + "/999000111", data={"status": "PAUSED"}, edit_of="999000111")
check(T[0] - t0 >= 29, "adhttp.call(edit_of=...) waits for the object's 30 s edit slot")
g.fail("POST", "object", "meta/error_edit_lock.json")
try:
    adhttp.call("meta", "POST", meta.GRAPH + "/999000111", data={"status": "PAUSED"}, retries=1)
    check(False, "Meta's edit lock answer raises")
except adhttp.RateLimited as e:
    check(e.subcode == 4841018 and e.retry_after == 30, "Meta's own edit-lock answer (subcode 4841018) is a 30 s wait")

# ================================================================ 8. L-14: reconnect by another Meta user
print("L-14")
ft = FakeTransport()
adhttp.TRANSPORT = ft
ft.on("DELETE", "/me/permissions", "meta/permissions_delete.json")
ft.on("GET", "/adspixels", "meta/adspixels_one.json")
OLD_TOKEN, NEW_TOKEN = "EAAoldPERSONtoken0000001", "EAAnewPERSONtoken0000002"


def pending_for(account, meta_user, token):
    with db.connect() as conn:
        pid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, external_user_id, token_enc, scopes, "
            "status, pending_json, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (WS, "meta", "pending:" + meta_user[-4:] + account[-3:], meta_user, adplatforms.seal_token(WS, token),
             " ".join(meta.SCOPES), "pending", json.dumps({"accounts": [{"id": account, "name": "Shared acct", "currency": "USD",
                                                                          "timezone": "UTC", "status": "Active", "usable": True,
                                                                          "business_id": None}], "pages": []}),
             u["id"], db.now(), db.now())).lastrowid
        conn.commit()
    return pid


def existing(account, meta_user, token):
    with db.connect() as conn:
        eid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, external_user_id, "
            "token_enc, scopes, status, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (WS, "meta", account, "Shared acct", "USD", meta_user, adplatforms.seal_token(WS, token), "ads_read", "active",
             u["id"], db.now(), db.now())).lastrowid
        conn.commit()
    return eid


eid = existing("444000444", "10200000000000444", OLD_TOKEN)
pid = pending_for("444000444", "10200000000000555", NEW_TOKEN)
r = owner.post(f"/app/settings/ad-accounts/{pid}/pick", data={"csrf": csrf(owner), "account": "444000444", "page": ""})
rv = ft.find("DELETE", "/me/permissions")
row = q("SELECT * FROM ad_connections WHERE id=?", eid)[0]
check(len(rv) == 1 and rv[0]["headers"]["Authorization"] == f"Bearer {OLD_TOKEN}" and adplatforms.open_token(row) == NEW_TOKEN
      and row["external_user_id"] == "10200000000000555", "another Meta user reconnecting revokes the old person's grant (L-14)")
eid2 = existing("555000555", "10200000000000666", OLD_TOKEN)
existing("555000556", "10200000000000666", OLD_TOKEN)   # the same person's grant on another ad account
pid2 = pending_for("555000555", "10200000000000777", NEW_TOKEN)
ft.calls.clear()
owner.post(f"/app/settings/ad-accounts/{pid2}/pick", data={"csrf": csrf(owner), "account": "555000555", "page": ""})
check(not ft.find("DELETE", "/me/permissions") and adplatforms.open_token(q("SELECT * FROM ad_connections WHERE id=?", eid2)[0])
      == NEW_TOKEN, "the old grant stays when another connection still uses it")
pid3 = pending_for("555000555", "10200000000000777", NEW_TOKEN)
ft.calls.clear()
owner.post(f"/app/settings/ad-accounts/{pid3}/pick", data={"csrf": csrf(owner), "account": "555000555", "page": ""})
check(not ft.find("DELETE", "/me/permissions"), "the same person reconnecting revokes nothing")

# ================================================================ 9. scoping and secrets
print("scoping and secrets")
check(other.get(f"/app/launches/{LID}").status_code == 404 and other.get(f"/app/launches/{LID}.json").status_code == 404,
      "another workspace's launch is a 404")
check(other.post(f"/app/launches/{LID}/retry", data={"csrf": csrf(other)}).status_code == 404, "and its retry")
os.environ["AD_LAUNCH_WORKSPACES"] = f"{WS},{ou['workspace_id']}"
check(other.get(f"/app/campaigns/{PLAN_ID}/launch/meta").status_code == 404, "another workspace's plan is a 404")
check(other.get(f"/app/campaigns/{PLAN_ID}/launch/meta?card={card['id']}").status_code == 404, "and its card")
check(member.post(f"/app/launches/{LID}/retry", data={"csrf": csrf(member)}).status_code == 403, "a member can't retry")
logs = "\n".join(CAP.lines)
check(TOKEN not in logs and APP_SECRET not in logs and OLD_TOKEN not in logs and NEW_TOKEN not in logs,
      "no token or app secret in any log line")
dump = json.dumps([dict(r) for t in ("launches", "launch_steps", "ad_objects", "approval_cards")
                   for r in q(f"SELECT * FROM {t}")])
check(TOKEN not in dump and APP_SECRET not in dump, "no token or app secret in any launch, step, object or card row")
with open(db.DB_PATH, "rb") as f:
    raw = f.read()
check(TOKEN.encode() not in raw, "the token is never in the database file in the clear")

import subprocess  # noqa: E402
env = {k: v for k, v in os.environ.items() if k != "META_SANDBOX_TOKEN"}
env["RS_DATA"] = os.path.join(TMP, "sandbox")
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "tools", "meta_sandbox_launch.py"), "--account", "act_1",
                    "--page", "2", "--image", IMG, "--video", VID], capture_output=True, text=True, env=env, timeout=60)
check(p.returncode != 0 and "Set META_SANDBOX_TOKEN" in (p.stdout + p.stderr),
      "tools/meta_sandbox_launch.py (the sandbox acceptance run) refuses to start without a token")
launch_routes.APPROVALS_PER_HOUR = 1
c_cap, _ = card_page(owner, **BOTH, name="One too many")
r, lid_cap = approve(owner, c_cap)
check(lid_cap is None and r.status_code == 302, "approvals are rate limited per workspace")

print(f"all {passed} checks passed")
