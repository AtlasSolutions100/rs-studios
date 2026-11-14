"""A launch from Claude end to end: prepare_launch -> launch_campaign -> the saga -> get_launch_status, through the
MCP, on a throwaway database with Meta faked by tests/fakegraph.py (no request leaves the machine).

    python tests/test_mcp_launch_flow.py

Covers: the approval card the MCP returns is the Launch page's card (same lines, PAUSED, AI disclosure); a stale
spec hash creates nothing; the approved launch runs the same saga and leaves every Meta object PAUSED;
get_launch_status reports it with the Ads Manager link; approving the same card again makes no second launch.
"""
import atexit
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_mcp_launch_flow_")
atexit.register(shutil.rmtree, TMP, True)
KEY = base64.b64encode(b"M" * 32).decode()
TOKEN = "EAAmcpLAUNCHtoken987654321"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=KEY, MCP_SERVER="on",
                  META_APP_ID="1000000000000001", META_APP_SECRET="fake-secret-mcp", META_LOGIN_CONFIG_ID="424242424242")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "AD_RATE_TIER", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL",
          "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MCP_ALLOWED_ORIGINS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
import launches  # noqa: E402
from adplatforms import meta  # noqa: E402
from fakegraph import FakeGraph  # noqa: E402

app = appmod.app
app.testing = True
T = [time.time()]
adhttp.CLOCK = launches.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.77.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, plan="growth"):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active' WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def make_key(c, scope):
    r = c.post("/app/mcp", data={"csrf": csrf(c), "action": "create", "name": f"k {scope}", "scope": scope})
    return re.search(r"(cfy_[A-Za-z0-9_-]{40,})", r.get_data(as_text=True)).group(1)


anon = app.test_client()
N = [0]


def call(key, name, args=None):
    N[0] += 1
    r = anon.post("/mcp", json={"jsonrpc": "2.0", "id": N[0], "method": "tools/call",
                                "params": {"name": name, "arguments": args or {}}},
                  headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {key}"})
    res = r.get_json()["result"]
    return res, (json.loads(res["content"][0]["text"]) if not res.get("isError") else res["content"][0]["text"])


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------- a Growth workspace with a ready plan, 2 ads, Meta connected
from PIL import Image  # noqa: E402

owner, u = signup("mcp-launch-owner@example.com")
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
                {"name": "Primary text", "text": "Light it once. Smell it all week."}, {"name": "Headline", "text": "Ember Candle"}]},
            {"angle": "Lasts the month", "ok": True, "compliance": OKC, "fields": [
                {"name": "Primary text", "text": "One candle, a whole month of evenings."}, {"name": "Headline", "text": "Ember Candle"}]}]}],
        "final": {"summary": "Image and video first.", "budget_split": [{"platform": "meta", "share_percent": 100}],
                  "checklist": [{"title": "Set up the Meta pixel", "platform": "meta"}]}}
BRIEF = {"offer": "Ember Candle", "goal": "sales", "audience": "Renters 25-40", "platforms": ["meta"],
         "link": "https://embercandle.example/shop", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14, "budget": "$40 a day for 14 days ($560 total)"}
with db.connect() as conn:
    PLAN_ID = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (WS, u["id"], "Ember Candle", json.dumps(BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
    for tool, kind, path, angle, params in (("ad_image", "image", IMG, "Evening ritual", {}),
                                            ("ugc_video", "video", VID, "Lasts the month", {"ad_type": "testimonial"})):
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (WS, u["id"], tool, "b", json.dumps(params), "done", 43, db.now(), db.now())).lastrowid
        conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, kind, "", path))
        conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                     "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (PLAN_ID, WS, angle, "meta", tool, "b", 43, "queued", jid, db.now()))
    conn_id = conn.execute(
        "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
        "page_id, page_name, instagram_actor_id, instagram_username, external_user_id, token_enc, scopes, status, "
        "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (WS, "meta", "111000111", "Ember Shop", "USD", "America/Chicago", "500000001", "Ember Candle Co",
         "17841400000000001", "embercandle", "10200000000000001", adplatforms.seal_token(WS, TOKEN),
         " ".join(meta.SCOPES), "active", u["id"], db.now(), db.now())).lastrowid
    conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                 "VALUES (?,?,?,?,?,?)", (WS, "meta", conn_id, "700000002", "Ember site pixel", db.now()))
    conn.commit()
