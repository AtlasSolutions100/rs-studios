"""Instagram placements on Meta (roadmap M6.3) on a throwaway database, through the fake Graph (tests/fakegraph.py).

    python tests/test_meta_placements.py      (tests/test_app.py runs it too)

  * the Launch page offers Automatic or a choice of Facebook and Instagram Feed, Stories and Reels; the choice is kept
    in the spec and written on the approval card in plain words
  * the ad set's targeting gets exactly the chosen publisher platforms and positions (instagram_positions stream,
    story, reels; facebook_positions feed, story, facebook_reels), and every creative runs as the Page's Instagram
    account (instagram_user_id)
  * Instagram placements without an Instagram account linked to the Page block; Automatic without one warns; no
    placement ticked blocks; Stories and Reels with an ad that isn't 9:16 warn
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
TMP = tempfile.mkdtemp(prefix="rs_mplace_")
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
import adspec  # noqa: E402
import launches  # noqa: E402
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


# ---------------------------------------------------------------- a workspace with a plan, 2 finished Meta ads and a connection
from PIL import Image  # noqa: E402

owner, u = signup("mplace-owner@example.com")
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

os.environ["AD_LAUNCH"] = "on"
PLACE = {**BOTH, "landing": "https://embercandle.example/shop", "pixel": "700000002", "event": "Purchase"}


def cp(**form):
    return card_page(owner, **PLACE, **form)

# ================================================================ 1. the page and the spec
print("the placement choice")
card, page = card_page(owner)
check('name="placement_mode" value="auto"' in page and 'value="instagram_reels"' in page and "Instagram Reels" in page
      and "Instagram ads show as @embercandle." in page, "the page offers Automatic or Facebook and Instagram Feed, Stories, Reels")
check(json.loads(card["spec_json"])["placements"] == "auto", "the draft starts on automatic placements")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the page")
card, page = cp(card=card["id"], placement_mode="choose", place=["instagram_feed", "instagram_reels"])
spec = json.loads(card["spec_json"])
c1 = json.loads(card["after_json"])
check(spec["placements"] == ["instagram_feed", "instagram_reels"], "Let me choose keeps the ticked placements in the spec")
check("placements: Instagram Feed, Instagram Reels" in c1["lines"][1], "the card names them in plain words")
check(re.search(r'value="instagram_reels" checked', page) and not re.search(r'value="facebook_feed" checked', page)
      and re.search(r'value="choose" checked', page), "the page shows the choice back")
check("meta.aspect_stories" in {i["rule_id"] for i in c1["issues"] if i["level"] == "warn"},
      "Reels with an ad that isn't 9:16 warns")
card_ticks_only, _ = cp(card=card["id"], placement_mode="auto", place=["facebook_feed"])
check(json.loads(card_ticks_only["spec_json"])["placements"] == "auto", "with Automatic on, ticks don't count")
none, _ = cp(card=card["id"], placement_mode="choose")
check("meta.placements_none" in {i["rule_id"] for i in json.loads(none["after_json"])["issues"] if i["level"] == "block"},
      "Let me choose with nothing ticked blocks")

# ================================================================ 2. what Meta gets
print("the ad set's targeting")
card, _ = cp(card=card["id"], placement_mode="choose", place=["facebook_feed", "instagram_feed",
                                                                            "instagram_stories", "instagram_reels"])
check(not json.loads(card["after_json"])["blocking"], "with the Instagram account linked nothing blocks")
r, lid = approve(owner, card)
check(drive(lid) == "paused", "the launch runs to paused")
def posted(edge):
    return [c["data"] for c in G.calls if c["method"] == "POST" and c["path"].endswith(f"/{edge}")]


def as_json(v):
    return json.loads(v) if isinstance(v, str) else v


t = as_json(posted("adsets")[-1]["targeting"])
check(t["publisher_platforms"] == ["facebook", "instagram"] and t["facebook_positions"] == ["feed"]
      and t["instagram_positions"] == ["stream", "story", "reels"],
      "targeting: Facebook feed; Instagram stream, story and reels, nothing else")
oss = [as_json(d["object_story_spec"]) for d in posted("adcreatives")]
check(oss and all(o.get("instagram_user_id") == "17841400000000001" for o in oss),
      "every creative runs as the Page's Instagram account (instagram_user_id)")
st = meta.targeting(adspec.CampaignSpec(objective="sales", platform="meta", name="x", daily_budget_minor=100,
                                        currency="USD", geo={"countries": ["US"]}, placements=["facebook_reels"]))
check(st["publisher_platforms"] == ["facebook"] and st["facebook_positions"] == ["facebook_reels"]
      and "instagram_positions" not in st, "Facebook Reels alone: only facebook_reels")
check("publisher_platforms" not in meta.targeting(adspec.CampaignSpec(
    objective="sales", platform="meta", name="x", daily_budget_minor=100, currency="USD", geo={"countries": ["US"]})),
    "Automatic sends no placement fields: Advantage+ placements")

# ================================================================ 3. no Instagram account on the Page
print("no Instagram account")
with db.connect() as conn:
    conn.execute("UPDATE ad_connections SET instagram_actor_id=NULL, instagram_username=NULL WHERE id=?", (CONN_ID,))
    conn.commit()
card, page = cp(card=card["id"], placement_mode="choose", place=["instagram_reels"])
iss = {i["rule_id"]: i["level"] for i in json.loads(card["after_json"])["issues"]}
check(iss.get("launch.no_instagram") == "block" and "Link it on Meta, then reconnect" in page,
      "Instagram placements without an Instagram account linked to the Page block")
card, _ = cp(card=card["id"], placement_mode="choose", place=["facebook_feed", "facebook_stories"])
iss = {i["rule_id"]: i["level"] for i in json.loads(card["after_json"])["issues"]}
check("launch.no_instagram" not in iss and "launch.auto_no_instagram" not in iss, "Facebook placements alone are fine")
card, _ = cp(card=card["id"], placement_mode="auto")
iss = {i["rule_id"]: i["level"] for i in json.loads(card["after_json"])["issues"]}
check(iss.get("launch.auto_no_instagram") == "warn", "Automatic without an Instagram account warns")

print(f"\nall {passed} checks passed")
