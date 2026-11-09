"""Manage a launched campaign (roadmap M2.4): the Campaigns page, turning a campaign or an ad on or off, the daily
budget, and the approval card every change goes through. Meta is the fake Graph in tests/fakegraph.py (answers shaped
like tests/fixtures/meta/*.json, hand-written from Meta's v26.0 reference): no request leaves the machine.

    python tests/test_manage.py      (tests/test_app.py runs it too)

  * AD_LAUNCH off / beta (allowlist) / on: the page, the nav item, the worker
  * the card shows exactly what changes (each object, from what to what), read from Meta first; a member drafts,
    only the owner approves; who and when stay on the card; a card that is no longer true on Meta is refused
  * turning on: ads, then the ad set, then the campaign; an ad paused from Campaignify stays paused; AI ads need the
    AI label confirmed; pausing touches the campaign only
  * the budget: D3 cap in the ad account's currency (USD, EUR, JPY), re-checked at approval; +50% in 24 hours needs a
    second confirm; D2 paid plan for turning on and raising, never for pausing; a billing hold
  * the 30 s edit lock and the rate budget make a change wait, and the worker finishes it
  * every recorded error code: plain words, wait or fail, 190 marks the connection for Reconnect, what is left on Meta
  * another workspace's campaign and card are 404; no token in any log line or row
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
TMP = tempfile.mkdtemp(prefix="rs_manage_")
atexit.register(shutil.rmtree, TMP, True)
KEY = base64.b64encode(b"M" * 32).decode()
APP_SECRET = "fake-app-secret-manage-2424"
TOKEN = "EAAmanageTOKENsecret987654321"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=KEY, AD_RATE_TIER="full",
                  META_APP_ID="1000000000000001", META_APP_SECRET=APP_SECRET, META_LOGIN_CONFIG_ID="424242424242")
for k in ("AD_LAUNCH", "AD_LAUNCH_WORKSPACES", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
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

T = [time.time()]
adhttp.CLOCK = launches.CLOCK = admanage.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def later(s=31.0):
    T[0] += s


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.52.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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

owner, u = signup("manage-owner@example.com")
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


manage_routes.DRAFTS_PER_HOUR = 100000          # this file drafts a lot; the limit itself is checked at the end
LID, MAIN_G = launched("Ember Candle")
O = objs(LID)

# ================================================================ 1. the flag
print("AD_LAUNCH")
check(owner.get("/app/ad-campaigns").status_code == 404, "AD_LAUNCH unset: the Campaigns page is a 404")
check(owner.get(f"/app/ad-campaigns/{LID}").status_code == 404, "and a campaign's page")
check(change(owner, LID, action="on").status_code == 404 and card_of(LID) is None, "and a change: no card is made")
home = html(owner.get("/app"))
check("/app/ad-campaigns" not in home, "AD_LAUNCH unset: no Ad campaigns item in the nav")
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES="999999")
check(owner.get("/app/ad-campaigns").status_code == 404, "beta, not on the allowlist: 404")
os.environ["AD_LAUNCH_WORKSPACES"] = str(WS)
page = html(owner.get("/app/ad-campaigns"))
check(owner.get("/app/ad-campaigns").status_code == 200 and "Ad campaigns" in page and 'href="/app/ad-campaigns"' in page,
      "beta, on the allowlist: the page opens and the nav has Ad campaigns")

# ================================================================ 2. the Campaigns page
print("Campaigns page")
row = page.split(f'href="/app/ad-campaigns/{LID}"', 1)[1].split("</li>", 1)[0]
check("Ember Candle" in row and "Paused" in row and "$40 a day" in row and "Ads Manager" in row,
      "the launched campaign is listed: name, Paused, $40 a day, Manage and Ads Manager")
check("From the plan Ember Candle" in text(page) and "2 ads" in text(page), "with its plan and its 2 ads")
check(not re.search(r"[–—]", text(page)), "no em or en dashes on the Campaigns page")
G.calls.clear()
dpage = html(owner.get(f"/app/ad-campaigns/{LID}"))
reads = [c for c in G.calls if c["method"] == "GET" and c["params"].get("ids")]
check(len(reads) == 2 and "daily_budget" not in [c for c in reads if O["ads"][0] in c["params"]["ids"]][0]["params"]["fields"],
      "opening a campaign reads it from Meta: campaign and ad set in one call, the ads in another (no budget field on ads)")
check(all(c["headers"].get("Authorization") == f"Bearer {TOKEN}" and "appsecret_proof" in c["params"]
          and "access_token" not in c["params"] for c in reads), "reads carry the token in the header with appsecret_proof")
check(q("SELECT synced_at FROM ad_objects WHERE external_id=?", O["campaign"])[0]["synced_at"] is not None
      and q("SELECT daily_budget_minor FROM ad_objects WHERE external_id=?", O["campaign"])[0]["daily_budget_minor"] == 4000,
      "what Meta said is stored on ad_objects (status, budget, when)")
check("Turn on campaign" in dpage and "$40 a day" in dpage and "AI presenter or voice" in dpage and "AI-made media" in dpage,
      "the campaign page: Turn on, the budget, and which ads use AI")
G.calls.clear()
owner.get(f"/app/ad-campaigns/{LID}")
check(not G.calls, "opening it again within 2 minutes doesn't call Meta again")
other_lid_launch = q("SELECT status FROM launches WHERE id=?", LID)[0]["status"]
check(other_lid_launch == "paused", "managing never changes the launch's own status")

# ================================================================ 3. turning on: a member drafts, the owner approves
print("turn on")
member, mu = signup("manage-member@example.com")
ex("UPDATE users SET workspace_id=?, role='member' WHERE id=?", WS, mu["id"])
G.edits.clear()
r = change(member, LID, action="on", target="campaign")
card = card_of(LID)
a = after(card)
check(r.status_code == 302 and card["kind"] == "status_change" and card["decision"] is None and card["created_by"] == mu["id"],
      "a member drafts a turn-on card (undecided, drafted by the member)")
check(a["lines"] == ["Ad 1, Evening ritual. From Paused to On.", "Ad 2, Lasts the month. From Paused to On.",
                     "The ad set. From Paused to On.", "The campaign. From Paused to On."],
      "the card has one line per object that changes: Ad 1, <name>. From Paused to On.")
names = [c["kind"] for c in a["changes"]]
check(names == ["ad", "ad", "adset", "campaign"] and all(c["before"] == "PAUSED" and c["after"] == "ACTIVE" for c in a["changes"]),
      "turning on changes the 2 ads, then the ad set, then the campaign, each from PAUSED to ACTIVE")
check(a["title"] == "Turn on Ember Candle", "card title: Turn on <campaign>")
check("up to $40 a day for this campaign" in a["notes"][0], "the card says what it spends: up to $40 a day")
check(a["ai"] and a["ai_text"].startswith("Ad 2 has an AI presenter or AI voice; ad 1 is AI-made media.")
      and "tick Meta's AI label on those ads in Ads Manager" in a["ai_text"], "the card reminds to tick Meta's AI label")
check(not G.edits, "drafting changes nothing on Meta")
mpage = html(member.get(f"/app/ad-campaigns/{LID}"))
check("Only the workspace owner can approve" in mpage and 'disabled aria-describedby="mc-own"' in mpage,
      "the member sees the card with Approve off")
check(approve(member, card, confirm_ai="1").status_code == 403 and fresh(card["id"])["decision"] is None and not G.edits,
      "a member's approve is refused (403); nothing changes")
lpage = html(owner.get("/app/ad-campaigns"))
check("Waiting for approval: Turn on Ember Candle" in text(lpage), "the Campaigns page shows the card waiting for approval")
opage = html(owner.get(f"/app/ad-campaigns/{LID}"))
check("Approve and change on Meta" in opage and 'name="confirm_ai"' in opage and "Asked for by manage-member@example.com" in opage,
      "the owner sees the card, the AI label box, and who asked for it")
r = approve(owner, card)
check(r.status_code == 302 and "AI label" in flashes(owner) and fresh(card["id"])["decision"] is None and not G.edits,
      "approving without ticking the AI label box is refused")
r = approve(owner, card, confirm_ai="1")
card = fresh(card["id"])
check(card["decision"] == "approved" and card["decided_by"] == u["id"] and card["apply_state"] == "done" and card["applied_at"],
      "the owner approves: decided by the owner, applied, with when")
check([e["id"] for e in G.edits] == O["ads"] + [O["adset"], O["campaign"]] and all(e["status"] == "ACTIVE" for e in G.edits),
      "Meta got exactly the card's changes, in its order: ads, ad set, campaign")
check(all(G.store[i]["status"] == "ACTIVE" for i in O["ads"] + [O["adset"], O["campaign"]]), "everything is on on Meta")
check({r_["status"] for r_ in q("SELECT status FROM ad_objects WHERE launch_id=? AND kind IN ('campaign','adset','ad')", LID)}
      == {"ACTIVE"}, "and in ad_objects")
hist = html(owner.get(f"/app/ad-campaigns/{LID}"))
check("Approved by manage-owner@example.com" in hist and "Changed on Meta" in hist and "Pause campaign" in hist,
      "the change shows who asked, who approved and when; the page now offers Pause")
check(change(owner, LID, action="on").status_code == 302 and "already on" in (after(card_of(LID))["issues"][0]["message"]),
      "turning on what is on: the card says it's already on and can't be approved")

# ================================================================ 4. pausing, ad toggles, a card that is no longer true
print("pause and ads")
later()
G.edits.clear()
change(owner, LID, action="off")
card = card_of(LID)
check([c["id"] for c in after(card)["changes"]] == [O["campaign"]] and after(card)["lines"][0].endswith("From On to Paused."),
      "pausing changes the campaign only")
check(not after(card)["ai"], "pausing needs no AI label box")
approve(owner, card)
check(fresh(card["id"])["apply_state"] == "done" and G.store[O["campaign"]]["status"] == "PAUSED"
      and G.store[O["ads"][0]]["status"] == "ACTIVE" and G.store[O["ads"][0]]["effective_status"] == "CAMPAIGN_PAUSED",
      "the campaign is paused on Meta; its ads stay on and wait for it")
later()
change(owner, LID, action="off", target=O["ads"][0])
card = card_of(LID)
check([c["id"] for c in after(card)["changes"]] == [O["ads"][0]] and after(card)["title"] == "Pause ad 1 of Ember Candle",
      "pausing one ad: one change, titled with the ad")
approve(owner, card)
check(G.store[O["ads"][0]]["status"] == "PAUSED", "ad 1 is paused")
later()
change(owner, LID, action="on")
card = card_of(LID)
check([c["id"] for c in after(card)["changes"]] == [O["campaign"]],
      "turning the campaign on again leaves ad 1 as the member set it (paused)")
check(after(card)["ai"] and after(card)["ai_text"].startswith("Ad 2 has an AI presenter"), "the AI reminder names the ad that runs")
G.store[O["campaign"]]["status"] = "ACTIVE"         # someone turned it on in Ads Manager meanwhile
r = approve(owner, card, confirm_ai="1")
check(fresh(card["id"])["decision"] is None and "changed on Meta since this card was made" in flashes(owner),
      "a card that is no longer true on Meta is refused at approval")
G.store[O["campaign"]]["status"] = "PAUSED"
later()
change(member, LID, action="on", target=O["ads"][0])
card = card_of(LID)
check("manage.campaign_paused" in warns(card), "turning on an ad of a paused campaign warns it waits for the campaign")
r = member.post(f"/app/ad-cards/{card['id']}/discard", data={"csrf": csrf(member)})
check(fresh(card["id"])["decision"] == "rejected" and fresh(card["id"])["decided_by"] == mu["id"],
      "who drafted a card can drop it (recorded as dropped by them)")
change(owner, LID, action="on", target=O["ads"][0])
first = card_of(LID)
change(owner, LID, action="off", target=O["ads"][1])
check(fresh(first["id"])["decision"] == "replaced", "a new card replaces the campaign's undecided one")
check(change(owner, LID, action="on", target="99999").status_code == 302 and "isn't part of this campaign" in flashes(owner),
      "an ad that isn't the campaign's is refused")

# ================================================================ 5. the daily budget: D3 in the account's currency, +50%
print("budget")
later()
change(owner, LID, action="budget", budget="55")
card = card_of(LID)
a = after(card)
check(card["kind"] == "budget_change" and a["changes"] == [dict(a["changes"][0], before=4000, after=5500)]
      and a["lines"] == ["The daily budget. From $40 to $55 a day."], "a budget card: from $40 to $55 a day")
check(not blocks(card) and "manage.big_raise" not in warns(card), "$40 to $55 is within the plan and under +50%")
approve(owner, card)
check(G.store[O["campaign"]]["daily_budget"] == 5500 and G.edits[-1] == {"id": O["campaign"], "daily_budget": "5500"},
      "Meta got the new daily budget in cents")
check(q("SELECT daily_budget_minor FROM ad_objects WHERE external_id=?", O["campaign"])[0]["daily_budget_minor"] == 5500,
      "and ad_objects has it")
later()
change(owner, LID, action="budget", budget="61")
card = card_of(LID)
check("manage.big_raise" in warns(card) and "$40 a day" in [i for i in after(card)["issues"]
                                                            if i["rule_id"] == "manage.big_raise"][0]["message"],
      "$55 to $61 is over 1.5 times the budget of 24 hours ago ($40): flagged")
check('name="confirm_big"' in html(owner.get(f"/app/ad-campaigns/{LID}")), "the card asks for the second confirmation")
approve(owner, card)
check(fresh(card["id"])["decision"] is None and "more than half" in flashes(owner), "without it the approval is refused")
approve(owner, card, confirm_big="1")
check(fresh(card["id"])["apply_state"] == "done" and G.store[O["campaign"]]["daily_budget"] == 6100, "with it the raise is made")
later()
change(owner, LID, action="budget", budget="1500")
check("manage.budget_ceiling" in blocks(card_of(LID)) and "Your Growth plan runs campaigns up to $1,000 a day" in
      after(card_of(LID))["issues"][[i["rule_id"] for i in after(card_of(LID))["issues"]].index("manage.budget_ceiling")]["message"],
      "over the Growth cap ($1,000 a day): blocked, in dollars")
change(owner, LID, action="budget", budget="0.5")
check("manage.budget_min" in blocks(card_of(LID)), "under Meta's minimum: blocked")
change(owner, LID, action="budget", budget="61")
check("manage.no_change" in blocks(card_of(LID)), "the same budget: nothing to change")
change(owner, LID, action="budget", budget="abc")
check("as a number" in flashes(owner), "a budget that isn't a number is refused")
change(owner, LID, action="budget", budget="40")
card = card_of(LID)
check(not blocks(card) and not warns(card), "lowering the budget: no block, no warning")
ex("UPDATE workspaces SET plan='starter' WHERE id=?", WS)
change(owner, LID, action="budget", budget="250")
check("Your Starter plan runs campaigns up to $200 a day" in json.dumps(after(card_of(LID))["issues"]), "the Starter cap is $200 a day")
ex("UPDATE workspaces SET plan='growth' WHERE id=?", WS)
change(owner, LID, action="budget", budget="900")
card = card_of(LID)
check(not blocks(card), "$900 is under the Growth cap")
ex("UPDATE workspaces SET plan='starter' WHERE id=?", WS)
approve(owner, card, confirm_big="1")
check(fresh(card["id"])["decision"] is None and "Starter plan runs campaigns up to $200" in flashes(owner)
      and G.store[O["campaign"]]["daily_budget"] == 6100, "the cap is re-checked at approval: a downgrade in between refuses it")
ex("UPDATE workspaces SET plan='growth' WHERE id=?", WS)
# the cap in the ad account's currency
ex("UPDATE ad_connections SET currency='EUR' WHERE id=?", CONN_ID)
change(owner, LID, action="budget", budget="1000")
check("up to €920 a day" in json.dumps(after(card_of(LID))["issues"], ensure_ascii=False) or
      "920" in json.dumps(after(card_of(LID))["issues"]), "an EUR account: the Growth cap converts to 920 EUR")
change(owner, LID, action="budget", budget="900")
check(not blocks(card_of(LID)) and after(card_of(LID))["changes"][0]["after"] == 90000, "900 EUR is under it, in cents")
ex("UPDATE ad_connections SET currency='JPY' WHERE id=?", CONN_ID)
change(owner, LID, action="budget", budget="160000")
check("manage.budget_ceiling" in blocks(card_of(LID)) and after(card_of(LID))["changes"][0]["after"] == 160000,
      "a JPY account: whole yen (zero-decimal), the Growth cap is 150,000 yen")
ex("UPDATE ad_connections SET currency='USD' WHERE id=?", CONN_ID)
# a budget raised on Meta past the cap blocks turning on from Campaignify
G.store[O["campaign"]]["daily_budget"] = 250000
change(owner, LID, action="on")
check("manage.budget_ceiling" in blocks(card_of(LID)) and "$2,500 a day" in json.dumps(after(card_of(LID))["issues"]),
      "a budget raised in Ads Manager past the cap blocks turning on from Campaignify")
G.store[O["campaign"]]["daily_budget"] = 6100
del G.store[O["campaign"]]["daily_budget"]
change(owner, LID, action="on")
check("manage.budget_unknown" in blocks(card_of(LID)), "no daily campaign budget on Meta (moved to ad sets): can't check, blocked")
G.store[O["campaign"]]["daily_budget"] = 6100

# ================================================================ 6. D2 paid plan and the billing hold
print("paid plan")
ex("UPDATE workspaces SET plan='free', plan_status=NULL, comp_plan='growth' WHERE id=?", WS)
change(owner, LID, action="on")
check("manage.paid_plan" in blocks(card_of(LID)), "a comped workspace (no paid subscription) can't turn on from Campaignify")
change(owner, LID, action="budget", budget="70")
check("manage.paid_plan" in blocks(card_of(LID)), "or raise the budget")
change(owner, LID, action="budget", budget="50")
check(not blocks(card_of(LID)), "but can lower it")
later()
G.store[O["campaign"]]["status"] = "ACTIVE"
change(owner, LID, action="off")
card = card_of(LID)
approve(owner, card)
check(not blocks(card) and fresh(card["id"])["apply_state"] == "done" and G.store[O["campaign"]]["status"] == "PAUSED",
      "and can always pause")
ex("UPDATE workspaces SET plan='growth', plan_status='active', comp_plan=NULL WHERE id=?", WS)
ex("UPDATE workspaces SET billing_hold=1 WHERE id=?", WS)
change(owner, LID, action="on")
check("manage.billing_hold" in blocks(card_of(LID)), "a workspace on a billing hold can't turn on")
ex("UPDATE workspaces SET billing_hold=0 WHERE id=?", WS)

# ================================================================ 7. the edit lock, the rate budget, the worker
print("waits")
later()
adhttp.edit_lock("meta", O["campaign"])          # someone changed the campaign a second ago
change(owner, LID, action="budget", budget="45")
card = card_of(LID)
approve(owner, card)
card = fresh(card["id"])
check(card["apply_state"] == "waiting" and card["next_at"] and G.store[O["campaign"]]["daily_budget"] != 4500,
      "the campaign's 30 s edit slot is taken: the change waits instead of blocking the page")
check("one change per item every 30" in flashes(owner), "the owner is told it finishes in a moment")
wpage = html(owner.get(f"/app/ad-campaigns/{LID}"))
check("Waiting for Meta" in wpage and "data-mc-busy" in wpage, "the page shows it waiting and polls")
check(owner.get(f"/app/ad-campaigns/{LID}.json").get_json()["busy"], "the poll says busy")
change(owner, LID, action="on")
check("still being made" in flashes(owner), "no new change while one is being made")
check(admanage.tick() == 0, "the worker leaves it until its slot is free")
later()
check(admanage.tick() == 1 and fresh(card["id"])["apply_state"] == "done" and G.store[O["campaign"]]["daily_budget"] == 4500,
      "after 30 s the worker finishes it")
os.environ["AD_RATE_TIER"] = "limited"
change(owner, LID, action="budget", budget="41")
card = card_of(LID)
ex("INSERT OR REPLACE INTO api_budget (platform, account, points, at, blocked_until) VALUES (?,?,?,?,0)",
   "meta", "act_111000111", 51.5, T[0])      # the 2 reads of the approval fit; the write (3 points) doesn't
approve(owner, card)
check(fresh(card["id"])["apply_state"] == "waiting", "the ad account's rate budget nearly spent: the change waits")
os.environ["AD_LAUNCH"] = "off"
later(120)
check(admanage.tick() == 0 and fresh(card["id"])["apply_state"] == "waiting", "AD_LAUNCH off: the worker doesn't apply it")
os.environ["AD_LAUNCH"] = "beta"
check(admanage.tick() == 1 and fresh(card["id"])["apply_state"] == "done", "flag back on and points decayed: done")
os.environ["AD_RATE_TIER"] = "full"
later()
change(owner, LID, action="budget", budget="42")
card = card_of(LID)
G.fail("POST", "object", "meta/error_17.json", times=1000)
approve(owner, card)
for _ in range(130):
    later(70)
    admanage.tick()
    if fresh(card["id"])["apply_state"] == "failed":
        break
check(fresh(card["id"])["apply_state"] == "failed" and "slow down for over 2 hours" in fresh(card["id"])["error"],
      "a change Meta keeps refusing with slow down for over 2 hours fails in plain words")
G.failures.clear()

# ================================================================ 8. every recorded error code
print("error codes")
CASES = [("meta/error_190.json", "failed", "Reconnect it in Settings"),
         ("meta/error_200.json", "failed", "missing a Meta permission"),
         ("meta/error_10_permission.json", "failed", "missing a Meta permission"),
         ("meta/error_1885183_dev_mode.json", "failed", "switched to Live"),
         ("meta/error_368_policy.json", "failed", "against its advertising policies"),
         ("meta/error_2635_version.json", "failed", "API version"),
         ("meta/error_100_invalid.json", "failed", "Meta said:"),
         ("meta/error_100_budget_low.json", "failed", "Meta said: The minimum daily budget"),
         ("meta/error_17.json", "done", None), ("meta/error_edit_lock.json", "done", None),
         ("meta/error_80004_rate.json", "done", None), ("meta/error_2.json", "done", None),
         ("meta/error_1_unknown.json", "done", None)]
for i, (fx, want, words) in enumerate(CASES):
    lid, g = launched(f"Err {i}")
    o = objs(lid)
    change(owner, lid, action="on")
    card = card_of(lid)
    g.fail("POST", o["campaign"], fx)        # the campaign, the last change of a turn-on
    approve(owner, card, confirm_ai="1")
    for _ in range(8):
        if fresh(card["id"])["apply_state"] in ("done", "failed"):
            break
        later(61)
        admanage.tick()
    c_ = fresh(card["id"])
    ok = c_["apply_state"] == want and (words is None or words in (c_["error"] or ""))
    ok = ok and (g.store[o["campaign"]]["status"] == ("ACTIVE" if want == "done" else "PAUSED"))
    check(ok, f"{fx.split('/')[1]}: " + (f"fails with \"{words}\"" if words else "waits or retries, then the change is made"))
    if want == "failed":
        check("Nothing started: the campaign is still paused." in c_["error"] and
              any(ch.get("error") for ch in after(c_)["changes"]), "  and says nothing started; Meta's words kept for the page")
    if fx.endswith("190.json"):
        check(q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"] == "expired",
              "190 marks the connection for Reconnect")
        ex("UPDATE ad_connections SET status='active', last_error=NULL WHERE id=?", CONN_ID)
    later()
import requests  # noqa: E402
lid, g = launched("Network down")
o = objs(lid)
change(owner, lid, action="off", target=o["ads"][0])
card = card_of(lid)
change(owner, lid, action="on", target=o["ads"][0])
card = card_of(lid)
g.fail("POST", "object", requests.ConnectionError(f"https://graph.facebook.com/v26.0/{o['ads'][0]}?access_token={TOKEN}"),
       times=10)
approve(owner, card, confirm_ai="1")
for _ in range(6):
    later(61)
    admanage.tick()
c_ = fresh(card["id"])
check(c_["apply_state"] == "failed" and c_["error"].startswith(launches.NETWORK_WORDS) and TOKEN not in json.dumps(dict(c_)),
      f"a network that stays down: {admanage.NETWORK_TRIES} tries, then plain words, no token on the card")
g.failures.clear()
# Meta's reads fail: drafting refuses in plain words, the page shows what we last saw
g.fail("GET", "ids", "meta/error_2.json", times=5)
r = change(owner, lid, action="off")
check(card_of(lid)["id"] == card["id"] and "Meta" in flashes(owner), "Meta unreachable while drafting: no card, plain words")
g.failures.clear()
g.fail("GET", "ids", "meta/error_190.json")
r = change(owner, lid, action="off")
check("Reconnect" in flashes(owner) and q("SELECT status FROM ad_connections WHERE id=?", CONN_ID)[0]["status"] == "expired",
      "a 190 while reading marks the connection and says Reconnect")
ex("UPDATE ad_connections SET status='active', last_error=NULL WHERE id=?", CONN_ID)
# the multi-id read, from the recorded shapes
ft = FakeTransport()
ft.on("GET", "/v26.0/", lambda m, url, params, d, h: "meta/manage_read_ads.json" if "23850000000005" in params["ids"]
      else "meta/manage_read.json")
adhttp.TRANSPORT = ft
st = meta.read_objects(TOKEN, "111000111", {"campaign": ["23850000000003"], "adset": ["23850000000004"],
                                            "ad": ["23850000000005", "23850000000006", "23850000000099"]})
check(st["23850000000003"]["daily_budget"] == 4000 and st["23850000000004"]["effective_status"] == "CAMPAIGN_PAUSED"
      and st["23850000000006"]["effective_status"] == "DISAPPROVED" and "23850000000099" not in st,
      "read_objects parses Meta's multi-id answer; an id Meta doesn't return is left out")
check(all(c["params"]["fields"] == ("id,name,status,effective_status" if "23850000000005" in c["params"]["ids"]
                                    else meta.READ_FIELDS["campaign"]) for c in ft.calls), "with the fields per kind")
adhttp.TRANSPORT = g
g.store[o["ads"][1]]["review"] = "DISAPPROVED"
g.store[o["ads"][1]]["effective_status"] = "DISAPPROVED"
change(owner, lid, action="on")
check("manage.ad_disapproved" in warns(card_of(lid)) and "ad 2" in json.dumps(after(card_of(lid))["issues"]),
      "an ad Meta didn't approve: the turn-on card warns it won't run")
ex("UPDATE ad_objects SET synced_at=NULL WHERE launch_id=?", lid)
dpage = html(owner.get(f"/app/ad-campaigns/{lid}"))
check("Not approved by Meta" in dpage, "and the campaign page shows it")

# ================================================================ 9. scoping and secrets
print("scoping and secrets")
adhttp.TRANSPORT = G = MAIN_G
other, ou = signup("manage-other@example.com")
os.environ["AD_LAUNCH_WORKSPACES"] = f"{WS},{ou['workspace_id']}"
check(other.get(f"/app/ad-campaigns/{LID}").status_code == 404 and other.get(f"/app/ad-campaigns/{LID}.json").status_code == 404,
      "another workspace's campaign is a 404")
check(change(other, LID, action="off").status_code == 404, "and drafting a change on it")
any_card = card_of(LID)
check(approve(other, any_card).status_code == 404 and other.post(f"/app/ad-cards/{any_card['id']}/discard",
                                                                 data={"csrf": csrf(other)}).status_code == 404,
      "and its cards")
check("Ember Candle" not in html(other.get("/app/ad-campaigns")), "its Campaigns page lists only its own")
member2, mu2 = signup("manage-member2@example.com")
ex("UPDATE users SET workspace_id=?, role='member' WHERE id=?", WS, mu2["id"])
later()
change(owner, LID, action="off", target=O["ads"][1])
card = card_of(LID)
check(card["decision"] is None and card["created_by"] == u["id"], "the owner drafts a card")
check(member2.post(f"/app/ad-cards/{card['id']}/discard", data={"csrf": csrf(member2)}).status_code == 403,
      "another member can't drop a card someone else drafted")
with db.connect() as conn:
    GLID = conn.execute("INSERT INTO launches (workspace_id, plan_id, platform, connection_id, spec_json, status, "
                        "external_campaign_id, idempotency_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (WS, PLAN_ID, "google", CONN_ID, json.dumps({"name": "Google search test"}), "paused", "777",
                         "google-test-key", db.now(), db.now())).lastrowid
    conn.commit()
check(owner.get(f"/app/ad-campaigns/{GLID}").status_code == 404 and change(owner, GLID, action="on").status_code == 404,
      "a Google launch isn't managed here (Meta only for now)")
check("Google search test" in html(owner.get("/app/ad-campaigns")),
      "but it is listed on the Ad campaigns page with the others (M7: every platform's campaigns)")
logs = "\n".join(CAP.lines)
check(TOKEN not in logs and APP_SECRET not in logs, "no token or app secret in any log line")
dump = json.dumps([dict(r_) for t in ("approval_cards", "ad_objects", "launches") for r_ in q(f"SELECT * FROM {t}")])
check(TOKEN not in dump and APP_SECRET not in dump, "no token or app secret in any card, object or launch row")
manage_routes.APPROVALS_PER_HOUR = 1
r = approve(owner, card)
check("Too many changes" in flashes(owner) and fresh(card["id"])["decision"] is None, "approvals are rate limited per workspace")

import subprocess  # noqa: E402
env = {k: v for k, v in os.environ.items() if k != "META_SANDBOX_TOKEN"}
env["RS_DATA"] = os.path.join(TMP, "sandbox")
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "tools", "meta_sandbox_manage.py"), "--account",
                    "act_1", "--campaign", "2", "--adset", "3", "--ad", "4"], capture_output=True, text=True, env=env, timeout=60)
check(p.returncode != 0 and "Set META_SANDBOX_TOKEN" in (p.stdout + p.stderr),
      "tools/meta_sandbox_manage.py (the sandbox acceptance run) refuses to start without a token")
manage_routes.DRAFTS_PER_HOUR = 1
n_cards = len(q("SELECT id FROM approval_cards"))
change(owner, LID, action="off")
check("a lot of changes in an hour" in flashes(owner) and len(q("SELECT id FROM approval_cards")) == n_cards,
      "drafts are rate limited per workspace")

print(f"all {passed} checks passed")