G = FakeGraph(account="act_111000111")
adhttp.TRANSPORT = G
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES=str(WS))
wkey = make_key(owner, "write")

print("== list_ad_connections")
_, d = call(wkey, "list_ad_connections")
meta_row = next(p for p in d["platforms"] if p["platform"] == "meta")
check(d["launching"] and meta_row["connected"] and meta_row["account"]["id"] == "111000111",
      "Meta connected: Ember Shop, act_111000111")

print("== prepare_launch")
res, card = call(wkey, "prepare_launch", {"campaign_id": PLAN_ID, "platform": "meta"})
check(not res.get("isError"), f"prepare_launch returns a card ({res if res.get('isError') else 'ok'})")
row = q("SELECT * FROM approval_cards WHERE id=?", card["card_id"])[0]
after = json.loads(row["after_json"])
check(card["spec_hash"] == row["spec_hash"] and card["what_will_be_created"] == launches.card_text(after),
      "the MCP card is the Launch page's stored card, word for word")
check("PAUSED" in card["what_will_be_created"] and "Nothing runs until you turn it on." in card["what_will_be_created"],
      "the card says everything is created PAUSED")
check(card["ai_disclosure"].startswith("AI disclosure:"), "the card carries the AI disclosure")
check(card["can_launch"] and not [i for i in card["issues"] if i["level"] == "block"], "nothing blocks this launch")
check(G.calls == [], "preparing calls nothing on Meta")

print("== launch_campaign")
res, msg = call(wkey, "launch_campaign", {"campaign_id": PLAN_ID, "platform": "meta", "card_id": card["card_id"],
                                          "confirm_spec_hash": "0" * 64})
check(res.get("isError") and not q("SELECT * FROM launches"), "a stale spec hash is refused and nothing is launched")
res, out = call(wkey, "launch_campaign", {"campaign_id": PLAN_ID, "platform": "meta", "card_id": card["card_id"],
                                          "confirm_spec_hash": card["spec_hash"]})
check(not res.get("isError") and out["paused"] and out["launch_id"], f"the owner's approval starts a launch ({out})")
LID = out["launch_id"]
L = q("SELECT * FROM launches WHERE id=?", LID)[0]
check(L["status"] == "queued" and L["approved_by"] == u["id"] and L["card_id"] == card["card_id"],
      "a queued launch from that card, approved by the owner")
res, again = call(wkey, "launch_campaign", {"campaign_id": PLAN_ID, "platform": "meta", "card_id": card["card_id"],
                                            "confirm_spec_hash": card["spec_hash"]})
check(len(q("SELECT * FROM launches")) == 1, "approving the same card again makes no second launch")

print("== the saga, then get_launch_status")
for _ in range(60):
    launches.run(LID)
    if q("SELECT status FROM launches WHERE id=?", LID)[0]["status"] in launches.FINISHED:
        break
    T[0] += 61
st = q("SELECT status FROM launches WHERE id=?", LID)[0]["status"]
check(st == "paused", f"the launch finishes with the campaign PAUSED ({st})")
objs = q("SELECT * FROM ad_objects WHERE launch_id=?", LID)
live = [o for o in objs if o["kind"] in ("campaign", "adset", "ad")]
check(live and all(o["status"] == "PAUSED" for o in live), f"every campaign, ad set and ad on Meta is PAUSED ({[(o['kind'], o['status']) for o in objs]})")
_, s = call(wkey, "get_launch_status", {"launch_id": LID})
check(s["status"] == "paused" and s["ads_manager_url"] and "111000111" in s["ads_manager_url"],
      "get_launch_status: paused, with the Ads Manager link")

print("== a member who isn't the owner")
with db.connect() as conn:
    conn.execute("UPDATE users SET role='member' WHERE id=?", (u["id"],))
    conn.commit()
res, msg = call(wkey, "launch_campaign", {"campaign_id": PLAN_ID, "platform": "meta", "card_id": card["card_id"],
                                          "confirm_spec_hash": card["spec_hash"]})
check(res.get("isError") and "owner" in str(msg).lower(), "only the workspace owner can approve a launch")

print(f"\n{passed} checks passed")
