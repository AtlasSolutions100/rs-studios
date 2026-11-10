"""The new look, Studio and onboarding behind UI_V2 (ui_v2.py, studio_v2.py, onboarding.py; M14), on a throwaway database:

  * off (the default): Home, the card step and the public site are as before; Studio and the onboarding routes
    send people back; no v2 stylesheet
  * the preview: ?ui=v2 works only for a site admin and sets a signed, sticky cookie; a forged cookie and a member's
    ?ui=v2 turn nothing on; ?ui=v1 removes it; the Settings switch (users.ui_v2) is admin-only
  * UI_V2=1: everyone gets it; a new account answers the onboarding questions and they are stored
  * Studio never makes anything without the price that was shown: no total, or a wrong total, starts nothing and
    charges nothing; the quoted total starts the jobs and charges exactly that
  * the card step's timeline says what the billing config says (no trial, no charge date, the real plan and pack
    prices, the credits that are waiting)

    python tests/test_ui_v2.py      (tests/test_app.py runs it too)
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
TMP = tempfile.mkdtemp(prefix="rs_uiv2_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import card_on_file  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import onboarding  # noqa: E402
import plans  # noqa: E402
import studio_v2  # noqa: E402
import ui_v2  # noqa: E402
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


submitted = []


def fake_submit(model, inputs, webhook_url=None, idempotency_key=None):
    submitted.append(model)
    return f"req{len(submitted)}"


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


hf.submit, hf.upload = fake_submit, lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = no_vendor
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


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    return c


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def add_product(c, email):
    """A product with one photo, the active one (what the composer's product chip would hold)."""
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(400, 400), "ember.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                         (user(email)["workspace_id"],)).fetchone()
        assert p is not None, "the product was not made"
        if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
            import products
            path = os.path.join(TMP, f"ember-{p['id']}.png")
            open(path, "wb").write(png(400, 400).getvalue())
            products.add_photo(conn, p["workspace_id"], p["id"], path, "ember.png", "image/png")
            conn.commit()
        return p["id"]


def jobs_n(email):
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (user(email)["workspace_id"],)).fetchone()[0]


def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


V2_CSS = "ui_v2.css"

print("== off (the default): nothing changes")
check(not ui_v2.enabled(), "UI_V2 is off unless the variable is set")
c = signup("member@example.com")
home = c.get("/app").get_data(as_text=True)
check('data-ui="v2"' not in home and V2_CSS not in home and "data-cmp" not in home,
      "Home is the old Home: no v2 attribute, no v2 stylesheet, no composer")
check("data-first-run" in home, "a new account still gets the first-ad form")
r = c.get("/app/studio")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "/app/studio sends people to Home")
r = c.get("/app/welcome")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "/app/welcome sends people to Home")
r = c.get("/app/studio/brand")
check(r.status_code == 302 and "/app/products" in r.headers["Location"] or "/app/brand" in r.headers["Location"],
      "/app/studio/brand sends people to Products")
check(jpost(c, "/app/studio/quote", {"mode": "image"}).status_code == 404
      and jpost(c, "/app/studio/generate", {"mode": "image", "total": 1}).status_code == 404,
      "Studio's quote and generate routes answer 404")
pub = client().get("/").get_data(as_text=True)
check('data-ui="v2"' not in pub and V2_CSS not in pub, "the landing page is the old one")
check(user("member@example.com")["onboarded_at"] is None and user("member@example.com")["ui_v2"] == 0,
      "the new columns exist and are empty")

print("== the preview: an admin's ?ui=v2 and its sticky cookie")
r = c.get("/app?ui=v2")
check('data-ui="v2"' not in r.get_data(as_text=True) and "cfy_ui" not in (r.headers.get("Set-Cookie") or ""),
      "a member's ?ui=v2 turns nothing on and sets no cookie")
r = c.post("/app/settings/look", data={"csrf": csrf(c), "look": "v2"})
check(r.status_code == 403 and user("member@example.com")["ui_v2"] == 0, "the Settings switch is refused for a member (403)")
check("data-look-setting" not in c.get("/app/team").get_data(as_text=True), "a member's Team page has no look switch")

a = signup("boss@example.com")
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='boss@example.com'")
    conn.commit()
check("data-look-setting" in a.get("/app/team").get_data(as_text=True), "an admin's Team page has the look switch")
check('data-ui="v2"' not in a.get("/app").get_data(as_text=True), "an admin without the preview sees the old look")
r = a.get("/app/team?ui=v2")
cookie = r.headers.get("Set-Cookie") or ""
check('data-ui="v2"' in r.get_data(as_text=True) and V2_CSS in r.get_data(as_text=True), "an admin's ?ui=v2 shows the new look")
check("cfy_ui=" in cookie and "HttpOnly" in cookie and re.search(r"Max-Age=(\d+)", cookie)
      and int(re.search(r"Max-Age=(\d+)", cookie).group(1)) >= 150 * 86400, "and sets a sticky, HttpOnly cookie")
