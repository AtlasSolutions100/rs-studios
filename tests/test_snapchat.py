"""Snapchat connect and launch (roadmap M6.1) on a throwaway database. Snap is the fake Marketing API and OAuth server
in tests/fakesnap.py (answers shaped like tests/fixtures/snapchat/*.json, hand-written from developers.snap.com, to
re-record with tools/snapchat_sandbox_launch.py): no request leaves the machine.

    python tests/test_snapchat.py      (tests/test_app.py runs it too)

  * SNAPCHAT_ADS off (the default) or AD_LAUNCH off: no Snapchat button, every Snapchat route is a 404, no Create
    button in the launch kit; on: they show
  * OAuth: the scope, the fixed redirect URI, a single-use state tied to the person; the refresh token goes only into
    the token box; the picker lists ad accounts by organization and the organization's public profiles; the pixel step
  * the draft: the plan's videos (Snapchat's first), the copy studio's Snapchat headline, the budget share, the pixel
  * the card says exactly what will be made, and the launch makes exactly that, all PAUSED: media, creative, campaign,
    ad squad, ad; D2 paid plan and D3 ceiling in the account's currency; 9:16 only (the real file), 32 MB, headline
  * idempotency: approving twice is one launch, a finished launch run again makes nothing, a create whose answer was
    lost is found by its tagged name; a video still processing waits
  * compensation: a failure deletes the ads, keeps creatives, media, the campaign and ad squad PAUSED; Try again
    reuses them; 401 marks the connection for Reconnect; 403, 429, 5xx in plain words, wait or retry
  * stats read back as daily rows; Disconnect deletes the token (Snap has no revoke)
  * the Snapchat policy rule (capitals and repeated punctuation) and clean lines staying clean
  * no refresh or access token, client secret or code in any log line, page or the database file
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
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_snap_")
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=base64.b64encode(b"S" * 32).decode())
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "GOOGLE_ADS", "SNAPCHAT_ADS", "SNAPCHAT_CLIENT_ID", "SNAPCHAT_CLIENT_SECRET",
          "AD_RATE_TIER", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "META_APP_ID", "META_APP_SECRET",
          "META_LOGIN_CONFIG_ID"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import adpolicy  # noqa: E402
import db  # noqa: E402
import launch_routes  # noqa: E402
import launches  # noqa: E402
from adplatforms import snapchat  # noqa: E402
from fakesnap import CLIENT_SECRET, FakeSnap  # noqa: E402
from PIL import Image  # noqa: E402

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
launch_routes.APPROVALS_PER_HOUR = launch_routes.UPDATES_PER_HOUR = 10 ** 6
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.62.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


F = FakeSnap()
F.org("org-ember", "Ember Shop LLC",
      accounts=[("acc-ember", "Ember Shop", "USD", "America/Los_Angeles"),
                ("acc-old", "Old Ember", "USD", "America/Los_Angeles", "DISABLED")],
      profiles=[("prof-ember", "Ember Shop")],
      pixels={"acc-ember": [("px-ember", "Ember site pixel"), ("px-test", "Test pixel")]})
F.org("org-other", "Other Co", accounts=[("acc-other", "Other Co ads", "JPY", "Asia/Tokyo")],
      profiles=[("prof-other", "Other Co")], pixels={"acc-other": [("px-other", "Other pixel")]})
adhttp.TRANSPORT = F
owner, u = signup("snap-owner@example.com")
WS = u["workspace_id"]

# ================================================================ 1. the flags
print("flags")
check(owner.post("/connect/snapchat", data={"csrf": csrf(owner)}).status_code == 404, "AD_LAUNCH unset: connect is a 404")
os.environ["AD_LAUNCH"] = "on"
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect Snapchat" not in page and "data-snapchat-checklist" not in page,
      "AD_LAUNCH on, SNAPCHAT_ADS unset (the default): no Snapchat button or checklist")
check(owner.post("/connect/snapchat", data={"csrf": csrf(owner)}).status_code == 404
      and owner.get("/connect/snapchat/callback?state=a&code=b").status_code == 404, "and the Snapchat routes are 404")
os.environ["SNAPCHAT_ADS"] = "1"
page = html(owner.get("/app/settings/ad-accounts"))
check("Connecting Snapchat isn't switched on yet." in page and "Connect Snapchat</button>" not in page,
      "SNAPCHAT_ADS on without the OAuth app: says it isn't switched on, no button")
os.environ.update(SNAPCHAT_CLIENT_ID="snap-fake-client-id", SNAPCHAT_CLIENT_SECRET=CLIENT_SECRET)
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect Snapchat</button>" in page and "data-snapchat-checklist" in page and "Make a public profile" in page
      and "Campaignify never sees your card" in page, "both flags and the app: the button and the create-and-connect checklist")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on Settings")

# ================================================================ 2. OAuth
print("connect")


def start(c, **form):
    r = c.post("/connect/snapchat", data={"csrf": csrf(c), **form})
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    return loc, {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


loc, qs = start(owner)
check(loc.startswith("https://accounts.snapchat.com/login/oauth2/authorize?") and qs["scope"] == "snapchat-marketing-api"
      and qs["response_type"] == "code", "start goes to Snap's OAuth with the snapchat-marketing-api scope")
check(qs["redirect_uri"] == "https://campaignify.test/connect/snapchat/callback" and qs["client_id"] == "snap-fake-client-id"
      and len(qs["state"]) >= 24, "the redirect URI is RS_PUBLIC_URL + /connect/snapchat/callback; a random state")
member, mu = signup("snap-member@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET workspace_id=?, role='member' WHERE id=?", (WS, mu["id"]))
    conn.commit()
check(member.post("/connect/snapchat", data={"csrf": csrf(member)}).status_code == 403, "a member can't start a connect (403)")
owner.get("/connect/snapchat/callback?state=wrong&code=c1")
check("expired" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a wrong state is refused, nothing stored")
loc, qs = start(owner)
owner.get(f"/connect/snapchat/callback?error=access_denied&state={qs['state']}")
check("cancelled" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a cancelled consent changes nothing")
loc, qs = start(owner)
F.code("code-norefresh", orgs=["org-ember"], refresh=False)
owner.get(f"/connect/snapchat/callback?state={qs['state']}&code=code-norefresh")
check("lasting permission" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "no refresh token: refused, nothing stored")
loc, qs = start(owner)
owner.get(f"/connect/snapchat/callback?state={qs['state']}&code=never-issued")
check("didn't work" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a bad code (invalid_grant): plain words")
other, ou = signup("snap-other@example.com")
loc, qs = start(owner)
other.get(f"/connect/snapchat/callback?state={qs['state']}&code=x")
check("expired" in flashes(other), "another person can't use this person's state")

loc, qs = start(owner)
F.code("code-good", user="snap-user-1", orgs=["org-ember", "org-other"])
F.calls.clear()
r = owner.get(f"/connect/snapchat/callback?state={qs['state']}&code=code-good")
row = q("SELECT * FROM ad_connections WHERE platform='snapchat'")[0]
check(r.headers["Location"].endswith(f"/app/settings/ad-accounts/{row['id']}/pick"), "a good code: a pending connection, then the picker")
REFRESH = [k for k in F.refresh][-1]
check(row["status"] == "pending" and row["token_enc"].startswith("cfy1:") and REFRESH not in row["token_enc"]
      and adplatforms.open_token(row) == REFRESH and row["token_expires_at"] is None and row["external_user_id"] == "snap-user-1",
      "the refresh token is stored only in the token box, with no expiry, and who connected")
tok = F.find("/access_token")[0]
check(tok["data"]["grant_type"] == "authorization_code" and tok["data"]["redirect_uri"].endswith("/connect/snapchat/callback")
      and tok["data"]["client_secret"] == CLIENT_SECRET, "the code exchange sends the client secret and the same redirect URI")
check(not any(c["method"] != "GET" for c in F.calls if c["host"] == "adsapi.snapchat.com"),
      "connecting only reads the ad account (every API call is a GET)")

page = html(owner.get(f"/app/settings/ad-accounts/{row['id']}/pick"))
check("Signed in on Snapchat as Ember Owner" in page, "the picker says which Snapchat login")
m = re.search(r'value="acc-ember"[^>]*>\s*<b>Ember Shop', page)
check(m and "disabled" not in m.group(0) and "Ember Shop LLC" in page, "an active ad account can be picked, with its organization")
m = re.search(r'value="acc-old"[^>]*>', page)
check(m and "disabled" in m.group(0) and "Disabled" in page, "a disabled ad account is listed but not pickable")
check("Ember Shop</b>" in page and 'value="prof-ember"' in page and 'value="prof-other"' in page,
      "the organizations' public profiles are listed")
owner.post(f"/app/settings/ad-accounts/{row['id']}/pick", data={"csrf": csrf(owner), "account": "acc-ember", "profile": "prof-other"})
check("public profiles listed" in flashes(owner) and q("SELECT status FROM ad_connections WHERE id=?", row["id"])[0]["status"] == "pending",
      "a public profile of another organization is refused")
r = owner.post(f"/app/settings/ad-accounts/{row['id']}/pick", data={"csrf": csrf(owner), "account": "acc-ember", "profile": "prof-ember"})
row = q("SELECT * FROM ad_connections WHERE id=?", row["id"])[0]
check(row["status"] == "active" and row["external_account_id"] == "acc-ember" and row["business_id"] == "org-ember"
      and row["page_id"] == "prof-ember" and row["page_name"] == "Ember Shop" and row["currency"] == "USD"
      and row["timezone"] == "America/Los_Angeles", "picked: the ad account, its organization, the public profile, currency, zone")
check(r.headers["Location"].endswith(f"/ad-accounts/{row['id']}/pixel"), "two Snap Pixels: the pixel step")
owner.post(f"/app/settings/ad-accounts/{row['id']}/pixel", data={"csrf": csrf(owner), "pixel": "px-ember"})
check([p["external_pixel_id"] for p in q("SELECT * FROM pixels WHERE connection_id=?", row["id"])] == ["px-ember"]
      and "Connected Ember Shop" in flashes(owner), "the picked pixel is saved with the connection")
CONN_ID = row["id"]
page = html(owner.get("/app/settings/ad-accounts"))
check("Public profile: Ember Shop" in page and "Ember site pixel" in page, "Settings lists the connection, its profile and pixel")

# ================================================================ 3. a plan with videos
print("draft and card")
V916 = os.path.join(TMP, "snap916.mp4")
open(V916, "wb").write(b"\x00\x00\x00\x18ftypmp42fake 9:16 video bytes")
Image.new("RGB", (720, 1280), (200, 40, 120)).save(V916 + ".jpg")
V45 = os.path.join(TMP, "feed45.mp4")
open(V45, "wb").write(b"\x00\x00\x00\x18ftypmp42fake 4:5 video bytes")
Image.new("RGB", (864, 1080), (40, 200, 120)).save(V45 + ".jpg")
IMG = os.path.join(TMP, "img.png")
Image.new("RGB", (1080, 1080), (40, 40, 40)).save(IMG)
OKC = {"ok": True, "warnings": [], "blocks": []}
PLAN = {"angles": [{"name": "Evening ritual"}, {"name": "Lasts the month"}],
        "copy": [{"platform": "snapchat", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Evening ritual", "ok": True, "compliance": OKC,
             "fields": [{"name": "Headline", "text": "Light it once, smell it all week"}]}]},
                 {"platform": "meta", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Lasts the month", "ok": True, "compliance": OKC,
             "fields": [{"name": "Primary text", "text": "One candle, fifty hours of slow evenings at home."},
                        {"name": "Headline", "text": "Burns 50 hours"}]}]}],
        "final": {"summary": "Video first.", "budget_split": [{"platform": "snapchat", "share_percent": 50},
                                                               {"platform": "meta", "share_percent": 50}], "checklist": []}}
BRIEF = {"offer": "Ember Candle", "goal": "sales", "audience": "Renters 25-40", "platforms": ["snapchat", "meta"],
         "link": "https://embercandle.example/shop", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14, "budget": "$40 a day for 14 days"}


def make_plan(ws_id, user_id, name="Ember Candle"):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, name, json.dumps(BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        oids = []
        for tool, kind, path, angle, platform in (("ugc_video", "video", V916, "Evening ritual", "snapchat"),
                                                  ("photo_to_video", "video", V45, "Lasts the month", "meta"),
                                                  ("ad_image", "image", IMG, "Still", "snapchat")):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (ws_id, user_id, tool, "b", json.dumps({"ad_type": "demo"}), "done", 43, db.now(),
                                db.now())).lastrowid
            oids.append(conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                                     (jid, kind, "", path)).lastrowid)
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                         "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, ws_id, angle, platform, tool, "b", 43, "queued", jid, db.now()))
        conn.commit()
    return cid, oids


PLAN_ID, OIDS = make_plan(WS, u["id"])
URL = f"/app/campaigns/{PLAN_ID}/launch/snapchat"


def card_page(c, cid=PLAN_ID, **form):
    if form:
        data = {"csrf": csrf(c), "form": "1"}
        data.update(form)
        r = c.post(f"/app/campaigns/{cid}/launch/snapchat", data=data)
        assert r.status_code == 302, (r.status_code, html(r)[:300])
        card_id = int(re.search(r"card=(\d+)", r.headers["Location"]).group(1))
        page = html(c.get(r.headers["Location"]))
    else:
        page = html(c.get(f"/app/campaigns/{cid}/launch/snapchat"))
        card_id = int(re.search(r'name="card" value="(\d+)"', page).group(1))
    return q("SELECT * FROM approval_cards WHERE id=?", card_id)[0], page


def approve(c, card, cid=PLAN_ID):
    r = c.post(f"/app/campaigns/{cid}/launch/snapchat/approve", data={"csrf": csrf(c), "card": card["id"],
                                                                      "hash": card["spec_hash"]})
    m = re.search(r"/app/launches/(\d+)", r.headers.get("Location", ""))
    return r, (int(m.group(1)) if m else None)


def drive(lid, max_runs=60, step=61.0):
    st = None
    for _ in range(max_runs):
        launches.run(lid)
        st = q("SELECT status FROM launches WHERE id=?", lid)[0]["status"]
        if st in launches.FINISHED:
            return st
        T[0] += step
    return st


plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
sblock = plan_page.split('id="kit-snapchat"', 1)[1].split("</article>", 1)[0]
check("Create paused campaign" in sblock and URL in sblock and "Snapchat ad account, PAUSED until you turn it on" in sblock,
      "the launch kit's Snapchat block has the Create button and one line why")
os.environ.pop("SNAPCHAT_ADS")
check(owner.get(URL).status_code == 404, "SNAPCHAT_ADS off: the Snapchat Launch page is a 404")
plan_page = html(owner.get(f"/app/campaigns/{PLAN_ID}"))
check("Create paused campaign" not in plan_page.split('id="kit-snapchat"', 1)[1].split("</article>", 1)[0],
      "and the kit's Snapchat block has no Create button")
os.environ["SNAPCHAT_ADS"] = "1"

card, page = card_page(owner)
spec0, c0 = json.loads(card["spec_json"]), json.loads(card["after_json"])
check(spec0["platform"] == "snapchat" and spec0["objective"] == "sales" and [c["asset_id"] for c in spec0["creatives"]]
      == OIDS[:2] and all(c["kind"] == "video" for c in spec0["creatives"]),
      "the draft has the plan's 2 videos (Snapchat's first), no image")
check(spec0["creatives"][0]["headline"] == "Light it once, smell it all week" and spec0["creatives"][1]["headline"] == "Burns 50 hours"
      and spec0["creatives"][0]["utm"].startswith("utm_source=snapchat"),
      "headlines from the copy studio's Snapchat copy, Meta's when Snapchat has none; Snapchat's UTM")
check(spec0["daily_budget_minor"] == 2000 and spec0["conversion"]["pixel_id"] == "px-ember"
      and spec0["creatives"][0]["aspect"] == "9:16" and spec0["creatives"][1]["aspect"] == "4:5",
      "the budget is Snapchat's 50% share of $40 a day; the connection's pixel; each video's real shape")
blocks = {i["rule_id"] for i in c0["issues"] if i["level"] == "block"}
check({"snapchat.aspect", "snapchat.file_not_vertical"} <= blocks and c0["blocking"],
      "the 4:5 video blocks: Snapchat takes 9:16 only (the spec and the real file)")
check("<button class=\"btn\" disabled" in page and "What Campaignify will create on Snapchat" in page
      and "Public profile: Ember Shop" in page, "the card shows, with the approve button off")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the Snapchat Launch page")

form = {"card": card["id"], f"use_{OIDS[0]}": "1", f"head_{OIDS[0]}": "Light it once, smell it all week",
        f"cta_{OIDS[0]}": "SHOP_NOW", "landing": "https://embercandle.example/shop", "pixel": "px-ember", "event": "Purchase",
        "countries": "US, CA", "age_min": "21", "age_max": "65", "genders": "women", "daily_budget": "25", "vertical": "general"}
card, page = card_page(owner, **form)
c1 = json.loads(card["after_json"])
check(not c1["blocking"], "with the 4:5 video left out nothing blocks")
lines = c1["lines"]
check(lines[0] == "1 campaign: Ember Candle. Sales, PAUSED.", "card: the campaign, PAUSED")
check(lines[1] == "1 ad squad: $25 a day, US, CA, ages 21 and over, women, automatic placements, optimizes for purchases "
      "on Ember site pixel. PAUSED.", "card: the ad squad with the budget, where, who, the goal and the pixel")
check(lines[2].startswith("1 ad, PAUSED: Ember Candle · ad 1 Evening ritual") and
      lines[3] == 'Ad 1: "Light it once, smell it all week", button Shop now, opens https://embercandle.example/shop.',
      "card: the ad, its headline, button and landing page")
check("AI presenter or AI voice" in c1["disclosure"] and "keep the AI label on the video" in c1["disclosure"],
      "card: the AI disclosure for the AI presenter video")
check("<button class=\"btn\">Create paused campaign</button>" in page, "the owner can approve")


def review_with(plan=None, **changes):
    if plan:
        with db.connect() as conn:
            conn.execute("UPDATE workspaces SET plan=?, plan_status='active', plan_override=NULL, comp_plan=NULL WHERE id=?",
                         (plan, WS))
            conn.commit()
    d = json.loads(card["spec_json"])
    cr = changes.pop("cr", None)
    if cr:
        d["creatives"] = [dict(d["creatives"][0], **cr)]
    d.update(changes)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM ad_connections WHERE id=?", (CONN_ID,)).fetchone()
        spec, issues, cd, steps = launches.review(conn, ws(WS), d, row)
    return {i.rule_id for i in issues if i.level == "block"}, issues, cd, steps


print("rules")
b, _, _, _ = review_with(plan="free")
check("launch.paid_plan" in b, "D2: a free workspace can't launch on Snapchat")
for plan, ok_minor, over_minor in (("starter", 20000, 20100), ("growth", 100000, 100100)):
    b_ok, _, _, _ = review_with(plan=plan, daily_budget_minor=ok_minor)
    b_over, _, _, _ = review_with(plan=plan, daily_budget_minor=over_minor)
    check("launch.budget_ceiling" not in b_ok and "launch.budget_ceiling" in b_over, f"D3: the {plan} ceiling holds on Snapchat")
b, _, _, _ = review_with(daily_budget_minor=400)
check("snapchat.budget_min" in b, "under Snapchat's 5.00 a day ad squad minimum blocks")
b, _, _, _ = review_with(cr={"headline": "A headline that runs past thirty-four characters"})
check("snapchat.text.headline_too_long" in b, "a headline over 34 characters blocks")
b, _, _, _ = review_with(cr={"headline": ""})
check("snapchat.headline_missing" in b, "a missing headline blocks")
b, _, _, _ = review_with(cr={"aspect": "4:5"})
check("snapchat.aspect" in b, "a spec that says 4:5 blocks")
b, _, _, _ = review_with(geo={"countries": ["US"], "zips": ["94110"]})
check("snapchat.geo_detail" in b, "ZIP codes block for now (whole countries)")
b, _, _, _ = review_with(special_ad_categories=["social_issues"])
check("snapchat.political" in b, "political ads block (Snap's own approval first)")
b, _, _, _ = review_with(conversion={"pixel_id": "px-other", "event": "Purchase", "landing_url": ""})
check("launch.pixel_unknown" in b, "another account's pixel is refused")
b, _, _, _ = review_with(conversion={"pixel_id": "px-ember", "event": "Schedule", "landing_url": ""})
check("snapchat.event" in b, "an event Snapchat can't optimize for here is refused")
b, iss, _, _ = review_with(audience={"age_min": 18, "age_max": 55, "genders": ["all"]})
check("snapchat.age_band" in {i.rule_id for i in iss}, "an oldest age of 50 to 64 warns: Snap's oldest band is 50 and over")
b, _, _, _ = review_with(currency="EUR")
check("launch.currency" in b, "a spec in another currency than the account is refused")
big = os.path.join(TMP, "big.mp4")
with open(big, "wb") as fh:
    fh.truncate(33 * 1024 * 1024)
with db.connect() as conn:
    conn.execute("UPDATE outputs SET path=? WHERE id=?", (big, OIDS[0]))
    conn.commit()
shutil.copy(V916 + ".jpg", big + ".jpg")
b, _, _, _ = review_with()
check("snapchat.file_large" in b, "a video over 32 MB blocks (Snap's one-request upload)")
with db.connect() as conn:
    conn.execute("UPDATE outputs SET path=? WHERE id=?", (V916, OIDS[0]))
    conn.commit()
os.remove(big)
os.remove(big + ".jpg")
b, _, _, _ = review_with(plan="growth")
check(not b, "back to the card: nothing blocks")
mpage = html(member.get(URL + f"?card={card['id']}"))
check("Only the workspace owner can approve" in mpage, "a member sees the card with the approve button off")
r, lid_m = approve(member, card)
check(r.status_code == 403 and lid_m is None, "a member's approve is refused (403)")
r = other.post(URL + "/approve", data={"csrf": csrf(other), "card": card["id"], "hash": card["spec_hash"]})
check(r.status_code == 404, "another workspace can't approve this plan's card (404)")
check(owner.get(f"/app/campaigns/{PLAN_ID}/launch/meta?card={card['id']}").status_code == 404,
      "a Snapchat card can't be opened on the Meta page")

# ================================================================ 4. the launch
print("launch")
F.calls.clear()
r, lid = approve(owner, card)
check(r.status_code == 302 and lid and "creating the paused campaign on Snapchat" in flashes(owner), "approving queues the launch")
steps = q("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", lid)
check([s["step_key"] for s in steps] == ["upload:1", "creative:1", "campaign", "adset", "ad:1"]
      and [s["kind"] for s in steps] == ["upload", "creative", "campaign", "adsquad", "ad"],
      "the ledger: upload, creative, campaign, ad squad, ad")
check(not F.calls, "nothing is sent to Snapchat at approval")
check(drive(lid) == "paused", "the saga runs to paused")
media, crs, camps, sqs, ads = (F.objects(k) for k in ("media", "creative", "campaign", "adsquad", "ad"))
check(len(media) == len(crs) == len(camps) == len(sqs) == len(ads) == 1, "exactly 1 media, creative, campaign, ad squad and ad")
check(camps[0]["status"] == sqs[0]["status"] == ads[0]["status"] == "PAUSED", "campaign, ad squad and ad are PAUSED")
check(camps[0]["name"] == f"Ember Candle [cfy:{lid}:campaign]" and sqs[0]["name"].endswith(f"[cfy:{lid}:adset]")
      and ads[0]["name"].endswith(f"[cfy:{lid}:ad:1]") and media[0]["name"].endswith(f"[cfy:{lid}:upload:1]"),
      "every name carries the cfy tag")
check(camps[0]["objective_v2_properties"] == {"objective_v2_type": "SALES"} and camps[0]["buy_model"] == "AUCTION"
      and camps[0]["start_time"].endswith("-08:00") and camps[0]["end_time"].endswith("T23:59:59-08:00"),
      "the campaign: Sales, auction, start and end in the account's time zone")
sq = sqs[0]
check(sq["daily_budget_micro"] == 25_000_000 and sq["optimization_goal"] == "PIXEL_PURCHASE" and sq["pixel_id"] == "px-ember"
      and sq["bid_strategy"] == "AUTO_BID" and sq["placement_v2"] == {"config": "AUTOMATIC"} and sq["type"] == "SNAP_ADS",
      "the ad squad: $25 a day as 25,000,000 micro, purchases on the pixel, auto bid, automatic placements")
check(sq["targeting"] == {"geos": [{"country_code": "us"}, {"country_code": "ca"}],
                          "demographics": [{"min_age": "21", "gender": "FEMALE"}]},
      "targeting: US and CA, 21 and over (no max: Snap's 50+ band), women")
cr = crs[0]
check(cr["top_snap_media_id"] == media[0]["id"] and cr["headline"] == "Light it once, smell it all week"
      and cr["profile_properties"] == {"profile_id": "prof-ember"} and cr["call_to_action"] == "SHOP_NOW"
      and cr["web_view_properties"]["url"].startswith("https://embercandle.example/shop?utm_source=snapchat"),
      "the creative: the video as top snap, the headline, the public profile, Shop now, the page with the UTM")
check(ads[0]["creative_id"] == cr["id"] and ads[0]["ad_squad_id"] == sq["id"] and ads[0]["type"] == "REMOTE_WEBPAGE",
      "the ad ties the creative to the ad squad")
up = F.find("/upload", "POST")
check(len(up) == 1 and "file" in up[0]["files"], "the video is uploaded once, as a multipart file")
L = q("SELECT * FROM launches WHERE id=?", lid)[0]
objs = q("SELECT kind, external_id, parent_external_id, status FROM ad_objects WHERE launch_id=? ORDER BY id", lid)
check(L["external_campaign_id"] == camps[0]["id"] and {o["kind"] for o in objs} == {"video", "creative", "campaign", "adsquad", "ad"}
      and next(o for o in objs if o["kind"] == "adsquad")["parent_external_id"] == camps[0]["id"]
      and next(o for o in objs if o["kind"] == "ad")["parent_external_id"] == sq["id"],
      "ad_objects has each object with its parent")
spage = html(owner.get(f"/app/launches/{lid}"))
check("Paused campaign created" in spage and "Open Snapchat Ads Manager" in spage and "Ad squad" in spage,
      "the status page: created, PAUSED, Ads Manager link, the ad squad listed")
check(not re.search(r"[–—]", text(spage)), "no em or en dashes on the status page")

print("idempotency")
r2, lid2 = approve(owner, card)
check(lid2 == lid, "approving the same card again is the same launch")
n_calls = len(F.calls)
launches.run(lid)
check(len(F.calls) == n_calls and len(F.objects("campaign")) == 1, "a finished launch run again sends nothing")

# a create whose answer is lost: found again by its tagged name
form2 = dict(form, name="Ember Candle lost")
card2, _ = card_page(owner, **form2)
F.fail("/adaccounts/acc-ember/campaigns", "lost", method="POST")
r, lid3 = approve(owner, card2)
check(drive(lid3) == "paused", "a campaign create whose answer was lost: the launch still finishes")
lost = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid3}:campaign]")]
check(len(lost) == 1 and len(F.objects("adsquad")) == 2, "and Snap has exactly 1 campaign for it (found by its tagged name)")

# a video still processing waits
F.processing = 2
card3, _ = card_page(owner, **dict(form, name="Ember Candle wait"))
r, lid4 = approve(owner, card3)
launches.run(lid4)
L4 = q("SELECT status, next_at FROM launches WHERE id=?", lid4)[0]
check(L4["status"] == "launching" and L4["next_at"], "a video still processing: the launch waits, it doesn't fail")
check(drive(lid4) == "paused", "and finishes once Snap says READY")
F.processing = 0

# ================================================================ 5. failure, compensation, retry
print("compensation")
card4, _ = card_page(owner, **dict(form, name="Ember Candle fail"))
F.fail("/ads", "snapchat/error_creative_profile.json", method="POST")
r, lid5 = approve(owner, card4)
st = drive(lid5)
L5 = q("SELECT * FROM launches WHERE id=?", lid5)[0]
s5 = {s["step_key"]: s["status"] for s in q("SELECT * FROM launch_steps WHERE launch_id=?", lid5)}
check(st == "rolled_back" and s5 == {"upload:1": "kept", "creative:1": "kept", "campaign": "kept", "adset": "kept",
                                     "ad:1": "failed"},
      "an ad refused: rolled back; media, creative, campaign and ad squad kept")
camp5 = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid5}:campaign]")][0]
check(camp5["status"] == "PAUSED" and "A public profile is required" in L5["error"], "kept PAUSED; Snap's own words are shown")
fpage = html(owner.get(f"/app/launches/{lid5}"))
check("Campaignify removed the ads it had made" in fpage and "Try again" in fpage, "the status page explains and offers Try again")
F.calls.clear()
owner.post(f"/app/launches/{lid5}/retry", data={"csrf": csrf(owner)})
check(drive(lid5) == "paused", "Try again finishes the launch")
check(len([o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid5}:campaign]")]) == 1
      and not F.find("/adaccounts/acc-ember/campaigns", "POST") and not F.find("/upload", "POST"),
      "and reuses what was kept: no second campaign, no second upload")

# an active campaign (someone turned it on in Ads Manager) is paused again by compensation
card5, _ = card_page(owner, **dict(form, name="Ember Candle on"))
r, lid6 = approve(owner, card5)


F.fail("/adsquads", "snapchat/error_adsquad_budget.json", method="POST")
st = drive(lid6)
camp6 = [o for o in F.objects("campaign") if o["name"].endswith(f"[cfy:{lid6}:campaign]")][0]
check(st == "rolled_back" and "at least 5.00 USD" in q("SELECT error FROM launches WHERE id=?", lid6)[0]["error"],
      "an ad squad refused for its budget: rolled back with Snap's reason")
camp6["status"] = "ACTIVE"
with db.connect() as conn:
    L6 = conn.execute("SELECT * FROM launches WHERE id=?", (lid6,)).fetchone()
    conn.execute("UPDATE launch_steps SET status='done' WHERE launch_id=? AND step_key='campaign'", (lid6,))
    conn.commit()
    launches.compensate(conn, L6, REFRESH)
check(camp6["status"] == "PAUSED" and F.find("/adaccounts/acc-ember/campaigns", "PUT"),
      "a campaign someone turned on is paused again (a whole-object PUT)")

print("errors")
for fx, rule, words in (("snapchat/error_forbidden.json", adhttp.PermissionDenied, "Account Manager role"),
                        ("snapchat/error_internal.json", adhttp.AdHttpError, "Snapchat said: Something went wrong")):
    F.fail("/adaccounts/acc-ember/campaigns", (403 if "forbidden" in fx else 500, fx), times=5, method="POST")
    cd, _ = card_page(owner, **dict(form, name=f"Ember Candle {fx[-12:-5]}"))
    r, lx = approve(owner, cd)
    st = drive(lx)
    err = q("SELECT error FROM launches WHERE id=?", lx)[0]["error"]
    check(st == "rolled_back" and words in err, f"{fx.split('/')[-1]}: {words!r}")
    F.failures.clear()
F.fail("/adaccounts/acc-ember/campaigns", (429, "snapchat/error_rate_limited.json"), times=1, method="POST")
cd, _ = card_page(owner, **dict(form, name="Ember Candle slow"))
r, lx = approve(owner, cd)
check(drive(lx) == "paused", "429: the step waits and then finishes")
F.fail("/adaccounts/acc-ember/media", (401, "snapchat/error_unauthorized.json"), times=1, method="POST")
cd, _ = card_page(owner, **dict(form, name="Ember Candle 401"))
r, lx = approve(owner, cd)
st = drive(lx)
check(st == "rolled_back" and "Reconnect it in Settings" in q("SELECT error FROM launches WHERE id=?", lx)[0]["error"]
      and q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"] == "expired",
      "401: plain words and the connection is marked for Reconnect")
page = html(owner.get("/app/settings/ad-accounts"))
check("Needs reconnect" in page and "Reconnect</button>" in page, "Settings shows Reconnect for it")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='active' WHERE id=?", (CONN_ID,))
    conn.commit()
check(isinstance(adhttp._classify("snapchat", adhttp.Response(400, {}, {"error": "invalid_grant"}), [])[0], adhttp.TokenInvalid),
      "an invalid_grant on refresh reads as Reconnect")
snapchat._ACCESS.clear()
F.refresh.pop(REFRESH)
try:
    snapchat.access_token(REFRESH)
    ok = False
except adhttp.TokenInvalid:
    ok = True
check(ok, "a refresh token Snap no longer honours raises TokenInvalid")
F.refresh[REFRESH] = "snap-user-1"

# ================================================================ 6. stats
print("stats")
F.stats[camps[0]["id"]] = [("2026-11-07", {"impressions": 1200, "swipes": 34, "spend": 12_345_678, "conversion_purchases": 2,
                                           "conversion_sign_ups": 0, "conversion_purchases_value": 61_000_000}),
                           ("2026-11-08", {"impressions": 900, "swipes": 20, "spend": 8_000_000})]
rows = snapchat.ADAPTER.pull_insights({"token": REFRESH, "account": "acc-ember", "currency": "USD",
                                       "timezone": "America/Los_Angeles"},
                                      [{"kind": "campaign", "external_id": camps[0]["id"]}], ("2026-11-07", "2026-11-08"))
check(len(rows) == 2 and rows[0]["day"] == "2026-11-07" and rows[0]["spend_minor"] == 1234 and rows[0]["clicks"] == 34
      and rows[0]["purchases"] == 2 and rows[0]["purchase_value_minor"] == 6100 and rows[1]["spend_minor"] == 800,
      "daily rows: spend from micro to cents, swipes as clicks, purchases and their value")
sc = F.find("/stats")[-1]["params"]
check(sc["start_time"].startswith("2026-11-07T00:00:00-08:00") and sc["end_time"].startswith("2026-11-09T00:00:00-08:00"),
      "whole days at midnight in the account's time zone")

# ================================================================ 7. policy
print("policy")
hits = {i.rule_id for i in adpolicy.lint_text("SHOP THE BIG CANDLE SALE NOW!!", platform="snapchat")}
check("policy.caps_punctuation" in hits, "Snapchat: a headline in capitals with !! warns")
check(not adpolicy.lint_text("SHOP THE BIG CANDLE SALE NOW!!", platform="meta"), "the same line on Meta: no finding")
for t in ("Light it once, smell it all week", "Burns 50 hours", "New USA-made soy candles", "Book a cleaning today",
          "Fresh roasted beans, shipped fast", "Wait, what? It smells like cedar."):
    check(not adpolicy.lint_text(t, platform="snapchat"), f"clean on Snapchat: {t!r}")

# ================================================================ 8. disconnect and secrets
print("disconnect and secrets")
r = owner.post(f"/app/settings/ad-accounts/{CONN_ID}/disconnect", data={"csrf": csrf(owner)})
row = q("SELECT * FROM ad_connections WHERE id=?", CONN_ID)[0]
check(row["status"] == "revoked" and row["token_enc"] is None and not q("SELECT 1 FROM pixels WHERE connection_id=?", CONN_ID)
      and "connected apps" in flashes(owner), "Disconnect deletes the token and pixels and says where to remove the app on Snap")
secrets = [REFRESH, CLIENT_SECRET] + list(F.tokens)
logs = "\n".join(CAP.lines)
check(not any(s in logs for s in secrets), "no refresh token, access token or client secret in any log line")
raw = open(db.DB_PATH, "rb").read()
check(not any(s.encode() in raw for s in secrets), "nor in the database file")
pages = html(owner.get("/app/settings/ad-accounts")) + spage + fpage
check(not any(s in pages for s in secrets), "nor in any page")

print("== a public profile pasted by the owner (Snap lists profiles only to allowlisted apps)")
import adconnect_routes  # noqa: E402
U = "783de194-f87f-4387-9c55-6ffad75f66c2"
check(adconnect_routes.snap_profile_id(f"https://profile.snapchat.com/2439fe9d-a300-40fc-b8e1-899e4bc758c9/profiles/{U}/details/public-stories?ref_aid=86157ec4-a38d-44e1-9836-130e81a34375") == U,
      "the profile id is read from the Ads Manager address, not the organization or ad account id in it")
check(adconnect_routes.snap_profile_id(U.upper()) == U and adconnect_routes.snap_profile_id("not a link") is None
      and adconnect_routes.snap_profile_id(f"{U} 2439fe9d-a300-40fc-b8e1-899e4bc758c9") is None, "a bare id works; nothing or two ids is refused")
tpl = open(os.path.join(os.path.dirname(os.path.abspath(adconnect_routes.__file__)), "web", "ad_connect_snapchat.html")).read()
check('name="profile_link"' in tpl and "Public Profiles" in tpl, "with no profile listed, the picker asks for the profile link")

print(f"\nall {passed} checks passed")
