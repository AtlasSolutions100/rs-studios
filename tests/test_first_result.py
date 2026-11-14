"""The free first result before the plan step (first_ads.free_open, onboarding.first and first_result; Koby, Nov 14
2026), on a throwaway database. Every vendor and the product page are faked; nothing leaves the machine.

  * the new order for a sign-up with a product link: the two questions, confirm the email, the free first ads (the
    link is read there), the first-result screen, the plan step, then the brand brief (skippable), then what is
    still on us; a sign-up with no link keeps the old order
  * the grant: exactly what the image ads cost (FREE_IMAGES at catalog.rates()["image"]), marked on the workspace,
    once per workspace and once per person; two requests at once can not both take it; a start that is refused
    gives the mark back; the plan gate is lifted for those ads only
  * real ads only: the same Studio path as every other ad (jobs, prices, slots in first_gifts)
  * the presenter preview for sign-ups whose first touch was /ai-ugc-ad-generator or /arcads-alternative
  * the step tracking on /admin/signups: the new step, and where each account stopped

    python tests/test_first_result.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_firstresult_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  STRIPE_SECRET_KEY="sk_test_fr", STRIPE_WEBHOOK_SECRET="whsec_fr", PLANS_ONLY="1", UI_V2="1",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "CLAIMS_CHECK",
          "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MCP_SERVER", "STRIPE_SETUP", "WELCOME_CREDITS"):
    os.environ.pop(k, None)

import admin_funnel  # noqa: E402
import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import first_ads  # noqa: E402
import hf  # noqa: E402
import onboarding  # noqa: E402
import plan_gate  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=400, h=400, rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(b, "PNG")
    b.seek(0)
    return b


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Ember Candle",
"description":"A soy wax candle with amber and cedar. It burns for 40 hours.","image":["https://cdn.hearth.example/ember-1.jpg"],
"brand":{"@type":"Brand","name":"Hearth & Co"}}</script></head><body><h1>Ember Candle</h1></body></html>"""
LINK = "https://hearth.example/p/ember"
submitted = []
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: submitted.append(model) or f"req{len(submitted)}"
hf.upload = lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = no_vendor
billing._post = billing._get = no_vendor
appmod._maybe_reconcile = lambda ws_id, force=False: 0
fetcher.fetch = lambda url, accept=None: {"url": url, "status": 200, "content_type": "text/html", "text": PAGE}
fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {"url": url, "status": 200, "content_type": "image/png",
                                                             "data": png(400).getvalue()}
admin_funnel.ONBOARD_SINCE = "2000-01-01T00:00:00+00:00"
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def signup(email, link=None, landing=None):
    """A sign-up; `link` pasted on the home page first (/signup?url=), `landing` the page its ad click landed on."""
    c = client()
    if landing:
        c.set_cookie("cfy_ft", f"utm_source=google&utm_medium=cpc&lp={landing}&at=1763100000")
    if link:
        c.get("/signup?url=" + link)
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    return c


def questions(c, website=""):
    c.post("/app/welcome", data={"csrf": csrf(c), "goals": ["image", "ugc"]})
    return c.post("/app/welcome/profile", data={"csrf": csrf(c), "first_name": "Kay", "last_name": "Lee",
                                               "company": "Hearth", "website": website})


def read_here(c, url=LINK):
    return c.post("/app/welcome/first", data={"csrf": csrf(c), "do": "import", "url": url})


def make(c):
    return c.post("/app/welcome/first", data={"csrf": csrf(c), "do": "make"})


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def where(r):
    return r.headers.get("Location", "") if r.status_code in (301, 302, 303) else f"({r.status_code})"


def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def ledger(email, reason=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id",
                            (user(email)["workspace_id"],)).fetchall()
    return [r for r in rows if reason is None or r["reason"] == reason]


def steps_of(email):
    with db.connect() as conn:
        return [r["detail"] for r in conn.execute("SELECT detail FROM site_events WHERE workspace_id=? AND "
                                                  "name='onboard_step' ORDER BY id", (user(email)["workspace_id"],))]


def journey(email):
    with db.connect() as conn:
        return admin_funnel.steps(conn, 30)["per_ws"][user(email)["workspace_id"]]


def is_open(email):
    with appmod.app.test_request_context("/app"), db.connect() as conn:
        return first_ads.free_open(conn, ws_of(email), user(email))


def on_plan(email, code="starter"):
    import time
    with db.connect() as conn:
        wid = user(email)["workspace_id"]
        conn.execute("UPDATE workspaces SET plan=?, plan_status='trialing' WHERE id=?", (code, wid))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        plan_gate.start_trial(conn, ws, f"sub_test_{wid}", code, int(time.time()) + 3 * 86400)
        conn.commit()


EACH = catalog.rates()["image"]
COST = first_ads.FREE_IMAGES * EACH