r = a.get("/app/team")
check('data-ui="v2"' in r.get_data(as_text=True), "the cookie keeps it on without the parameter")
check('data-surface="app"' in r.get_data(as_text=True), "app pages are the app surface (dark by default)")
value = a.get_cookie("cfy_ui").value
check(value != "v2", "the cookie's value is signed, not the bare word")

f = client()
f.set_cookie("cfy_ui", "v2")
check('data-ui="v2"' not in f.get("/").get_data(as_text=True), "a forged cookie turns nothing on")
p = client()
p.set_cookie("cfy_ui", value)
pubv2 = p.get("/").get_data(as_text=True)
check('data-ui="v2"' in pubv2 and 'data-surface="public"' in pubv2,
      "the admin's signed cookie also previews the public site in that browser (light surface)")
c.set_cookie("cfy_ui", value)
check('data-ui="v2"' not in c.get("/app").get_data(as_text=True), "a member holding an admin's cookie still sees the old look")
c.delete_cookie("cfy_ui")

r = a.get("/app/team?ui=v1")
check('data-ui="v2"' not in r.get_data(as_text=True) and "cfy_ui=;" in (r.headers.get("Set-Cookie") or ""),
      "?ui=v1 shows the old look and removes the cookie")
check('data-ui="v2"' not in a.get("/app/team").get_data(as_text=True), "and it stays off")
r = a.post("/app/settings/look", data={"csrf": csrf(a), "look": "v2"})
check(r.status_code == 302 and user("boss@example.com")["ui_v2"] == 1, "the Settings switch saves the preview on the admin's account")
check('data-ui="v2"' in a.get("/app/team").get_data(as_text=True), "and the new look is on for them, with no cookie")
check('data-ui="v2"' not in c.get("/app").get_data(as_text=True), "still off for everyone else")
a.post("/app/settings/look", data={"csrf": csrf(a), "look": "v1"})
check(user("boss@example.com")["ui_v2"] == 0 and 'data-ui="v2"' not in a.get("/app/team").get_data(as_text=True),
      "switching back turns it off")

print("== UI_V2=1: everyone, and the onboarding answers")
os.environ["UI_V2"] = "1"
check('data-ui="v2"' in client().get("/").get_data(as_text=True), "the public site has the new look")
check('data-ui="v2"' in client().get("/?ui=v1").get_data(as_text=True), "?ui=v1 can't switch off what is on for everyone")
n = signup("new@example.com")
r = n.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome"), "a new account's Home starts the onboarding steps")
page = n.get("/app/welcome").get_data(as_text=True)
check("What do you want to make?" in page and all(label in page for _, label in onboarding.GOALS)
      and 'name="other"' in page and 'data-surface="public"' in page, "step 1 lists every goal and Other, on the light surface")
check(page.count('type="checkbox"') == len(onboarding.GOALS) and "<label" in page, "every goal is a labelled checkbox")
r = n.post("/app/welcome", data={"csrf": csrf(n), "goals": ["ugc", "image", "not-a-goal"], "other": "  Menus   for my cafe "})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/welcome/profile"), "step 1 goes on to the profile")
ans = onboarding.answers(user("new@example.com"))
check(ans.get("goals") == ["ugc", "image"] and ans.get("other") == "Menus for my cafe",
      "the goals are stored (known codes only) with the Other text")
check(user("new@example.com")["onboarded_at"] is None, "not finished yet")
page = n.get("/app/welcome/profile").get_data(as_text=True)
check(all(f'for="{i}"' in page for i in ("ob-first", "ob-last", "ob-co", "ob-web")), "step 2's four inputs have labels")
r = n.post("/app/welcome/profile", data={"csrf": csrf(n), "first_name": "Dana", "last_name": "Reyes",
                                         "company": "Hearth & Co", "website": "hearth.example"})
u = user("new@example.com")
ans = onboarding.answers(u)
check(r.status_code == 302 and u["name"] == "Dana Reyes" and ans.get("company") == "Hearth & Co"
      and ans.get("website") == "https://hearth.example" and ans.get("goals") == ["ugc", "image"],
      "the profile is stored: name on the member, company and website with the answers, goals kept")
check(u["onboarded_at"], "the steps are finished")
home = n.get("/app")
hh = home.get_data(as_text=True)
check(home.status_code == 200 and "data-cmp" in hh and "data-first-run" not in hh, "Home is now Studio, with the composer")
boot = json.loads(re.search(r"window\.CFY_STUDIO = (\{.*?\});</script>", hh, re.S).group(1))
check(boot["mode"] == "image" and boot["first_run"] and "Presenter video" in boot["note"],
      "first run: they picked presenter videos, the free credits cover image ads, so it starts there and says why")
