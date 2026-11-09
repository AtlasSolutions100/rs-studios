"""Launch kit on the plan page (launch_kit.py, RS_LAUNCH_KIT; roadmap M5.3, UX-4, UX-10), on a throwaway database:

  * off (the default): the plan page keeps the old browser-only "Launch checklist"; the tick route is 404
  * on: one block per platform in the plan, with the finished ads and export links at that platform's sizes, the
    copy with Copy buttons, goal / budget / audience lines, the pixel and "Create the campaign" steps from the hub,
    disclose-and-label, and a link to the setup guide; no localStorage ticks
  * on: ticks are saved on the server and seen from a second login (another device); the hub's steps are the same
    rows as on the hub page, both ways; only the plan's own items can be ticked; another workspace can't
  * on: the hub lists the plan's platforms first; deleting the plan removes its ticks

    python tests/test_launch_kit.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_launchkit_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
os.environ.pop("RS_LAUNCH_KIT", None)

import adhub  # noqa: E402
import app as appmod  # noqa: E402
import db  # noqa: E402

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
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.11.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def login(email, pw="longpassword"):
    c = client()
    r = c.post("/login", data={"csrf": csrf(c), "email": email, "password": pw})
    assert r.status_code == 302, r.status_code
    return c


def signup(email, plan="starter"):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


c, u = signup("owner@example.com")
ws_id = u["workspace_id"]
img = os.path.join(TMP, "ad.png")
from PIL import Image  # noqa: E402
Image.new("RGB", (864, 1080), (200, 120, 40)).save(img)

PLAN = {
    "angles": [{"name": "Evening ritual", "insight": "x", "audience": "Renters 25-40"}],
    "copy": [{"platform": "meta", "kind": "ad", "angle": "all", "variants": [
        {"angle": "Evening ritual", "ok": True, "compliance": {"ok": True, "warnings": [], "blocks": []},
         "fields": [{"name": "Primary text", "text": "Light it once. Smell it all week."},
                    {"name": "Headline", "text": "Ember Candle, 50 hours"}]},
        {"angle": "Lasts the month", "ok": True, "compliance": {"ok": True, "warnings": [], "blocks": []},
         "fields": [{"name": "Primary text", "text": "One candle, fifty hours."}]}]}],
    "final": {"summary": "Image ads first.", "budget_split": [{"platform": "meta", "share_percent": 60, "note": "Image ads first"},
                                                              {"platform": "google", "share_percent": 40}],
              "checklist": [{"title": "Set up the Meta pixel", "detail": "Purchase event.", "platform": "meta"},
                            {"title": "Upload the 2 image ads", "platform": "meta"},
                            {"title": "Add the search keywords", "platform": "google"},
                            {"title": "Write down the test dates", "platform": "all"}]}}
with db.connect() as conn:
    cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (ws_id, u["id"], "Ember Candle", json.dumps({"offer": "Ember Candle", "goal": "sales",
                                                                    "budget": "$600 for two weeks",
                                                                    "audience": "Renters 25-40 who work from home",
                                                                    "platforms": ["meta", "google"]}),
                        "ready", json.dumps(PLAN), "[]", db.now(), db.now())).lastrowid
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (ws_id, u["id"], "ad_image", "b", "{}", "done", 43,
                                                                 db.now(), db.now())).lastrowid
    oid = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, "image", "", img)).lastrowid
    conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                 "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (cid, ws_id, "Evening ritual", "meta", "ad_image", "b", 43, "queued", jid, db.now()))
    conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                 "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, ws_id, "Lasts the month", "google", "ad_image", "b", 43, "proposed", db.now()))
    conn.commit()

print("== off (the default): the old checklist")
html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
check("<h2>Launch checklist</h2>" in html and "Ticks are saved in this browser only." in html and "data-launch-kit" not in html,
      "the plan page keeps the browser-only checklist")
check(c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "lib"}).status_code == 404,
      "the tick route is off")
check(c.get("/app/ad-accounts").get_data(as_text=True).index("Facebook &amp; Instagram") <
      c.get("/app/ad-accounts").get_data(as_text=True).index("TikTok") and "In your plan" not in
      c.get("/app/ad-accounts").get_data(as_text=True), "the hub keeps its order")

os.environ["RS_LAUNCH_KIT"] = "1"
print("== on: one screen holds the launch")
html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
kit = html.split('id="launch"', 1)[1].split("</section>", 1)[0]
check("<h2>Launch kit</h2>" in kit and "Launch checklist" not in html and 'class="checklist"' not in html
      and "this browser only" not in html, "the Launch kit replaces the checklist (no localStorage ticks)")
check('id="kit-meta"' in kit and 'id="kit-google"' in kit and kit.index('id="kit-meta"') < kit.index('id="kit-google"'),
      "one block per platform in the plan, in the brief's order")
meta = kit.split('id="kit-meta"', 1)[1].split('id="kit-google"', 1)[0]
for code, label, w, h in adhub.PLATFORMS["meta"]["exports"]:
    check(f'href="/app/export/{oid}/meta/{code}"' in meta and label.replace("&", "&amp;") in meta and f"{w} x {h}" in meta,
          f"the Meta ad exports at {label} ({w} x {h})")
check(f'/media/{oid}?download=1' in meta, "and the original download")
check('data-copy="lk-meta-1-1"' in meta and "Light it once. Smell it all week." in meta and "Ember Candle, 50 hours" in meta
      and "1 more variant under Copy for each platform." in meta, "the Meta copy, each field with a Copy button")
mt = text(meta)
check("Goal Sales. Pick the objective in Ads Manager that matches it." in mt
      and "Budget 60% of $600 for two weeks. Image ads first." in mt and "Audience Renters 25-40 who work from home" in mt,
      "goal, budget share and audience as plain lines")
pix = adhub.PLATFORMS["meta"]["steps"].index(next(s for s in adhub.PLATFORMS["meta"]["steps"] if "Pixel" in s[0]))
create = [s[0] for s in adhub.PLATFORMS["meta"]["steps"]].index("Create the campaign")
check(f'value="hub:meta:{pix}"' in meta and f'value="hub:meta:{create}"' in meta and 'value="disc:meta"' in meta
      and 'value="c1"' not in meta and 'value="c2"' in meta and "Set up the Meta pixel" in meta
      and meta.count(f'value="hub:meta:{pix}"') == 1,
      "steps: the plan's Meta items, the hub's pixel and create steps, disclose; the plan's pixel item is the hub's step")
check('href="#kit-meta-ads">The ads →</a>' in meta, "'Upload the 2 image ads' links to the 2 ads in this block, not a generic guide")
check(f'href="/app/ad-accounts/meta#s{pix}">Setup guide →</a>' in meta and 'href="https://www.facebook.com/adsmanager"' in meta,
      "the pixel steps link to the hub step; Ads Manager opens from the block")
google = kit.split('id="kit-google"', 1)[1]
check('href="/app/ad-accounts/youtube"' in google and 'value="c3"' in google and "40% of $600 for two weeks" in text(google)
      and "No finished ads for Google yet." in text(google) and 'href="#ads"' in google,
      "Google: the YouTube & Google Ads guide, its item, its share, and a pointer to the ads still to make")
check('value="c4"' in kit.split('id="kit-meta"', 1)[0] and 'value="lib"' in kit.split('id="kit-meta"', 1)[0],
      "items for all platforms and 'Download the finished ads' sit above the blocks")
check("—" not in text(kit) and "–" not in text(kit), "no em or en dashes in the kit")
r = c.get(f"/app/export/{oid}/meta/feed")
check(r.status_code == 200 and r.headers.get("Content-Disposition", "").startswith("attachment"), "an export link downloads")

print("== on: ticks live on the server")
r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "c2"})
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/campaigns/{cid}#launch"), "a tick without JS goes back to the kit")
r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": f"hub:meta:{pix}"},
           headers={"Accept": "application/json", "X-Requested-With": "fetch"})
check(r.status_code == 200 and r.get_json() == {"ok": True, "item": f"hub:meta:{pix}", "done": True}, "a tick from the page's fetch answers JSON")
other = login("owner@example.com")
html2 = other.get(f"/app/campaigns/{cid}").get_data(as_text=True)
check(re.search(r'<li class="lk-item done" id="lk-c2">', html2) and re.search(rf'<li class="lk-item done" id="lk-hub-meta-{pix}">', html2)
      and "2 of " in text(html2.split('data-lk-count>', 1)[1][:30]), "a second login (another device) sees both ticks")
hub = other.get("/app/ad-accounts/meta").get_data(as_text=True)
check(re.search(rf'<li id="s{pix}" class="done">', hub), "the hub page shows the pixel step done: one tick, one place")
other.post(f"/app/ad-accounts/meta/steps/{create}", data={"csrf": csrf(other)})
html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
check(re.search(rf'<li class="lk-item done" id="lk-hub-meta-{create}">', html), "a step ticked on the hub shows ticked in the kit")
r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "c2"},
           headers={"Accept": "application/json", "X-Requested-With": "fetch"})
check(r.get_json()["done"] is False, "ticking again unticks")
print("== on: a tick sends the state it wants (re-audit L-11)")
J = {"Accept": "application/json", "X-Requested-With": "fetch"}
html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
check('name="item" value="c2"><input type="hidden" name="done" value="1">' in html,
      "the kit's form for an open item asks for done=1")
for _ in range(2):
    r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "c2", "done": "1"}, headers=J)
    check(r.get_json()["done"] is True, "done=1 sent twice (a retry or a double click) stays ticked")
html = c.get(f"/app/campaigns/{cid}").get_data(as_text=True)
check('name="item" value="c2"><input type="hidden" name="done" value="0">' in html, "a ticked item's form asks for done=0")
for _ in range(2):
    r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "c2", "done": "0"}, headers=J)
    check(r.get_json()["done"] is False, "done=0 sent twice stays unticked")
for _ in range(2):
    r = c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": f"hub:meta:{create}", "done": "1"}, headers=J)
    check(r.get_json()["done"] is True, "a hub step with done=1 twice stays ticked")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM adhub_progress WHERE workspace_id=? AND platform='meta' AND step=?",
                       (ws_id, create)).fetchone()[0] == 1, "one row, no duplicate")
check("f.querySelector('[name=done]').value = d.done ? '0' : '1';" in html, "the page flips the form's next state after each answer")
for bad in ("hub:meta:99", "hub:tiktok:0", "c9", "anything", ""):
    check(c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": bad}).status_code == 404,
          f"only this plan's items can be ticked ({bad or 'empty'!r} is 404)")
check(c.post(f"/app/campaigns/{cid}/launch", data={"item": "lib"}).status_code in (400, 403), "no CSRF token, refused")
x, xu = signup("stranger@example.com")
check(x.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(x), "item": "lib"}).status_code == 404,
      "another workspace can't tick this plan")
f, fu = signup("free@example.com", plan=None)
check(f.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(f), "item": "lib"}).status_code == 402, "Free: the upgrade wall")

print("== on: the hub lists the plan's platforms first")
hubi = c.get("/app/ad-accounts").get_data(as_text=True)
cards = re.findall(r'href="/app/ad-accounts/(\w+)"', hubi)
check(cards[:2] == ["meta", "youtube"] and hubi.count("In your plan") == 2, "Meta, then YouTube & Google Ads, marked 'In your plan'")

print("== on: deleting the plan removes its ticks")
c.post(f"/app/campaigns/{cid}/launch", data={"csrf": csrf(c), "item": "lib"})
c.post(f"/app/campaigns/{cid}/delete", data={"csrf": csrf(c)})
with db.connect() as conn:
    left = conn.execute("SELECT COUNT(*) FROM launch_ticks WHERE campaign_id=?", (cid,)).fetchone()[0]
    hubrows = conn.execute("SELECT COUNT(*) FROM adhub_progress WHERE workspace_id=?", (ws_id,)).fetchone()[0]
check(left == 0 and hubrows == 2, "the plan's ticks go with it; the hub's account steps stay")

os.environ.pop("RS_LAUNCH_KIT", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