# ---------------------------------------------------------------- the new order
print("== a sign-up with a pasted product link: questions, free first ads, result, plan, brief")
with appmod.app.test_request_context("/app"):
    check(plan_gate.on() and first_ads.on() and first_ads.FREE_IMAGES == 3, "plans only and the new look are on; 3 free image ads")
a = signup("a@example.com", link="hearth.example/p/ember")
check(where(a.get("/app")).endswith("/app/welcome"), "a new account starts at the first question")
r = questions(a)
check(where(r).endswith("/app/welcome/first"), "after the questions: the free first ads, not the brand brief")
check(onboarding.answers(user("a@example.com")).get("brief") == "after", "the brief waits until after them")
body = page(a, "/app/welcome/first")
check(f"Your first {first_ads.FREE_IMAGES} ads are free." in body and LINK in body and "Read my page" in body,
      "the step offers the free ads and reads the pasted link")
check(where(a.get("/app")).endswith("/app/welcome/first"), "Home sends a new account to the free first ads")
check(where(a.get("/app/desk")).endswith("/app/plan"), "the rest of the app still waits for a plan")
r = read_here(a)
check(where(r).endswith("/app/welcome/first") and ws_of("a@example.com")["product_id"], "the link is read on the step")
check(not ledger("a@example.com") and not jobs_of("a@example.com"), "reading it costs nothing and makes nothing")
body = page(a, "/app/welcome/first")
check(all(f"Image ad {i}" in body for i in range(1, first_ads.FREE_IMAGES + 1)) and "Talking video" not in body,
      "three image ads are drafted, and no video (videos come with a plan)")
check('type="checkbox" name="image' not in body and 'name="headline2"' in body, "all three are made; every headline can be changed")
check(f"{COST:,} credits of ads, on us" in body and "Make my free ads" in body,
      f"the price shown is the real one from the rates ({first_ads.FREE_IMAGES} x {EACH})")
check("You see them before you are asked to pick a plan" in body, "and it says the plan step comes after")

# ---------------------------------------------------------------- the grant, once
print("== the grant: exactly what the ads cost, once")
r = make(a)
import re as _re
check(where(r).endswith("/app/welcome/first/result"), "making them opens the first-result screen " + where(r)
      + " ".join(_re.findall(r'class="flash"[^>]*>([^<]*)', r.get_data(as_text=True))))
js = jobs_of("a@example.com")
check([j["tool"] for j in js] == ["ad_image"] * first_ads.FREE_IMAGES, "three real image ads were started, through Studio")
check(sum(j["credits"] for j in js) == COST and all(j["credits"] == EACH for j in js), "each at the image ad's price")
led = ledger("a@example.com", first_ads.REASON)
check(len(led) == 1 and led[0]["delta"] == COST, f"one grant in the ledger, of exactly {COST} credits")
w = ws_of("a@example.com")
check(w["credits"] == 0 and w["plan_credits"] == 0, "and the workspace holds nothing extra to spend")
check(w["first_free_at"] and w["first_free_credits"] == COST, "the workspace is marked, with what it was granted")
with db.connect() as conn:
    gs = conn.execute("SELECT * FROM first_gifts WHERE workspace_id=? ORDER BY slot", (w["id"],)).fetchall()
check([(x["kind"], x["slot"]) for x in gs] == [("image", 1), ("image", 2), ("image", 3)] and all(x["job_id"] for x in gs),
      "each free ad holds a slot and its job")
check(not is_open("a@example.com"), "the free first result is closed")
r = make(a)
check(len(jobs_of("a@example.com")) == first_ads.FREE_IMAGES and len(ledger("a@example.com", first_ads.REASON)) == 1,
      "a second go makes nothing and grants nothing")
check(where(r).endswith("/app/plan"), "and goes to the plan step")
with appmod.app.test_request_context("/app"):
    try:
        plan_gate.check(ws_of("a@example.com"), user("a@example.com"))
        gated = False
    except plan_gate.PlanRequired:
        gated = True
check(gated, "outside the free ads' own request the plan gate is back")

print("== the first-result screen, then the plan step")
body = page(a, "/app/welcome/first/result")
check(body.count('data-job="') == first_ads.FREE_IMAGES and 'data-live-cards="batch"' in body,
      "the result screen shows the three ads, filling in as they finish")
check("Next: pick a plan" in body and "/app/plan" in body, "then leads to the plan step")
check("data-presenter-preview" not in body, "no presenter preview for a sign-up that didn't come from the presenter pages")
check(where(a.get("/app")).endswith("/app/plan"), "after it, the app opens on the plan step")
check(where(a.get("/app/welcome/first")).endswith("/app/plan") and a.get("/app/welcome/first/result").status_code == 200,
      "the first ads step now goes to the plan step; the result screen stays open")