check(boot["website"] == "https://hearth.example", "the website from the profile is ready for 'Add product from URL'")
check(studio_v2.first_mode(["ugc", "image"], 10 ** 6, studio_v2.MODES) == ("presenter", ""),
      "with enough credits the first mode is the first thing they picked")
check(studio_v2.first_mode([], 44, studio_v2.MODES)[0] == "image" and studio_v2.first_mode(["plan"], 44, studio_v2.MODES) == ("image", ""),
      "no pick (or one with no mode) starts on image ads")
s = signup("skip@example.com")
r = s.post("/app/welcome", data={"csrf": csrf(s), "skip": "1"})
check(r.status_code == 302 and user("skip@example.com")["onboarded_at"] and s.get("/app").status_code == 200,
      "Skip for now leaves the steps for good")
old = c.get("/app")
check(old.status_code == 302, "an account that never made an ad and never saw the steps gets them once too")

print("== Studio: nothing is made without the price that was shown")
pid = add_product(n, "new@example.com")
bal = db.balance(ws_of("new@example.com"))
payload = {"mode": "image", "product": pid, "text": "Your whole evening, in one candle.", "count": 2, "aspect": "4:5"}
hh = n.get("/app").get_data(as_text=True)
check('data-dlg="price"' in hh and "data-confirm-make" in hh and "data-send" in hh
      and 'aria-label="See the price"' in hh, "the page has the send button (it asks for the price) and the price box")
js = open(os.path.join(ROOT, "static", "studio_v2.js"), encoding="utf-8").read()
send_fn = js[js.index("function send(){"):js.index("function showPrice(")]
check(js.count("B.urls.generate") == 1 and "generate" not in send_fn and "B.urls.quote" in send_fn
      and re.search(r"\[data-confirm-make\]'\)\.addEventListener\('click', function\(e\)\{ if\(!lastQuote\) return; generate\(", js),
      "studio_v2.js posts to generate in one place, reached only from the price box's button after a quote")
r = jpost(n, "/app/studio/generate", payload)
check(r.status_code == 409 and r.get_json().get("needs_quote") and jobs_n("new@example.com") == 0,
      "generate without a total: 409, nothing made")
q = jpost(n, "/app/studio/quote", payload).get_json()
want = catalog.price_credits("ad_image", batch=2)
check(q.get("count") == 1 and q.get("total") == want and q.get("balance") == bal and q["lines"][0]["credits"] == want,
      "quote: the same price Create gives for it (catalog.price_credits), with the balance")
check(jobs_n("new@example.com") == 0 and db.balance(ws_of("new@example.com")) == bal, "a quote makes nothing and charges nothing")
r = jpost(n, "/app/studio/generate", dict(payload, total=want - 1))
check(r.status_code == 409 and r.get_json().get("total") == want and jobs_n("new@example.com") == 0
      and db.balance(ws_of("new@example.com")) == bal, "a total that isn't the price: 409 with the real total, nothing made or charged")
r = jpost(n, "/app/studio/generate", dict(payload, count=4, total=want))
check(r.status_code == 409 and jobs_n("new@example.com") == 0,
      "changing the composer after the quote (4 versions, the old total): refused")
r = jpost(n, "/app/studio/generate", dict(payload, total=want))
d = r.get_json()
check(r.status_code == 200 and d.get("ok") and d.get("queued") == 1 and "/app/studio?made=" in d.get("url", ""),
      "the confirmed total starts it and comes back to Studio")
check(jobs_n("new@example.com") == 1 and db.balance(ws_of("new@example.com")) == bal - want,
      "one job, and exactly the shown total is charged")
with db.connect() as conn:
    j = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (user("new@example.com")["workspace_id"],)).fetchone()
    b = conn.execute("SELECT * FROM batches WHERE id=?", (j["batch_id"],)).fetchone()
pr = json.loads(j["params_json"])
check(j["tool"] == "ad_image" and pr["batch"] == 2 and pr["aspect"] == "4:5" and pr["headline"] == payload["text"]
      and json.loads(b["config_json"])["type"] == "image", "the job is the image ad the composer described, in a normal batch")
canvas = n.get(d["url"]).get_data(as_text=True)
check('data-live-cards="studio"' in canvas and f'data-job="{j["id"]}"' in canvas and "st-ghosts" not in canvas,
      "the result is a card on the Studio canvas")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET status='done' WHERE id=?", (j["id"],))
    conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (j["id"], "image", os.path.join(TMP, "none.png")))
    conn.commit()
canvas = n.get("/app/studio").get_data(as_text=True)
check(">Download<" in canvas and f'/app/studio?batch={j["batch_id"]}' in canvas and "More like this" in canvas,
      "a finished card has Download and More like this")
