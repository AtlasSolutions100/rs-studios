"""The next-step thread on Home (next_steps.py, RS_NEXT_STEPS; roadmap M5.1, UX-1, UX-2, COPY-2), on a throwaway database:

  * off (the default): Home, the first-ads card and the upgrade wall are as before
  * on: the 2 free ads are counted as 2 ads ("Making your 2 ads.", "Your 2 ads are ready.", 2 thumbnails on the card)
  * on: Home shows "Your campaign" (Ads · Plan · Page · Launch) with one primary button; after the free ads it is
    "Plan the campaign for {product}", and the first-ads card's third next step is the same
  * on: one confirm-email message on Home after the first ads (the inline box), the banner elsewhere
  * on: the upgrade wall's replacement copy (03b section 5), its button goes to the plans
  * on: the strip moves Plan -> Page -> Launch -> done as the plan, the page and the hub's "Create the campaign" tick land

    python tests/test_next_steps.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_nextsteps_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
os.environ.pop("RS_NEXT_STEPS", None)
os.environ.pop("SIGNUP_CREDITS_ADS", None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=64, h=80):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


results = {}


def fake_submit(model, inputs, webhook_url=None, idempotency_key=None):
    rid = f"req{len(results) + 1}"
    n = int((inputs or {}).get("batch_size") or 1)
    results[rid] = {"status": "completed", "images": [{"url": f"https://cdn.example/{rid}-{i}.png"} for i in range(n)]}
    return rid


class FakeResp:
    status_code = 200

    def __init__(self):
        self.content = png().getvalue()

    def raise_for_status(self):
        pass


hf.submit, hf.status, hf.upload = fake_submit, lambda rid: results[rid], lambda data, ctype: "https://files.example/1.png"
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = lambda url, timeout=0: FakeResp()
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def ws_of(email):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()


def first_ads(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    r = c.post("/app/start", data={"csrf": csrf(c), "kind": "image", "product_name": "Ember Candle", "template": "hero",
                                   "photo": (png(400, 400), "ember.png")}, content_type="multipart/form-data")
    assert r.status_code == 302 and "/app?made=" in r.headers["Location"], (r.status_code, r.headers.get("Location"))
    return c, r.headers["Location"]


def strip(html):
    m = re.search(r'<section class="panel nsteps".*?</section>', html, re.S)
    return m.group(0) if m else ""


def primaries(fragment):
    """Accent buttons (class btn without dark) in an HTML fragment."""
    return [m for m in re.findall(r'class="btn( [^"]*)?"', fragment) if "dark" not in (m or "")]


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


print("== off (the default): nothing changes")
c, made = first_ads("off@example.com")
home = c.get(made).get_data(as_text=True)
check("data-next-steps" not in home and "Making 2 of 2." in home and "data-live-done" not in home,
      "Home after the first submit: no strip, the old 'Making 2 of 2.' line")
check("Make more like this" in home and "Plan the campaign for" not in home, "the first-ads card keeps 'Make more like this'")
check("data-verify-banner" in home and "Confirm your email to unlock video ads" in home,
      "off: the banner and the inline box both show (as before)")
wall = c.get("/app/campaigns")
wh = wall.get_data(as_text=True)
check(wall.status_code == 402 and "Included in Starter" in wh and "See plans" in wh and "data-wall" not in wh,
      "off: the planner wall is the old one")
worker.tick()
worker.tick()
home = c.get(made).get_data(as_text=True)
check("thumbs2" not in home, "off: one thumbnail per card")

os.environ["RS_NEXT_STEPS"] = "1"
print("== on: the 2 free ads, counted as 2")
c, made = first_ads("on@example.com")
ws = ws_of("on@example.com")
home = c.get(made).get_data(as_text=True)
check("Making your 2 ads." in home and "Making 1 of 1" not in home, "rendering: 'Making your 2 ads.' (outputs, not jobs)")
check(re.search(r'data-live-done hidden><b>Your 2 ads are ready\.</b> Download them', home),
      "the done line is on the page, hidden until app.js sees the last card finish")
check("data-verify-banner" not in home and "Confirm your email to unlock video ads" in home
      and 'class="btn sm dark" data-verify-open' in home,
      "one confirm-email message: the inline box (its button not a second primary), no top banner (COPY-2)")
check("data-verify-banner" in c.get("/app/library").get_data(as_text=True), "the banner still shows on other pages")
worker.tick()
worker.tick()
with db.connect() as conn:
    n_out = conn.execute("SELECT COUNT(*) FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=?",
                         (ws["id"],)).fetchone()[0]
check(n_out == 2, "the fake render made 2 outputs, one per job (two different templates)")
home = c.get(made).get_data(as_text=True)
check(re.search(r"<p class=\"small ok\"[^>]*data-live-done ><b>Your 2 ads are ready\.</b>", home), "done: 'Your 2 ads are ready.'")
check(len(set(re.findall(r'data-job="(\d+)"', home))) == 2 and "thumbs2" not in home,
      "the 2 ads are 2 cards, one thumbnail each")
jid = re.search(r'data-job="(\d+)"', home).group(1)
with db.connect() as conn:      # a job that made 2 versions (Create's "versions") still shows both on its card
    o = conn.execute("SELECT * FROM outputs WHERE job_id=?", (jid,)).fetchone()
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, o["kind"], o["url"], o["path"]))
    conn.commit()
home2 = c.get(made).get_data(as_text=True)
check(home2.count('alt="Generated ad, version') == 2 and "thumbs2" in home2, "a card with 2 versions shows 2 thumbnails")
cards = c.get(f"/app/jobs/cards?ctx=batch&ids={jid}").get_json()
check("thumbs2" in cards["cards"][jid]["html"], "the live card (app.js patch) renders the same 2 thumbnails")
with db.connect() as conn:
    conn.execute("DELETE FROM outputs WHERE id=(SELECT MAX(id) FROM outputs WHERE job_id=?)", (jid,))
    conn.commit()

print("== on: the strip after the free ads")
s = strip(home)
check(s and 'data-current="plan"' in s, "Home shows 'Your campaign' with Plan as the current step")
check([b for b in ("Ads", "Plan", "Page", "Launch") if f"<b>{b}</b>" in s] == ["Ads", "Plan", "Page", "Launch"],
      "4 steps: Ads · Plan · Page · Launch")
check("2 made" in text(s) and "1 of 4 done" in text(s), "Ads is done with '2 made'; 1 of 4 done")
check(len(primaries(s)) == 1 and 'href="/app/campaigns" data-ns-primary>Plan the campaign for Ember Candle</a>' in s,
      "one primary button: 'Plan the campaign for Ember Candle' to /app/campaigns")
check("Comes with Starter, $39 a month" in s, "on Free the price of the step is next to its button")
main = home.split('<main class="main" id="main">', 1)[1]
visible = re.sub(r"<template.*?</template>", "", main.split("<aside", 1)[0], flags=re.S)   # the details dialogs' contents
check(len(primaries(visible)) == 1, "Home after the free ads has exactly one primary button")
check('<a href="/app/campaigns"><b>Plan the campaign for Ember Candle</b>' in home and "Make more like this" not in home,
      "the first-ads card's third next step is 'Plan the campaign for Ember Candle'")
plain = c.get("/app").get_data(as_text=True)
check('data-current="plan"' in strip(plain), "plain /app (no ?made=) shows the strip too")

print("== on: the upgrade wall")
wall = c.get("/app/campaigns")
wh = wall.get_data(as_text=True)
wt = text(wh.split("data-wall", 1)[1].split("</main>", 1)[0])
check(wall.status_code == 402 and "Comes with Starter, $39 a month" in wt and "Plan the campaign for Ember Candle" in wt,
      "eyebrow 'Comes with Starter, $39 a month' and h1 'Plan the campaign for Ember Candle'")
check("Planning costs up to 150 credits; you pay what it uses." in wt and "hands you the launch checklist" in wt,
      "the lede says what the planner does and its credit cap")
check("Starter is $39 a month with 2,000 credits, plus the copy studio, landing pages, smart links, flows and the "
      "ad-account guide. Your free credits, product and ads carry over." in wt, "the plan line")
check('href="/app/billing#plans">Get Starter, $39 a month</a>' in wh and ">Back to Home</a>" in wh
      and "Cancel any month. No trial that bills you." in wt, "buttons 'Get Starter, $39 a month' and 'Back to Home'")
check("every tool on the cheaper plans" not in wh and "See plans" not in wh, "the old sentence and button are gone")
check("—" not in wt and "–" not in wt, "no em or en dashes in the wall")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (ws["id"],))
    conn.commit()
seo = c.get("/app/seo").get_data(as_text=True)
check("Comes with Growth, $99 a month" in seo and "Growth is $99 a month with 6,000 credits, plus everything in Starter"
      in seo and "Your credits, products and ads carry over." in seo and "Get Growth, $99 a month" in seo,
      "a Growth wall on Starter: the same pattern, with Growth's numbers and no 'free'")

print("== on: the strip moves through Plan, Page, Launch")
with db.connect() as conn:
    conn.execute("INSERT INTO campaigns (workspace_id, name, brief_json, status, plan_json, steps_json, created_at, "
                 "updated_at) VALUES (?,?,?,?,?,?,?,?)", (ws["id"], "Ember Candle launch", "{}", "ready", "{}", "[]",
                                                          db.now(), db.now()))
    cid = conn.execute("SELECT MAX(id) FROM campaigns").fetchone()[0]
    conn.commit()
s = strip(c.get("/app").get_data(as_text=True))
check('data-current="page"' in s and f'href="/app/campaigns/{cid}">Ember Candle launch</a>' in s
      and len(primaries(s)) == 1 and ">Build the landing page</a>" in s,
      "a finished plan: Plan done (links to it), Page current with one button 'Build the landing page'")
with db.connect() as conn:
    conn.execute("INSERT INTO pages (workspace_id, title, slug, status, blocks_json, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)", (ws["id"], "Ember", "ember-launch", "published", "[]", db.now(), db.now()))
    conn.execute("INSERT INTO adhub_progress (workspace_id, platform, step, done_by, done_at) VALUES (?,?,?,?,?)",
                 (ws["id"], "meta", 0, None, db.now()))
    conn.commit()
s = strip(c.get("/app").get_data(as_text=True))
check('data-current="launch"' in s and "/p/ember-launch" in s and len(primaries(s)) == 1
      and f'href="/app/campaigns/{cid}#launch" data-ns-primary>Open the launch checklist</a>' in s,
      "a live page: Launch current, one button to the plan's launch section; an early hub tick doesn't count")
import adhub  # noqa: E402
create_step = [t for t, _, _ in adhub.PLATFORMS["meta"]["steps"]].index("Create the campaign")
c.post(f"/app/ad-accounts/meta/steps/{create_step}", data={"csrf": csrf(c)})
s = strip(c.get("/app").get_data(as_text=True))
check('data-current="all"' in s and "4 of 4 done" in text(s) and len(primaries(s)) == 1
      and ">Plan the next campaign</a>" in s, "the hub's 'Create the campaign' tick: 4 of 4, one button 'Plan the next campaign'")
check("—" not in text(s) and "–" not in text(s), "no em or en dashes in the strip")

print("== on: the first-run form is untouched")
f = client()
f.post("/signup", data={"csrf": csrf(f), "email": "fresh@example.com", "password": "longpassword"})
fresh = f.get("/app").get_data(as_text=True)
check("data-first-run" in fresh and "data-next-steps" not in fresh, "a new account still gets only the first-ad form (no strip)")

os.environ.pop("RS_NEXT_STEPS", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