check("are free, before you pick a plan" not in page(a, "/app/plan"), "the plan step no longer offers free ads")

print("== after the plan: the brand brief, skippable, then what is still on us")
on_plan("a@example.com")
check(where(a.get("/app")).endswith("/app/welcome/brand"), "with a plan, the brand brief comes next")
body = page(a, "/app/welcome/brand")
check("Here is what I understood" in body and "Ember Candle" in body, "read back from the link, nothing to type")
check("Skip for now: keep what was read" in body, "and it can be skipped")
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "skip"})
check(where(r).endswith("/app/welcome/first"), "skipping it goes on to the first ads")
body = page(a, "/app/welcome/first")
check("Talking video, 8 seconds" in body and "Image ad 1" not in body, "where the talking video is still on us, and no more image ads")
check(where(a.get("/app")) == "(200)", "Home no longer sends them anywhere")

print("== the steps on /admin/signups")
st = steps_of("a@example.com")
check(st[:2] == ["intent:done", "profile:done"], "the questions are noted as before")
check("free:seen" in st and "free:done" in st and st.index("free:done") < st.index("plan:seen") < st.index("brief:seen"),
      "the free first ads are noted, before the plan step and the brief")
check("first:seen" in st and "brief:skipped" in st, "then the brief (skipped) and the first ads step")
j = journey("a@example.com")
check("done" in j["steps"]["free"] and "made_ad" in j["steps"]["free"] and "made_ad" not in j["steps"]["first"],
      "the first ad counts on the free step")
check(j["last"][0] == "first", "the last step they were on is the first ads step")
with db.connect() as conn:
    data = admin_funnel.steps(conn, 30)
row = next(s for s in data["steps"] if s["key"] == "free")
check(row["label"] == "Free first ads" and row["reached"] == 1 and row["done"] == 1, "the new step has its own row")
admin = signup("admin@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='admin@example.com'")
    conn.commit()
body = page(admin, "/admin/signups")
check("Where they stop" in body and "Free first ads" in body, "/admin/signups shows the new step")

# ---------------------------------------------------------------- stopping on the plan step after the free ads
print("== someone who saw their ads and stopped at the plan step")
b = signup("b@example.com", link="hearth.example/p/ember")
questions(b)
read_here(b)
make(b)
b.get("/app/welcome/first/result")
b.get("/app/plan")
j = journey("b@example.com")
check(j["last"][0] == "plan" and j["state"] == "saw plans, didn't pick", "the admin page says they stopped at the plan step")
with db.connect() as conn:
    data = admin_funnel.steps(conn, 30)
check(next(s for s in data["steps"] if s["key"] == "plan")["stopped"] >= 1, "and counts them there")

# ---------------------------------------------------------------- once per person
print("== one person, one free first result")
c = signup("a+second@example.com", link="hearth.example/p/ember")
r = questions(c)
check(where(r).endswith("/app/welcome/brand"), "the same person (email folded) in a new workspace gets the old order")
check(not is_open("a+second@example.com"), "and no free ads")
read_link = c.post("/app/welcome/brand", data={"csrf": csrf(c), "do": "import", "url": LINK})
check(where(c.get("/app/welcome/first")).endswith("/app/plan"), "the first ads step sends them to the plan step")
r = make(c)
check(not jobs_of("a+second@example.com") and not ledger("a+second@example.com"), "and posting anyway makes nothing")

print("== two requests at once")
d = signup("d@example.com", link="hearth.example/p/ember")
questions(d)
read_here(d)
with db.connect() as conn:      # the other request marked the workspace first
    conn.execute("UPDATE workspaces SET first_free_at=? WHERE id=?", (db.now(), user("d@example.com")["workspace_id"]))
    conn.commit()
r = make(d)
check(not jobs_of("d@example.com") and not ledger("d@example.com"), "the second one makes nothing and grants nothing")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET first_free_at=NULL WHERE id=?", (user("d@example.com")["workspace_id"],))
    conn.commit()
    wsd = conn.execute("SELECT * FROM workspaces WHERE id=?", (user("d@example.com")["workspace_id"],)).fetchone()
    with appmod.app.test_request_context("/app"):
        from flask import g
        g.user = user("d@example.com")
        g.ws = wsd
        first = first_ads.make(conn, wsd, g.user, conn.execute("SELECT * FROM products WHERE id=?", (wsd["product_id"],)).fetchone(), {})
check(first.get("ok") and len(jobs_of("d@example.com")) == first_ads.FREE_IMAGES, "with the mark free, one request makes them")
check(sum(x["delta"] for x in ledger("d@example.com", first_ads.REASON)) == COST, "granting exactly what they cost")

print("== a start that is refused gives everything back")
e = signup("e@example.com", link="hearth.example/p/ember")
questions(e)
read_here(e)
real = studio_v2.generate_parts