cards = n.get(f"/app/jobs/cards?ctx=studio&ids={j['id']}").get_json()["cards"][str(j["id"])]["html"]
check("More like this" in cards, "the live card refresh keeps the Studio actions")
more = n.get(f"/app/studio?batch={j['batch_id']}").get_data(as_text=True)
boot = json.loads(re.search(r"window\.CFY_STUDIO = (\{.*?\});</script>", more, re.S).group(1))
check(boot["prefill"]["mode"] == "image" and boot["prefill"]["text"] == payload["text"] and boot["prefill"]["count"] == 2
      and not boot["first_run"] and boot["note"] == "", "More like this refills the composer from that batch")
cfg = studio_v2.config_for({"mode": "presenter", "product": pid, "text": "one two three four five", "presenters": ["maya"]})
check(cfg["type"] == "ugc" and cfg["presenters"] == ["maya"] and cfg["duration"] == catalog.TOOLS["ugc_video"]["durations"][0]
      and cfg["items"] == ["one two three four five"], "a presenter video maps to Create's UGC config, length fitted to the script")
cfg = studio_v2.config_for({"mode": "image", "product": pid, "text": "", "preset": "marketplace", "count": 9})
check(cfg["preset"] == "marketplace" and cfg["versions"] == studio_v2.MAX_COUNT and cfg["items"] == ["Marketplace image"],
      "a text-layer preset is carried as Create's preset; the count is capped")
check({g["id"] for g in boot["gallery"]} >= {"type:ugc", "type:image", "clean-headline", "offer", "callouts", "review", "compare"}
      and {g["group"] for g in boot["gallery"]} == {"type", "image", "video"},
      "More lists the ad types and the wave-2 presets, text-layer ones included, in its three groups")
check(len(boot["presenters"]) == len(catalog.PRESENTERS) == 8 and all(p["voice"] for p in boot["presenters"]),
      "the presenter picker has our 8 presenters, each with its voice")
brand = n.get("/app/studio/brand").get_data(as_text=True)
check('for="bs-url"' in brand and 'name="kit_primary"' in brand and 'name="kit_font"' in brand and "/app/start/import" in brand,
      "the brand screen is one labelled input on the free importer, then the Look kit")

print("== the card step says what the billing config says")
os.environ["RS_CARD_REQUIRED"] = "1"
card_on_file.active = card_on_file.required          # Stripe keys stay unset in tests
k = signup("card@example.com")
with db.connect() as conn:
    onboarding.finish(conn, user("card@example.com"))
    conn.commit()
r = k.get("/app")
check(r.status_code == 302 and r.headers["Location"].endswith("/app/card"), "after the steps, a new account lands on the card step")
ws = ws_of("card@example.com")
pending = card_on_file.pending(ws)
t = card_on_file.terms(pending)
page = text(k.get("/app/card").get_data(as_text=True))
plan = min((p for p in plans.PLANS.values() if p["usd"] > 0), key=lambda p: p["usd"])
pack = min(catalog.PACKS, key=lambda p: p["usd"])
check(t["today_usd"] == 0 and t["charge_date"] is None and t["trial_days"] == 0 and not t["auto_renews"],
      "terms: $0 today, no trial, no charge date, nothing renews by itself")
check(t["plan_from"]["usd"] == plan["usd"] and t["pack_from"]["usd"] == pack["usd"] and t["credits"] == pending > 0
      and t["images"] == pending // catalog.price_credits("ad_image", batch=1), "terms: the cheapest plan and pack, the waiting credits")
check("Today, $0" in page and "Card saved. Nothing charged." in page and "Save card, $0 today" in page,
      "the timeline starts at Today, $0 and the button says $0 today")
check(f"{pending:,} free credits" in page and f"Enough for {t['images']} image ad" in page, "the waiting credits and what they make")
check(f"A plan from ${plan['usd']} a month" in page and f"a credit pack from ${pack['usd']}" in page
      and f"{plan['name']} is ${plan['usd']} a month with {plan['credits']:,} credits" in page
      and f"{pack['name']} is ${pack['usd']} once for {pack['credits']:,} credits" in page,
      "the only later charges named are the real plan and pack prices")
check("There is no charge date." in page and not re.search(r"free trial|trial ends|charged on|you'll be charged|days? left", page, re.I),
      "no trial, no charge date and no countdown anywhere on the step")
real = plan["usd"]
plan["usd"] = 41
try:
    check("A plan from $41 a month" in text(k.get("/app/card").get_data(as_text=True)), "the price is read from plans.PLANS, not typed into the page")
finally:
    plan["usd"] = real
os.environ.pop("UI_V2")
oldcard = k.get("/app/card").get_data(as_text=True)
check("Add a card to get your free credits" in oldcard and "data-card-terms" not in oldcard, "off: the card step is the old page")

os.environ.pop("RS_CARD_REQUIRED", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
