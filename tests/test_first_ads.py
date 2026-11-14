"""The guided first run and the first ads on us (first_ads.py, onboarding.first, the brand brief before the plan
step), on a throwaway database. Every vendor and the product page are faked; nothing leaves the machine.

  * the order: two questions, the brand brief (open before a plan; its paid action is not), the plan step, the first
    ads; a workspace with no plan is sent from the first ads to the plan step
  * the gift: the ads are made through Studio and cost the workspace nothing (its plan credits and its pack credits
    are where they were; the ledger shows the grant and the ads); once only, per workspace and per owner email; two
    requests at once can not both take it; a start that is refused keeps no slot and no credits
  * a failed ad is refunded to the member like any other
  * what a member may leave out, a script too long for the video, and a skipped step that stays on offer
  * off unless plans only is on

    python tests/test_first_ads.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_firstads_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  STRIPE_SECRET_KEY="sk_test_fa", STRIPE_WEBHOOK_SECRET="whsec_fa", PLANS_ONLY="1", UI_V2="1",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "CLAIMS_CHECK",
          "ANTHROPIC_API_KEY", "GROQ_API_KEY", "MCP_SERVER", "STRIPE_SETUP"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import first_ads  # noqa: E402
import hf  # noqa: E402
import plan_gate  # noqa: E402
import plans  # noqa: E402
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
_ip = [0]
SCRIPT = "Ember Candle. A soy wax candle with amber and cedar."


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


def signup(email):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    return c


def answer_questions(c):
    c.post("/app/welcome", data={"csrf": csrf(c), "goals": ["image", "ugc"]})
    return c.post("/app/welcome/profile", data={"csrf": csrf(c), "first_name": "Kay", "last_name": "Lee",
                                               "company": "Hearth", "website": ""})


def read_link(c):
    return c.post("/app/welcome/brand", data={"csrf": csrf(c), "do": "import", "url": "hearth.example/p/ember"})


def on_plan(email, code="starter"):
    """Put the workspace on a plan the way a started trial leaves it (plan_gate.start_trial: the plan in its trial,
    with the trial's credits in the plan bucket)."""
    import time
    with db.connect() as conn:
        wid = user(email)["workspace_id"]
        conn.execute("UPDATE workspaces SET plan=?, plan_status='trialing' WHERE id=?", (code, wid))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        assert plan_gate.start_trial(conn, ws, f"sub_test_{wid}", code, int(time.time()) + 3 * 86400) > 0
        conn.commit()


def ready(email, code="starter"):
    """A new account that answered the questions, had its link read, checked the brief and picked a plan."""
    c = signup(email)
    answer_questions(c)
    read_link(c)
    c.post("/app/welcome/brand", data={"csrf": csrf(c), "do": "done"})
    on_plan(email, code)
    return c


def buckets(email):
    w = ws_of(email)
    return w["plan_credits"], w["credits"]


def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def gifts(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM first_gifts WHERE workspace_id=? ORDER BY kind, slot",
                            (user(email)["workspace_id"],)).fetchall()


def ledger(email, reason=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id",
                            (user(email)["workspace_id"],)).fetchall()
    return [r for r in rows if reason is None or r["reason"] == reason]


def open_now(email):
    with appmod.app.test_request_context("/app"), db.connect() as conn:
        return first_ads.open_for(conn, ws_of(email), user(email))


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def form(c, **kw):
    """The step's form as the page sends it: everything ticked, the drafts as they are, unless said otherwise."""
    d = {"csrf": csrf(c), "do": "make", "image0": "1", "image1": "1", "talk": "1", "script": SCRIPT,
         "presenter": studio_v2.roster()[0]["id"]}
    d.update(kw)
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------- the order of the steps
print("== the order: questions, brand, plan, first ads")
check(plan_gate.on() and first_ads.TALK_SECONDS == 8 and first_ads.words_max() > 10, "plans only is on; the video is 8 seconds")
a = signup("a@example.com")
r = a.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome"), "a new account starts at the first question")
r = answer_questions(a)
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/brand"), "after the questions: the brand brief")
r = a.get("/app/welcome/brand")
check(r.status_code == 200 and "Start with your link" in r.get_data(as_text=True), "the brief opens before a plan is picked")
r = a.get("/app/desk")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/plan"), "the rest of the app still waits for a plan")
r = read_link(a)
check(r.status_code == 302 and ws_of("a@example.com")["product_id"], "the link is read before a plan, at no cost")
check(not ledger("a@example.com"), "nothing was charged for it")
body = page(a, "/app/welcome/brand")
check("Here is what I understood" in body and "Ember Candle" in body and "Looks right. Make my free ads" in body,
      "the brief reads the brand back and leads to the free first ads (the link was read: test_first_result.py)")
check(f"Your first {first_ads.FREE_IMAGES} image ads are free, made from this page before you pick a plan" in body,
      "it says the first image ads are free before a plan")
check("Skip for now: keep what was read" in body, "and the brief can be skipped, keeping what was read")
check("Suggest five" not in body, "the brief's paid step is not offered before a plan")
before = buckets("a@example.com")
a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "angles"})
check(buckets("a@example.com") == before and not ledger("a@example.com"), "and posting it anyway spends nothing")
r = a.post("/app/welcome/brand", data={"csrf": csrf(a), "do": "done"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/first"), "the brief hands over to the first ads")
r = a.get("/app/welcome/first")
check(r.status_code == 200 and f"Your first {first_ads.FREE_IMAGES} ads are free." in htmllib.unescape(r.get_data(as_text=True)),
      "with no plan and a product link, the first ads step offers the free first ads")
check(a.get("/app/desk").headers.get("Location", "").endswith("/app/plan"), "everything else still waits for a plan")
check(f"Your first {first_ads.FREE_IMAGES} image ads are free, before you pick a plan" in page(a, "/app/plan"),
      "the plan step says the first image ads are free before it")

on_plan("a@example.com")
r = a.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/first"), "with a plan, a new account lands on its first ads")
body = page(a, "/app/welcome/first")
check("Your first ads are on us." in body and "Image ad 1" in body and "Image ad 2" in body and "Talking video, 8 seconds" in body,
      "the step shows two image ads and the talking video")
check("Ember Candle" in body and "Make my first ads" in body and "Skip for now" in body, "drafted for the product, with a way out")
check('name="headline0"' in body and 'name="script"' in body and 'name="presenter"' in body, "every draft can be edited")
check("A soy wax candle with amber and cedar." in body, "the script starts from the product's own description")

# ---------------------------------------------------------------- the gift
print("== the gift")
start = buckets("a@example.com")
r = a.post("/app/welcome/first", data=form(a, script="word " * 40))
check(r.status_code == 200 and "fits about" in htmllib.unescape(r.get_data(as_text=True)) and not jobs_of("a@example.com"),
      "a script too long for the video is refused before anything starts")
check(buckets("a@example.com") == start and not gifts("a@example.com") and not ledger("a@example.com", first_ads.REASON),
      "and nothing was granted or kept")
check("word word word" in r.get_data(as_text=True), "what was typed is still in the box")
r = a.post("/app/welcome/first", data=form(a, image0=None, image1=None, talk=None))
check(r.status_code == 200 and "Tick at least one" in r.get_data(as_text=True), "nothing ticked: it says so")

r = a.post("/app/welcome/first", data=form(a))
check(r.status_code == 302 and "/app/desk/c/" in r.headers["Location"], "making them opens the campaign on the desk " + str(r.headers.get("Location")))
js = jobs_of("a@example.com")
check(sorted(j["tool"] for j in js) == ["ad_image", "ad_image", "ugc_video"], "two image ads and one talking video were started")
total = sum(j["credits"] for j in js)
check(total > 0 and all(j["credits"] > 0 for j in js), f"each is priced and recorded as usual ({total} credits)")
check(buckets("a@example.com") == start, "the workspace's credits are exactly where they were, in both buckets")
led = ledger("a@example.com", first_ads.REASON)
check(len(led) == 1 and led[0]["delta"] == total, "the ledger shows one grant, for exactly what the ads cost")
gs = gifts("a@example.com")
check([(x["kind"], x["slot"]) for x in gs] == [("image", 1), ("image", 2), ("talk", 1)] and all(x["job_id"] for x in gs),
      "each gifted ad holds its slot and its job")
check(sum(x["credits"] for x in gs) == total and gs[0]["email_norm"] == "a@example.com", "with its cost and the owner's email")
talk = next(j for j in js if j["tool"] == "ugc_video")
check('"duration": 8' in talk["params_json"], "the talking video is the 8-second one")
check(len({j["template_id"] or j["params_json"] for j in js if j["tool"] == "ad_image"}) == 2, "the two image ads are two different looks")
body = page(a, r.headers["Location"])
check("Your first campaign" in body and "2 image ads and 1 talking video" in body, "the campaign names what is being made")
check("On us: these did not use your plan's credits." in body, "and says it was on us")

print("== once only")
r = a.post("/app/welcome/first", data=form(a))
check(r.status_code == 200 and len(jobs_of("a@example.com")) == 3 and buckets("a@example.com") == start,
      "a second go makes nothing and grants nothing")
body = page(a, "/app/welcome/first")
check("Your first ads are made." in body and "Open the campaign" in body and "/app/desk/c/" in body,
      "the step now points at the campaign")
r = a.get("/app")
check(r.status_code == 200, "and Home no longer sends you to it")

print("== a failed ad is refunded like any other")
with db.connect() as conn:
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (talk["id"],)).fetchone()
    db.refund_job(conn, j, "refund: test")
    conn.commit()
check(sum(buckets("a@example.com")) == sum(start) + talk["credits"], "its credits come back to the member, to make it again")
check(len(gifts("a@example.com")) == 3, "the slot stays used: the credits are the second try")

# ---------------------------------------------------------------- leaving parts out, skipping
print("== leaving parts out, and skipping")
b = ready("b@example.com", "growth")
start_b = buckets("b@example.com")
r = b.post("/app/welcome/first", data={"csrf": csrf(b), "do": "skip"})
check(r.status_code == 302 and not gifts("b@example.com"), "skipping uses nothing")
r = b.get("/app")
check(r.status_code == 200, "and Home stops sending you to the step")
check("Your first ads are on us." in page(b, "/app/welcome/first"), "the step still offers them")
r = b.post("/app/welcome/first", data=form(b, talk=None, script=None, headline0="Light it when the day ends"))
check(r.status_code == 302 and [j["tool"] for j in jobs_of("b@example.com")] == ["ad_image", "ad_image"],
      "only the image ads were ticked: only they are made")
check(buckets("b@example.com") == start_b, "on us")
check(any("Light it when the day ends" in (j["brief"] or "") + j["params_json"] for j in jobs_of("b@example.com")),
      "with the headline the member wrote")
with db.connect() as conn:
    check(first_ads.left(conn, user("b@example.com")["workspace_id"]) == {"image": 0, "talk": 1}, "the talking video is still on us")
body = page(b, "/app/welcome/first")
check("Talking video, 8 seconds" in body and "Image ad 1" not in body, "and the step now offers just that")
r = b.post("/app/welcome/first", data=form(b, image0=None, image1=None))
check(r.status_code == 302 and jobs_of("b@example.com")[-1]["tool"] == "ugc_video" and buckets("b@example.com") == start_b,
      "it is made later, still on us")
check(not open_now("b@example.com"), "then the offer is closed")

# ---------------------------------------------------------------- a start that is refused
print("== a start that is refused")
c = ready("c@example.com")
start_c = buckets("c@example.com")
real = studio_v2.generate_parts


def refuse(parts, d, title, extra):
    from flask import jsonify
    return jsonify({"error": "Generation is paused for a moment."}), 503


studio_v2.generate_parts = refuse
r = c.post("/app/welcome/first", data=form(c))
studio_v2.generate_parts = real
check(r.status_code == 200 and "Generation is paused" in r.get_data(as_text=True), "the refusal is shown")
check(not jobs_of("c@example.com") and not gifts("c@example.com"), "no ad, and no slot is kept")
check(buckets("c@example.com") == start_c, "what was granted was taken back")
led = ledger("c@example.com", first_ads.REASON)
check(len(led) == 2 and led[0]["delta"] == -led[1]["delta"], "the ledger shows the grant and its return")
r = c.post("/app/welcome/first", data=form(c))
check(r.status_code == 302 and len(jobs_of("c@example.com")) == 3 and buckets("c@example.com") == start_c,
      "a second try then works, on us")

print("== two requests at once")
d = ready("d@example.com")
start_d = buckets("d@example.com")
with db.connect() as conn:          # the other request got there first and holds every slot
    taken = first_ads._claim(conn, ws_of("d@example.com"), ["image", "image", "talk"], "d@example.com")
check(len(taken) == 3, "one request claims the slots")
with db.connect() as conn:
    check(first_ads._claim(conn, ws_of("d@example.com"), ["image"], "d@example.com") == [], "a second one gets none")
r = d.post("/app/welcome/first", data=form(d))
check(not jobs_of("d@example.com") and buckets("d@example.com") == start_d and not ledger("d@example.com", first_ads.REASON),
      "and makes nothing, grants nothing")

print("== one person, one gift")
with db.connect() as conn:
    conn.execute("INSERT INTO first_gifts (workspace_id, kind, slot, job_id, credits, email_norm, created_at) VALUES (?,?,?,?,?,?,?)",
                 (987654, "image", 1, 1, 12, "f@example.com", db.now()))
    conn.commit()
f = ready("f@example.com")
check(not open_now("f@example.com"), "an owner who had the gift in another workspace is not offered it again")
body = page(f, "/app/welcome/first")
check("Make my first ads" not in body and "not available here" in body, "and the step does not offer it")
r = f.post("/app/welcome/first", data=form(f))
check(not jobs_of("f@example.com"), "posting anyway makes nothing")

print("== first means first")
h = ready("h@example.com")
start_h = buckets("h@example.com")
r = h.post("/app/studio/quote", json={"mode": "image", "product": ws_of("h@example.com")["product_id"], "text": "A headline",
                                       "count": 1}, headers={"X-CSRF": csrf(h)})
total_h = r.get_json()["total"]
r = h.post("/app/studio/generate", json={"mode": "image", "product": ws_of("h@example.com")["product_id"], "text": "A headline",
                                          "count": 1, "total": total_h}, headers={"X-CSRF": csrf(h)})
check(r.status_code == 200 and sum(buckets("h@example.com")) == sum(start_h) - total_h,
      "an image ad made in Studio is charged at the price shown")
with db.connect() as conn:
    check(first_ads.left(conn, user("h@example.com")["workspace_id"], user("h@example.com")) == {"image": 0, "talk": 1},
          "after it the image ads are no longer a first; the talking video still is")
body = page(h, "/app/welcome/first")
check("Talking video, 8 seconds" in body and "Image ad 1" not in body, "the step offers only the video")
r = h.post("/app/welcome/first", data=form(h))
check(r.status_code == 302 and [j["tool"] for j in jobs_of("h@example.com")] == ["ad_image", "ugc_video"],
      "ticked image ads that are not on us are not made")
check(sum(buckets("h@example.com")) == sum(start_h) - total_h, "and the video cost nothing")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='h@example.com'")
    conn.commit()
    check(first_ads.left(conn, user("h@example.com")["workspace_id"], user("h@example.com")) == {"image": 2, "talk": 0},
          "a site admin can still look at the step on an account that has made ads (each slot once)")

print("== the agent on the desk")
k = signup("k@example.com")
with db.connect() as conn:
    import onboarding
    onboarding.finish(conn, user("k@example.com"))
    conn.execute("UPDATE users SET name='Kay Lee' WHERE email='k@example.com'")
    conn.commit()
on_plan("k@example.com")
body = page(k, "/app/desk")
check('data-guide="brand"' in body and "Hi Kay." in body and "Read my website" in body and "data-ask-form" not in body,
      "no product yet: the agent asks for the website, and there is no box to plan from")
answer_questions(k)
read_link(k)
body = page(k, "/app/desk")
check('data-guide="brief"' in body and "Check my brand" in body, "the link was read: the agent asks you to check the brief")
k.post("/app/welcome/brand", data={"csrf": csrf(k), "do": "done"})
k.post("/app/welcome/first", data={"csrf": csrf(k), "do": "skip"})
body = page(k, "/app/desk")
check('data-guide="first"' in body and "Your first 2 image ads and a short talking video are on us." in body
      and "Review my first ads" in body and "/app/welcome/first" in body, "then it offers the first ads, on us")
check(body.index('data-guide="first"') < body.index("data-ask-form") < body.index("Start from an example"),
      "the agent speaks above the box; the examples sit below it")
k.post("/app/welcome/first", data=form(k))
body = page(k, "/app/desk")
check('data-guide="making"' in body and "3 ads are being made" in body and "Your campaigns" in body,
      "while they are made it says so and points at the campaign")
check(body.index("Your campaigns") < body.index("Start from an example"), "your campaigns lead the page")
check("2 image ads and 1 talking video · Being made" in body, "the campaign card shows what is being made")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET status='done' WHERE workspace_id=?", (user("k@example.com")["workspace_id"],))
    conn.commit()
body = page(k, "/app/desk")
check('data-guide="next"' in body and "What's next for Ember Candle?" in body, "when they are done it asks what is next")
check("2 image ads and 1 talking video · Ready" in body, "and the card says they are ready")
check(body.count('class="dk-chip" data-try="') >= 3, "with example asks one tap away")

# ---------------------------------------------------------------- off
print("== off without plans only")
os.environ["PLANS_ONLY"] = "0"
with appmod.app.test_request_context("/app"):
    check(not plan_gate.on() and not first_ads.on(), "plans only off: the gift is off")
g = signup("g@example.com")
r = g.get("/app/welcome/first")
check(r.status_code == 302 and "/welcome/first" not in r.headers["Location"], "there is no first-ads step")
r = g.post("/app/welcome/first", data=form(g))
check(r.status_code == 302 and not jobs_of("g@example.com") and not gifts("g@example.com"), "and nothing is given")
answer_questions(g)
read_link(g)
check("Looks right. Open Studio" in page(g, "/app/welcome/brand"), "the brief ends as it did before")
os.environ["PLANS_ONLY"] = "1"

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nALL OK ({passed} checks)")
