"""TikTok connect and launch (roadmap M6.2) on a throwaway database. TikTok is the fake API for Business in
tests/faketiktok.py (answers shaped like tests/fixtures/tiktok/*.json, hand-written from TikTok's v1.3 reference, to
re-record with tools/tiktok_sandbox_launch.py): no request leaves the machine.

    python tests/test_tiktok.py      (tests/test_app.py runs it too)

  * TIKTOK_ADS off (the default) or AD_LAUNCH off: no TikTok button, every TikTok route is a 404, no Create button in
    the launch kit; on: they show
  * the advertiser authorization: app id, state, the fixed redirect URI; the token goes only into the token box; the
    advertiser picker; the identity step (pick one, or make one); the pixel step by pixel code; a hand-added TikTok
    pixel stays the member's (Disconnect doesn't delete it)
  * the draft: the plan's videos, the copy studio's TikTok ad text, the budget share, the pixel
  * the card says exactly what will be made, and the launch makes exactly that: uploads (video and cover), the campaign
    turned off, the ad group and the ads on inside it; the pixel code resolved to TikTok's pixel id; D2, D3, the $20 ad
    group minimum, ad text of 1 to 100 characters without emoji, age bands, countries, the video's shape and cover
  * idempotency: approving twice is one launch, a finished launch run again makes nothing, a create whose answer was
    lost is found by its tagged name; a video still processing waits
  * compensation: a failure deletes the ads and keeps the campaign off; Try again reuses it; 40105 marks the
    connection for Reconnect; 40001, 40100 and 50000 in plain words, wait or retry
  * reporting read back as daily rows; Disconnect revokes the token at TikTok
  * no access token, app secret or auth code in any log line, page or the database file
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
TMP = tempfile.mkdtemp(prefix="rs_tiktok_")
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=base64.b64encode(b"T" * 32).decode())
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "GOOGLE_ADS", "SNAPCHAT_ADS", "TIKTOK_ADS", "TIKTOK_APP_ID",
          "TIKTOK_APP_SECRET", "AD_RATE_TIER", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "META_APP_ID",
          "META_APP_SECRET", "META_LOGIN_CONFIG_ID", "CONVERSIONS_API"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import adpolicy  # noqa: E402
import db  # noqa: E402
import launch_routes  # noqa: E402
import launches  # noqa: E402
from adplatforms import tiktok  # noqa: E402
from faketiktok import APP_ID, APP_SECRET, FakeTikTok  # noqa: E402
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
    c.environ_base["REMOTE_ADDR"] = f"10.63.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


F = FakeTikTok()
ADV, ADV_OLD, ADV_OTHER = "7001000000000000001", "7001000000000000002", "7009000000000000009"
F.advertiser(ADV, "Ember Shop", currency="USD", tz="America/Los_Angeles")
F.advertiser(ADV_OLD, "Ember Old", status="STATUS_DISABLE")
F.advertiser(ADV_OTHER, "Not shared")
F.identity(ADV, "ident-ember", "Ember Candle")
F.pixel(ADV, "7300000000000000001", "CEMBERPIXEL00000001", "Ember site pixel")
F.pixel(ADV, "7300000000000000002", "CEMBERTESTPX0000002", "Test pixel")
adhttp.TRANSPORT = F
owner, u = signup("tt-owner@example.com")
WS = u["workspace_id"]
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET brand_name='Ember Candle Co' WHERE id=?", (WS,))
    # a TikTok pixel the member added by hand in Conversion tracking (with an Events API token)
    conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) VALUES "
                 "(?,?,?,?,?,?)", (WS, "tiktok", None, "CEMBERTESTPX0000002", "Hand-added pixel", db.now()))
    conn.commit()

# ================================================================ 1. the flags
print("flags")
check(owner.post("/connect/tiktok", data={"csrf": csrf(owner)}).status_code == 404, "AD_LAUNCH unset: connect is a 404")
os.environ["AD_LAUNCH"] = "on"
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect TikTok" not in page and "data-tiktok-checklist" not in page, "TIKTOK_ADS unset (the default): no TikTok button")
check(owner.post("/connect/tiktok", data={"csrf": csrf(owner)}).status_code == 404
      and owner.get("/connect/tiktok/callback?state=a&auth_code=b").status_code == 404, "and the TikTok routes are 404")
os.environ["TIKTOK_ADS"] = "1"
check("Connecting TikTok isn't switched on yet." in html(owner.get("/app/settings/ad-accounts")),
      "TIKTOK_ADS on without the developer app: says it isn't switched on")
os.environ.update(TIKTOK_APP_ID=APP_ID, TIKTOK_APP_SECRET=APP_SECRET)
page = html(owner.get("/app/settings/ad-accounts"))
check("Connect TikTok</button>" in page and "data-tiktok-checklist" in page and "Campaignify never sees your card" in page,
      "both flags and the app: the button and the create-and-connect checklist")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on Settings")

# ================================================================ 2. connect
print("connect")


def start(c):
    r = c.post("/connect/tiktok", data={"csrf": csrf(c)})
    assert r.status_code == 302, r.status_code
    loc = r.headers["Location"]
    return loc, {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


loc, qs = start(owner)
check(loc.startswith("https://business-api.tiktok.com/portal/auth?") and qs["app_id"] == APP_ID
      and qs["redirect_uri"] == "https://campaignify.test/connect/tiktok/callback" and len(qs["state"]) >= 24,
      "start goes to TikTok's advertiser authorization with the app id, the fixed redirect URI and a state")
member, mu = signup("tt-member@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET workspace_id=?, role='member' WHERE id=?", (WS, mu["id"]))
    conn.commit()
check(member.post("/connect/tiktok", data={"csrf": csrf(member)}).status_code == 403, "a member can't start a connect (403)")
owner.get("/connect/tiktok/callback?state=wrong&auth_code=c1")
check("expired" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a wrong state is refused, nothing stored")
loc, qs = start(owner)
owner.get(f"/connect/tiktok/callback?state={qs['state']}")
check("cancelled" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "no auth_code (cancelled): nothing changes")
loc, qs = start(owner)
owner.get(f"/connect/tiktok/callback?state={qs['state']}&auth_code=never-issued")
check("didn't work" in flashes(owner) and not q("SELECT 1 FROM ad_connections"), "a bad auth_code: plain words")
loc, qs = start(owner)
F.code("auth-good", user="tt-user-1", advertisers=[ADV, ADV_OLD])
F.calls.clear()
r = owner.get(f"/connect/tiktok/callback?state={qs['state']}&auth_code=auth-good&code=auth-good")
row = q("SELECT * FROM ad_connections WHERE platform='tiktok'")[0]
TOKEN = [t for t in F.tokens][-1]
check(r.headers["Location"].endswith(f"/app/settings/ad-accounts/{row['id']}/pick"), "a good auth_code: pending, then the picker")
check(row["status"] == "pending" and TOKEN not in row["token_enc"] and adplatforms.open_token(row) == TOKEN
      and row["external_user_id"] == "tt-user-1", "the access token is stored only in the token box; who connected")
tok = F.find("/oauth2/access_token/")[0]
check(tok["data"] == {"app_id": APP_ID, "secret": APP_SECRET, "auth_code": "auth-good"}, "the exchange sends app id, secret, code")
check(all(c["headers"].get("Access-Token") == TOKEN for c in F.calls if not c["path"].endswith("/access_token/"))
      and not any(c["method"] != "GET" for c in F.calls if not c["path"].endswith("/access_token/")),
      "the token goes in the Access-Token header; connecting only reads")
page = html(owner.get(f"/app/settings/ad-accounts/{row['id']}/pick"))
check("Signed in on TikTok as Ember Owner" in page and "Not shared" not in page, "the picker lists only shared advertisers")
m = re.search(r'value="' + ADV_OLD + r'"[^>]*>', page)
check(m and "disabled" in m.group(0) and "Disabled" in page, "a disabled advertiser is listed but not pickable")
r = owner.post(f"/app/settings/ad-accounts/{row['id']}/pick", data={"csrf": csrf(owner), "account": ADV})
row = q("SELECT * FROM ad_connections WHERE id=?", row["id"])[0]
check(row["status"] == "active" and row["external_account_id"] == ADV and row["currency"] == "USD"
      and row["timezone"] == "America/Los_Angeles" and r.headers["Location"].endswith(f"/ad-accounts/{row['id']}/identity"),
      "picked: the advertiser, its currency and zone; then the identity step")
page = html(owner.get(f"/app/settings/ad-accounts/{row['id']}/identity"))
check('value="ident-ember"' in page and 'value="Ember Candle Co"' in page and "Make a new one" in page,
      "the identity step lists the custom identities and offers a new one named after the brand")
r = owner.post(f"/app/settings/ad-accounts/{row['id']}/identity", data={"csrf": csrf(owner), "identity": "new",
                                                                         "display_name": "Ember Candle Co"})
row = q("SELECT * FROM ad_connections WHERE id=?", row["id"])[0]
made = F.find("/identity/create/")
check(made and made[0]["data"]["display_name"] == "Ember Candle Co" and row["page_name"] == "Ember Candle Co"
      and row["page_id"].startswith("ident-"), "a new identity is made on TikTok and kept with the connection")
check(r.headers["Location"].endswith(f"/ad-accounts/{row['id']}/pixel"), "two pixels: the pixel step")
owner.post(f"/app/settings/ad-accounts/{row['id']}/pixel", data={"csrf": csrf(owner), "pixel": "CEMBERPIXEL00000001"})
pxrows = q("SELECT * FROM pixels WHERE workspace_id=? AND platform='tiktok' ORDER BY id", WS)
check([(p["external_pixel_id"], p["connection_id"]) for p in pxrows] == [("CEMBERTESTPX0000002", None),
                                                                         ("CEMBERPIXEL00000001", row["id"])],
      "the picked pixel is saved by its code; the hand-added one stays the member's")
CONN_ID = row["id"]
# the identity can be switched later: use the existing one for the launches below
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET page_id='ident-ember', page_name='Ember Candle' WHERE id=?", (CONN_ID,))
    conn.commit()

# ================================================================ 3. a plan with videos
print("draft and card")
V916 = os.path.join(TMP, "tt916.mp4")
open(V916, "wb").write(b"\x00\x00\x00\x18ftypmp42fake 9:16 video bytes")
Image.new("RGB", (720, 1280), (200, 40, 120)).save(V916 + ".jpg")
V45 = os.path.join(TMP, "feed45.mp4")
open(V45, "wb").write(b"\x00\x00\x00\x18ftypmp42fake 4:5 video bytes")
Image.new("RGB", (864, 1080), (40, 200, 120)).save(V45 + ".jpg")
OKC = {"ok": True, "warnings": [], "blocks": []}
PLAN = {"angles": [{"name": "Evening ritual"}],
        "copy": [{"platform": "tiktok", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Evening ritual", "ok": True, "compliance": OKC,
             "fields": [{"name": "Ad text", "text": "Light it once and smell it all week. Hand poured soy, 50 hours of burn."}]}]}],
        "final": {"summary": "Video first.", "budget_split": [{"platform": "tiktok", "share_percent": 75},
                                                               {"platform": "meta", "share_percent": 25}], "checklist": []}}
BRIEF = {"offer": "Ember Candle", "goal": "sales", "audience": "Renters 25-40", "platforms": ["tiktok", "meta"],
         "link": "https://embercandle.example/shop", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14, "budget": "$40 a day for 14 days"}


def make_plan(ws_id, user_id):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, "Ember Candle", json.dumps(BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        oids = []
        for tool, path, angle, platform in (("photo_to_video", V916, "Evening ritual", "tiktok"),
                                            ("photo_to_video", V45, "Square-ish", "meta")):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (ws_id, user_id, tool, "b", "{}", "done", 43, db.now(), db.now())).lastrowid
            oids.append(conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)",
                                     (jid, "video", "", path)).lastrowid)
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                         "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, ws_id, angle, platform, tool, "b", 43, "queued", jid, db.now()))
        conn.commit()
    return cid, oids


PLAN_ID, OIDS = make_plan(WS, u["id"])
URL = f"/app/campaigns/{PLAN_ID}/launch/tiktok"


def card_page(c, **form):
    if form:
        data = {"csrf": csrf(c), "form": "1"}
        data.update(form)
        r = c.post(URL, data=data)
        assert r.status_code == 302, (r.status_code, html(r)[:300])
        card_id = int(re.search(r"card=(\d+)", r.headers["Location"]).group(1))
        page = html(c.get(r.headers["Location"]))
    else:
        page = html(c.get(URL))
        card_id = int(re.search(r'name="card" value="(\d+)"', page).group(1))
    return q("SELECT * FROM approval_cards WHERE id=?", card_id)[0], page


def approve(c, card):
    r = c.post(URL + "/approve", data={"csrf": csrf(c), "card": card["id"], "hash": card["spec_hash"]})
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
tblock = plan_page.split('id="kit-tiktok"', 1)[1].split("</article>", 1)[0]
check("Create paused campaign" in tblock and URL in tblock, "the launch kit's TikTok block has the Create button")
os.environ.pop("TIKTOK_ADS")
check(owner.get(URL).status_code == 404, "TIKTOK_ADS off: the TikTok Launch page is a 404")
os.environ["TIKTOK_ADS"] = "1"

card, page = card_page(owner)
spec0, c0 = json.loads(card["spec_json"]), json.loads(card["after_json"])
check(spec0["platform"] == "tiktok" and [c["asset_id"] for c in spec0["creatives"]] == OIDS
      and spec0["creatives"][0]["primary_text"] == "Light it once and smell it all week. Hand poured soy, 50 hours of burn."
      and spec0["creatives"][0]["headline"] == "" and spec0["creatives"][0]["utm"].startswith("utm_source=tiktok"),
      "the draft: the plan's videos (TikTok's first), the copy studio's TikTok ad text, TikTok's UTM")
check(spec0["daily_budget_minor"] == 3000 and spec0["conversion"]["pixel_id"] == "CEMBERPIXEL00000001",
      "75% of $40 a day = $30; the connection's pixel")
blocks = {i["rule_id"] for i in c0["issues"] if i["level"] == "block"}
check({"tiktok.aspect", "tiktok.file_shape"} <= blocks, "the 4:5 video blocks: TikTok takes 9:16, 1:1 or 16:9 (spec and file)")
check('Ad text <span class="dim">(100 characters)</span>' in page and "Headline" not in page.split("The ads", 1)[1].split("Update the card")[0],
      "the page asks for ad text, not a headline")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the TikTok Launch page")

form = {"card": card["id"], f"use_{OIDS[0]}": "1", f"text_{OIDS[0]}": "Light it once and smell it all week.",
        f"cta_{OIDS[0]}": "SHOP_NOW", "landing": "https://embercandle.example/shop", "pixel": "CEMBERPIXEL00000001",
        "event": "Purchase", "countries": "US, CA", "age_min": "25", "age_max": "44", "genders": "women",
        "daily_budget": "25", "vertical": "general"}
card, page = card_page(owner, **form)
c1 = json.loads(card["after_json"])
check(not c1["blocking"], "with the 4:5 video left out nothing blocks")
lines = c1["lines"]
check(lines[0] == "1 campaign: Ember Candle. Sales, turned off (PAUSED).", "card: the campaign, turned off")
check(lines[1] == "1 ad group: $25 a day, US, CA, ages 25 to 44, women, the TikTok placement, optimizes for purchases on "
      "Ember site pixel. On, inside the turned-off campaign.", "card: the ad group with budget, where, who, goal, pixel")
check(lines[2] == "1 ad, on inside the turned-off campaign, showing as Ember Candle: Ember Candle · ad 1 Evening ritual."
      and lines[3] == 'Ad 1: "Light it once and smell it all week.", button Shop now, opens https://embercandle.example/shop.',
      "card: the ad, the identity it shows as, its text, button and page")
check("turning the campaign on is all it takes" in c1["footer"] and "AI-generated content label" in c1["disclosure"],
      "card: what turning it on does; TikTok's AI label to turn on")


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
check("launch.paid_plan" in b, "D2: a free workspace can't launch on TikTok")
for plan, ok_minor, over_minor in (("starter", 20000, 20100), ("growth", 100000, 100100)):
    b_ok, _, _, _ = review_with(plan=plan, daily_budget_minor=ok_minor)
    b_over, _, _, _ = review_with(plan=plan, daily_budget_minor=over_minor)
    check("launch.budget_ceiling" not in b_ok and "launch.budget_ceiling" in b_over, f"D3: the {plan} ceiling holds on TikTok")
b, _, _, _ = review_with(daily_budget_minor=1500)
check("tiktok.budget_min" in b, "under TikTok's 20.00 a day ad group minimum blocks")
b, _, _, _ = review_with(cr={"primary_text": "x" * 101})
check("tiktok.text.primary_text_too_long" in b, "ad text over 100 characters blocks")
b, _, _, _ = review_with(cr={"primary_text": ""})
check("tiktok.text.primary_text_missing" in b, "missing ad text blocks")
b, _, _, _ = review_with(cr={"primary_text": "Light it once 🔥"})
check("policy.emoji" in b, "emoji in the ad text block (TikTok doesn't take them)")
b, iss, _, _ = review_with(cr={"primary_text": "LIGHT IT ONCE, SMELL IT ALL WEEK"})
check("policy.caps_punctuation" in {i.rule_id for i in iss if i.level == "warn"}, "ad text in capitals warns")
b, _, _, _ = review_with(geo={"countries": ["US", "KP"]})
check("tiktok.country_unsupported" in b, "a country Campaignify can't target on TikTok blocks")
b, iss, _, _ = review_with(audience={"age_min": 21, "age_max": 40, "genders": ["all"]})
check("tiktok.age_band" in {i.rule_id for i in iss}, "ages off TikTok's band edges warn (21 to 40 reaches 18 to 44)")
b, _, _, _ = review_with(special_ad_categories=["social_issues"])
check("tiktok.political" in b, "political ads block")
b, _, _, _ = review_with(conversion={"pixel_id": "CEMBERTESTPX0000002", "event": "Purchase", "landing_url": ""})
check("launch.pixel_unknown" not in b, "a hand-added TikTok pixel of the workspace can be picked")
b, _, _, _ = review_with(conversion={"pixel_id": "CNOTOURS0000000000", "event": "Purchase", "landing_url": ""})
check("launch.pixel_unknown" in b, "a pixel that isn't the workspace's is refused")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET page_id=NULL WHERE id=?", (CONN_ID,))
    conn.commit()
b, _, _, _ = review_with()
check("tiktok.no_profile" in b, "no identity picked blocks")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET page_id='ident-ember' WHERE id=?", (CONN_ID,))
    conn.commit()
b, _, _, _ = review_with(plan="growth")
check(not b, "back to the card: nothing blocks")
r, lid_m = approve(member, card)
check(r.status_code == 403 and lid_m is None, "a member's approve is refused (403)")

# ================================================================ 4. the launch
print("launch")
F.calls.clear()
r, lid = approve(owner, card)
check(lid and "creating the paused campaign on TikTok" in flashes(owner), "approving queues the launch")
steps = q("SELECT * FROM launch_steps WHERE launch_id=? ORDER BY step_no", lid)
check([s["step_key"] for s in steps] == ["upload:1", "campaign", "adset", "ad:1"]
      and [s["kind"] for s in steps] == ["upload", "campaign", "adgroup", "ad"], "the ledger: upload, campaign, ad group, ad")
check(not F.calls, "nothing is sent to TikTok at approval")
check(drive(lid) == "paused", "the saga runs to paused")
camps, ags, ads = F.objects("campaign"), F.objects("adgroup"), F.objects("ad")
check(len(camps) == len(ags) == len(ads) == 1 and len(F.objects("video")) == 1 and len(F.objects("image")) == 1,
      "exactly 1 video, 1 cover, 1 campaign, 1 ad group and 1 ad")
camp, ag, ad = camps[0], ags[0], ads[0]
check(camp["operation_status"] == "DISABLE" and camp["objective_type"] == "WEB_CONVERSIONS"
      and camp["campaign_name"] == f"Ember Candle [cfy:{lid}:campaign]", "the campaign: turned off, web conversions, tagged")
check(ag["budget"] == 25.0 and ag["budget_mode"] == "BUDGET_MODE_DAY" and ag["optimization_goal"] == "CONVERT"
      and ag["optimization_event"] == "SHOPPING" and ag["pixel_id"] == "7300000000000000001" and ag["billing_event"] == "OCPM",
      "the ad group: $25 a day, optimizes for purchases on TikTok's pixel id (looked up from the code)")
check(ag["location_ids"] == ["6252001", "6251999"] and ag["age_groups"] == ["AGE_25_34", "AGE_35_44"]
      and ag["gender"] == "GENDER_FEMALE" and ag["placements"] == ["PLACEMENT_TIKTOK"]
      and ag["schedule_type"] == "SCHEDULE_START_END" and ag["schedule_start_time"].endswith(" 08:00:00"),
      "targeting: US and CA, 25 to 44 in bands, women, the TikTok placement; start at local midnight in UTC")
check(ad["identity_type"] == "CUSTOMIZED_USER" and ad["identity_id"] == "ident-ember" and ad["ad_format"] == "SINGLE_VIDEO"
      and ad["ad_text"] == "Light it once and smell it all week." and ad["call_to_action"] == "SHOP_NOW"
      and ad["landing_page_url"].startswith("https://embercandle.example/shop?utm_source=tiktok")
      and ad["ad_name"].endswith(f"[cfy:{lid}:ad:1]"), "the ad: identity, video, text, Shop now, the page with the UTM")
objs = {o["kind"]: o for o in q("SELECT * FROM ad_objects WHERE launch_id=?", lid)}
check(objs["campaign"]["status"] == "PAUSED" and objs["adgroup"]["status"] == "ENABLED" and objs["ad"]["status"] == "ENABLED"
      and objs["adgroup"]["parent_external_id"] == camp["campaign_id"], "ad_objects: the campaign off, the ad group and ad on")
spage = html(owner.get(f"/app/launches/{lid}"))
check("Paused campaign created" in spage and "Open TikTok Ads Manager" in spage and "on, inside the paused campaign" in spage,
      "the status page: created, the Ads Manager link, the ad group and ad on inside the paused campaign")
check(not re.search(r"[–—]", text(spage)), "no em or en dashes on the status page")

print("idempotency")
check(approve(owner, card)[1] == lid, "approving the same card again is the same launch")
n = len(F.calls)
launches.run(lid)
check(len(F.calls) == n, "a finished launch run again sends nothing")
card2, _ = card_page(owner, **dict(form, name="Ember Candle lost"))
F.fail("/campaign/create/", "lost")
r, lid3 = approve(owner, card2)
check(drive(lid3) == "paused" and len([o for o in F.objects("campaign") if o["campaign_name"].endswith(f"[cfy:{lid3}:campaign]")]) == 1,
      "a campaign create whose answer was lost: found by its tagged name, exactly 1 made")
F.processing = 2
card3, _ = card_page(owner, **dict(form, name="Ember Candle wait"))
r, lid4 = approve(owner, card3)
launches.run(lid4)
check(q("SELECT status FROM launches WHERE id=?", lid4)[0]["status"] == "launching", "a video still processing: the launch waits")
check(drive(lid4) == "paused", "and finishes once TikTok has it")
F.processing = 0

# ================================================================ 5. failure, compensation, errors
print("compensation and errors")
card4, _ = card_page(owner, **dict(form, name="Ember Candle fail"))
F.fail("/ad/create/", {"code": 40002, "message": "The landing page can't be reached.", "request_id": "x", "data": {}})
r, lid5 = approve(owner, card4)
st = drive(lid5)
s5 = {s["step_key"]: s["status"] for s in q("SELECT * FROM launch_steps WHERE launch_id=?", lid5)}
camp5 = [o for o in F.objects("campaign") if o["campaign_name"].endswith(f"[cfy:{lid5}:campaign]")][0]
check(st == "rolled_back" and s5 == {"upload:1": "kept", "campaign": "kept", "adset": "kept", "ad:1": "failed"}
      and camp5["operation_status"] == "DISABLE" and "TikTok said: The landing page" in q("SELECT error FROM launches WHERE id=?", lid5)[0]["error"],
      "an ad refused: rolled back with TikTok's words; upload, campaign and ad group kept, the campaign off")
camp5["operation_status"] = "ENABLE"
with db.connect() as conn:
    L5 = conn.execute("SELECT * FROM launches WHERE id=?", (lid5,)).fetchone()
    conn.execute("UPDATE launch_steps SET status='done' WHERE launch_id=? AND step_key='campaign'", (lid5,))
    conn.commit()
    launches.compensate(conn, L5, TOKEN)
check(camp5["operation_status"] == "DISABLE", "a campaign someone turned on is turned off again by compensation")
with db.connect() as conn:
    conn.execute("UPDATE launch_steps SET status='kept' WHERE launch_id=? AND step_key='campaign'", (lid5,))
    conn.commit()
F.calls.clear()
owner.post(f"/app/launches/{lid5}/retry", data={"csrf": csrf(owner)})
check(drive(lid5) == "paused" and not F.find("/campaign/create/") and not F.find("/file/video/ad/upload/"),
      "Try again finishes, reusing the kept campaign and video")
for fx, words in (("tiktok/error_permission.json", "Operator access"), ("tiktok/error_budget.json", "TikTok said: The daily budget")):
    F.fail("/adgroup/create/", fx, times=5)
    cd, _ = card_page(owner, **dict(form, name=f"Ember Candle {fx[-10:-5]}"))
    r, lx = approve(owner, cd)
    check(drive(lx) == "rolled_back" and words in q("SELECT error FROM launches WHERE id=?", lx)[0]["error"],
          f"{fx.split('/')[-1]}: {words!r}")
    F.failures.clear()
for fx, what in (("tiktok/error_rate.json", "40100 waits"), ("tiktok/error_internal.json", "50000 is retried")):
    F.fail("/campaign/create/", fx, times=1)
    cd, _ = card_page(owner, **dict(form, name=f"Ember Candle {fx[-9:-5]}"))
    r, lx = approve(owner, cd)
    check(drive(lx) == "paused", f"{what}, then the launch finishes")
F.fail("/file/video/ad/upload/", "tiktok/error_token.json", times=1)
cd, _ = card_page(owner, **dict(form, name="Ember Candle 40105"))
r, lx = approve(owner, cd)
check(drive(lx) == "rolled_back" and "Reconnect it in Settings" in q("SELECT error FROM launches WHERE id=?", lx)[0]["error"]
      and q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"] == "expired",
      "40105: plain words and the connection is marked for Reconnect")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET status='active' WHERE id=?", (CONN_ID,))
    conn.commit()

# ================================================================ 6. reporting and policy
print("reporting and policy")
F.report = [{"dimensions": {"campaign_id": camp["campaign_id"], "stat_time_day": "2026-11-07 00:00:00"},
             "metrics": {"spend": "12.34", "impressions": "1500", "clicks": "41", "conversion": "3", "complete_payment": "2"}}]
rows = tiktok.ADAPTER.pull_insights({"token": TOKEN, "account": ADV, "currency": "USD"},
                                    [{"kind": "campaign", "external_id": camp["campaign_id"]}], ("2026-11-07", "2026-11-08"))
check(len(rows) == 1 and rows[0]["day"] == "2026-11-07" and rows[0]["spend_minor"] == 1234 and rows[0]["clicks"] == 41
      and rows[0]["purchases"] == 2, "daily rows: spend in cents, clicks, purchases")
rq = F.find("/report/integrated/get/")[-1]["params"]
check(rq["dimensions"] == ["campaign_id", "stat_time_day"] and rq["filtering"][0]["field_name"] == "campaign_ids",
      "one report call, by campaign and day, filtered to the launched campaigns")
err_tok, _ = adhttp._classify("tiktok", adhttp.Response(200, {}, {"code": 40105, "message": "bad"}), [])
err_rate, wait = adhttp._classify("tiktok", adhttp.Response(200, {}, {"code": 40100, "message": "slow"}), [])
check(isinstance(err_tok, adhttp.TokenInvalid) and isinstance(err_rate, adhttp.RateLimited) and wait,
      "adhttp: 40105 reads as Reconnect, 40100 as wait")
for t in ("Light it once and smell it all week.", "Hand poured soy, 50 hours of burn", "Book a deep clean for Saturday"):
    check(not adpolicy.lint_text(t, platform="tiktok"), f"clean on TikTok: {t!r}")

# ================================================================ 7. disconnect and secrets
print("disconnect and secrets")
owner.post(f"/app/settings/ad-accounts/{CONN_ID}/disconnect", data={"csrf": csrf(owner)})
row = q("SELECT * FROM ad_connections WHERE id=?", CONN_ID)[0]
check(row["status"] == "revoked" and row["token_enc"] is None and TOKEN not in F.tokens
      and "removed Campaignify's access on TikTok" in flashes(owner), "Disconnect revokes the token at TikTok and deletes it")
check([p["external_pixel_id"] for p in q("SELECT * FROM pixels WHERE workspace_id=? AND platform='tiktok'", WS)]
      == ["CEMBERTESTPX0000002"], "the connection's pixel goes; the hand-added pixel stays")
secrets = [TOKEN, APP_SECRET, "auth-good"]
logs = "\n".join(CAP.lines)
check(not any(s in logs for s in secrets), "no access token, app secret or auth code in any log line")
raw = open(db.DB_PATH, "rb").read()
check(not any(s.encode() in raw for s in secrets), "nor in the database file")
check(not any(s in spage + html(owner.get("/app/settings/ad-accounts")) for s in secrets), "nor in any page")

print(f"\nall {passed} checks passed")