def refuse(parts, d, title, extra):
    from flask import jsonify
    return jsonify({"error": "Generation is paused for a moment."}), 503


studio_v2.generate_parts = refuse
r = make(e)
studio_v2.generate_parts = real
check(r.status_code == 200 and "Generation is paused" in r.get_data(as_text=True), "the refusal is shown")
w = ws_of("e@example.com")
check(not jobs_of("e@example.com") and not w["first_free_at"] and w["credits"] == 0, "no ad, no mark, no credits kept")
led = ledger("e@example.com", first_ads.REASON)
check(len(led) == 2 and led[0]["delta"] == COST and led[1]["delta"] == -COST, "the ledger shows the grant and its return")
check(is_open("e@example.com"), "so the free first result is still open")
r = make(e)
check(where(r).endswith("/app/welcome/first/result") and len(jobs_of("e@example.com")) == first_ads.FREE_IMAGES,
      "and a second try makes them")

# ---------------------------------------------------------------- no link, or a typed one
print("== no product link: the old order")
f = signup("f@example.com")
r = questions(f)
check(where(r).endswith("/app/welcome/brand"), "after the questions: the brand brief, as before")
check(onboarding.answers(user("f@example.com")).get("brief") == "due", "the brief is due now")
check(not is_open("f@example.com"), "no free ads without a link")
check(where(f.get("/app/welcome/first")).endswith("/app/plan"), "the first ads step sends them to the plan step")
r = make(f)
check(not jobs_of("f@example.com") and not ledger("f@example.com"), "nothing can be made without a plan")
r = f.post("/app/welcome/brand", data={"csrf": csrf(f), "do": "import", "url": LINK})
check(is_open("f@example.com"), "reading a link on the brief opens the free first ads")
body = page(f, "/app/welcome/brand")
check("Looks right. Make my free ads" in body, "and the brief leads to them")

print("== a website typed on the second question counts as the link")
h = signup("h@example.com")
r = questions(h, website="hearth.example/p/ember")
check(where(r).endswith("/app/welcome/first"), "the free first ads come next")
check(f'value="{LINK}"' in page(h, "/app/welcome/first"), "with the typed link ready to read")

print("== a sign-up still confirms the email before the free ads are made")
os.environ["RS_EMAIL_VERIFY"] = "1"
k = signup("k@example.com", link="hearth.example/p/ember")
r = questions(k)
check(where(r).endswith("/app/welcome/first"), "the questions lead to the free first ads")
check(where(k.get("/app/welcome/first")).endswith("/app/confirm"), "which wait for a confirmed email")
os.environ["RS_EMAIL_VERIFY"] = "0"

print("== on the welcome credits the first ads are still free")
m = signup("m@example.com", link="hearth.example/p/ember")
questions(m)
read_here(m)
with db.connect() as conn:
    granted = plan_gate.grant_welcome(conn, ws_of("m@example.com"), user("m@example.com"))
    conn.commit()
check(granted > 0 and is_open("m@example.com"), "a workspace on its welcome credits still has the free first result")
make(m)
check(ws_of("m@example.com")["credits"] == granted, "made free: the welcome credits are all still there")

# ---------------------------------------------------------------- the presenter preview
print("== the presenter preview for sign-ups from the presenter pages")
check(first_ads.PRESENTER_PAGES == ("/ai-ugc-ad-generator", "/arcads-alternative"), "the two presenter pages")
check(first_ads.came_for_presenters({"first_touch": json.dumps({"lp": "/arcads-alternative/"})}),
      "a first touch on /arcads-alternative counts")
check(not first_ads.came_for_presenters({"first_touch": json.dumps({"lp": "/"})}) and
      not first_ads.came_for_presenters({"first_touch": None}), "the home page, or no first touch, does not")
p = signup("p@example.com", link="hearth.example/p/ember", landing="/ai-ugc-ad-generator")
check(json.loads(user("p@example.com")["first_touch"] or "{}").get("lp") == "/ai-ugc-ad-generator", "the landing page is on the account")
questions(p)
read_here(p)
make(p)
body = page(p, "/app/welcome/first/result")
check("data-presenter-preview" in body and "Preview of your video: a still, not the video" in body,
      "the result screen shows the presenter preview, labelled as a preview of the video")
check("made with an AI presenter" in body and "an AI presenter, not a real person" in body, "and says it is an AI presenter")
check("Ember Candle." in body and "A soy wax candle with amber and cedar." in body, "with a line written from their product")
check("Pick a plan to make the real video" in body, "and a button to make the real video after picking a plan")
check([j["tool"] for j in jobs_of("p@example.com")] == ["ad_image"] * first_ads.FREE_IMAGES,
      "the preview made nothing: no video was started")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nALL OK ({passed} checks)")
