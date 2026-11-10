"""The new look across the whole product (M15: ui_v2.py, studio_v2.py, public_site.showcase, web/v2/*), on a throwaway
database with every vendor faked:

  * off (the default): the home page, pricing, the standalone pages and Studio's new routes are as before
  * search metadata is the same with the look on as with it off: title, description, canonical, share card and
    JSON-LD of every public page, the sitemap, public_site.PAGES; /mcp still answers MCP clients
  * the home page: one promise, three steps, the FAQ its FAQPage data lists, prices from the billing config, and a
    gallery that shows only what static/showcase/manifest.json lists (neutral tiles while it is empty)
  * the pricing page's numbers are the billing config's (plans.PLANS, catalog.PACKS, the rate card)
  * Studio's first run: the two free image ads are two DIFFERENT templates, the ones the classic first run picks
    (catalog.first_run_pair), priced and confirmed once, made in one batch, charged exactly what was shown
  * the presenter picker: today's eight when there is no `presenters` module, that module's roster when there is,
    and the chosen situation rides along (an unknown one changes nothing)
  * "More": a preset's real example (presets.example_url) and its `use` tag when the records have them
  * Workflow: packs are bundles of existing presets, priced by the same quote and started only with the total shown;
    the describe box is the campaign planner's own form

    python tests/test_ui_v2_m15.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_uiv2m15_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY",
          "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import create_routes  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import plans  # noqa: E402
import presets  # noqa: E402
import public_site  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=64, h=80, rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(b, "PNG")
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
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
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


def add_product(c, email, name="Ember Candle"):
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": name, "photos": (png(400, 400), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                         (user(email)["workspace_id"],)).fetchone()
        assert p is not None, "the product was not made"
        if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
            import products
            path = os.path.join(TMP, f"p-{p['id']}.png")
            open(path, "wb").write(png(400, 400).getvalue())
            products.add_photo(conn, p["workspace_id"], p["id"], path, "p.png", "image/png")
            conn.commit()
        return p["id"]


def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def batches_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM batches WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def boot_of(html):
    return json.loads(re.search(r"window\.CFY_STUDIO = (\{.*?\});</script>", html, re.S).group(1))


def finish_onboarding(email):
    import onboarding
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.commit()


def head_of(html):
    """The search metadata of a page: title, description, canonical, robots, share card and JSON-LD."""
    def one(pat):
        m = re.search(pat, html, re.S)
        return m.group(1) if m else None
    return {"title": one(r"<title>(.*?)</title>"), "description": one(r'<meta name="description" content="([^"]*)"'),
            "canonical": one(r'<link rel="canonical" href="([^"]*)"'), "robots": one(r'<meta name="robots" content="([^"]*)"'),
            "og": sorted(re.findall(r'<meta property="og:[a-z:_]+" content="[^"]*">', html)),
            "twitter": sorted(re.findall(r'<meta name="twitter:[a-z:_]+" content="[^"]*">', html)),
            "jsonld": one(r'<script type="application/ld\+json">(.*?)</script>')}


PUBLIC = ["/", "/pricing", "/features", "/for-reps", "/guides", "/llm-info", "/privacy", "/terms", "/messaging-policy",
          "/docs/mcp", "/mcp", "/" + public_site.SOLUTION_ORDER[0], "/" + public_site.COMPARE[0],
          public_site.guide_path(public_site.GUIDE_ORDER[0])]
HTML = {"Accept": "text/html,application/xhtml+xml"}

print("== off (the default): the public site and Studio are as before")
off = {p: client().get(p, headers=HTML) for p in PUBLIC}
check(all(r.status_code == 200 for r in off.values()), "every public page answers")
home = off["/"].get_data(as_text=True)
check('data-ui="v2"' not in home and "lv-hero" not in home and "data-showcase" not in home and "lp-hero" in home
      and 'class="lp-gallery"' in home, "the home page is the old one: its hero, its gallery, no showcase")
price = off["/pricing"].get_data(as_text=True)
check("pr-plans" not in price and 'class="lp-prices plans"' in price and "Most popular" in price,
      "the pricing page is the old one")
sitemap_off = client().get("/sitemap.xml").get_data(as_text=True)
pages_off = dict(public_site.PAGES)
m = signup("member@example.com")
r = m.get("/app/studio/workflow")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "/app/studio/workflow sends people to Home")
check(jpost(m, "/app/studio/quote", {"mode": "image", "pair": True, "count": 2}).status_code == 404
      and jpost(m, "/app/studio/generate", {"pack": "launch", "total": 1}).status_code == 404,
      "a pair or a pack asked of Studio while it is off: 404")
check("ui_v2.css" not in client().get("/no-such-page").get_data(as_text=True), "the 404 page is the old one")
check("v2/_pp" not in open(os.path.join(ROOT, "web", "page_public.html")).read().split("{% if ui_v2 %}")[0],
      "members' pages only add the new styles behind the switch")

print("== on: the search metadata is the same")
os.environ["UI_V2"] = "1"
on = {p: client().get(p, headers=HTML) for p in PUBLIC}
check(all(r.status_code == 200 and 'data-ui="v2"' in r.get_data(as_text=True) for r in on.values()),
      "every public page takes the new look")
for path in PUBLIC:
    a, b = head_of(off[path].get_data(as_text=True)), head_of(on[path].get_data(as_text=True))
    check(a == b and a["title"] and (a["canonical"] or path == "/mcp"),
          f"{path}: the same title, description, canonical, share card and JSON-LD")
check(client().get("/sitemap.xml").get_data(as_text=True) == sitemap_off, "the sitemap is the same")
check(dict(public_site.PAGES) == pages_off and {"index", "features", "pricing", "for_reps", "mcp.mcp_endpoint", "mcp_docs"}
      <= set(public_site.PAGES), "public_site.PAGES is untouched")
r = client().post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                  content_type="application/json", headers={"Accept": "application/json, text/event-stream"})
check("<html" not in r.get_data(as_text=True).lower(), "/mcp still answers an MCP client with the server, not the page")
check('data-ui="v2"' in client().get("/no-such-page").get_data(as_text=True)
      and client().get("/no-such-page").status_code == 404, "the 404 page takes the new look too")

print("== the home page")
home = on["/"].get_data(as_text=True)
t = text(home)
check(home.count("<h1") == 1 and "Turn your product page into finished ads." in t, "one promise, in one h1")
check(len(re.findall(r'<ol class="lv-steps">.*?</ol>', home, re.S)) == 1
      and len(re.findall(r"<li>", re.search(r'<ol class="lv-steps">(.*?)</ol>', home, re.S).group(1))) == 3
      and all(s in t for s in ("Paste your product page", "See the price first", "Download finished ads")),
      "how it works, in three steps")
import adoffer  # noqa: E402
so = adoffer.offer()
check(home.count(so["cta"]) == 2 and 'data-cta="hero"' in home and 'data-cta="closing"' in home,
      "one call to action, the sign-up offer's own words, at the top and at the end")
faq = public_site.home_faq()
check(all(q in t for q, _ in faq) and home.count("<details>") == len(faq)
      and json.loads(head_of(home)["jsonld"].replace("<\\/", "</"))["@graph"][-1]["@type"] == "FAQPage",
      "every question the page's FAQPage data lists is on the page")
check(not re.search(r"countdown|ends in|hurry|only \d+ left|limited time|trusted by|\d[\d,]{2,}\+? (customers|brands|teams|businesses)|"
                    r"testimonial|most popular|as seen (on|in)", t, re.I),
      "no urgency, countdowns, customer counts, testimonials or 'most popular'")
r_ = catalog.rates()
check(f"{r_['image']} credits" in t and f"{r_['video5']:,} credits" in t and f"{r_['talk8']:,} credits" in t,
      "what each ad costs is the rate card's")
real = r_["image"]
_rates = catalog.rates
catalog.rates = lambda: dict(_rates(), image=real + 3)
appmod.app.jinja_env.globals["rates"] = catalog.rates
try:
    check(f"{real + 3} credits" in text(client().get("/").get_data(as_text=True)), "and it is read, not typed into the page")
finally:
    catalog.rates = _rates
    appmod.app.jinja_env.globals["rates"] = _rates

print("== the showcase shows only what the manifest lists")
check('data-showcase="0"' in home and home.count('class="lv-shot ph"') == len(public_site.SHOWCASE_SHAPES)
      and "site/ugc-" not in home and "site/show-" not in home and "<video" not in home,
      "an empty manifest: neutral tiles, none of the old invented examples, no media at all")
check(json.load(open(os.path.join(ROOT, "static", "showcase", "manifest.json")))["items"] == [],
      "the manifest in the repo starts empty")
real_dir = public_site.SHOWCASE_DIR
show = os.path.join(TMP, "showcase")
os.makedirs(show)
open(os.path.join(show, "hero.png"), "wb").write(png(800, 1000).getvalue())
open(os.path.join(show, "square.png"), "wb").write(png(600, 600).getvalue())
open(os.path.join(show, "clip.mp4"), "wb").write(b"\x00\x00\x00\x18ftypmp42")
open(os.path.join(show, "notes.txt"), "w").write("x")
json.dump({"items": [
    {"file": "hero.png", "kind": "image", "preset": "hero", "caption": "A candle on stone", "alt": "A candle in an amber jar"},
    {"file": "clip.mp4", "kind": "video", "preset": "voiceover", "caption": "A narrated clip", "alt": "A candle burning",
     "poster": "square.png", "aspect": "9:16"},
    {"file": "square.png", "kind": "image", "preset": "no-such-preset", "caption": "", "alt": "Square picture"},
    {"file": "../app.py", "kind": "image", "caption": "outside the folder", "alt": "x"},
    {"file": "missing.png", "kind": "image", "caption": "not there", "alt": "x"},
    {"file": "notes.txt", "kind": "image", "caption": "not a picture", "alt": "x"},
    {"file": "hero.png", "kind": "poster", "caption": "wrong kind", "alt": "x"},
    {"file": "hero.png", "kind": "image", "caption": "", "alt": ""},
    "not an item"]}, open(os.path.join(show, "manifest.json"), "w"))
public_site.SHOWCASE_DIR = show
try:
    items = public_site.showcase()
    check([i["src"] for i in items] == ["showcase/hero.png", "showcase/clip.mp4", "showcase/square.png"],
          "three good items are kept, in order; outside the folder, missing, wrong type, wrong kind and wordless are dropped")
    check(items[0]["preset_name"] == presets.get("hero").name and (items[0]["width"], items[0]["height"]) == (800, 1000)
          and items[1]["kind"] == "video" and items[1]["poster"] == "showcase/square.png" and items[1]["aspect"] == "9:16"
          and items[2]["preset"] == "" and items[2]["alt"] == "Square picture",
          "each carries its preset's name, its size, a video's still, and an alt text")
    page = client().get("/").get_data(as_text=True)
    check('data-showcase="3"' in page and 'class="lv-shot ph"' not in page and "Made in Campaignify" in page
          and 'alt="A candle in an amber jar"' in page and 'width="800" height="1000"' in page
          and "showcase/clip.mp4" in page and "A narrated clip" in page and presets.get("hero").name in page,
          "the home page shows them: picture with alt and size, clip with caption, the preset named")
    json.dump({"items": "nonsense"}, open(os.path.join(show, "manifest.json"), "w"))
    os.utime(os.path.join(show, "manifest.json"), (1, 10 ** 9 + 5))
    check(public_site.showcase() == [], "a manifest that isn't a list of items shows nothing, and is read again when it changes")
finally:
    public_site.SHOWCASE_DIR = real_dir
    public_site._showcase.update(key=None, items=[])

print("== pricing: every number is the billing config's")
for path in ("/pricing", "/"):
    page = client().get(path).get_data(as_text=True)
    pt = text(page)
    check('class="pr-plans"' in page and 'class="lp-prices plans"' not in page and "Most popular" not in page,
          f"{path}: the plans in the new look, without a 'Most popular' label")
    for p in plans.PAID:
        block = text(re.search(rf'data-plan="{p["code"]}">(.*?)</section>', page, re.S).group(1))
        check(block.startswith(p["name"]) and f"${p['usd']} a month" in block and f"{p['credits']:,} credits every month" in block
              and public_site.ad_math(p["credits"], p["usd"])["counts"] in block and p["line"] in block,
              f"{path}: {p['name']} shows ${p['usd']}, {p['credits']:,} credits and what they make")
    for k in catalog.PACKS:
        block = text(re.search(rf'data-pack="{k["code"]}">(.*?)</section>', page, re.S).group(1))
        check(f"${k['usd']} once" in block and f"{k['credits']:,} credits" in block
              and (not k["bonus"] or f"includes {k['bonus']:,} bonus" in block), f"{path}: the {k['name']} pack shows ${k['usd']} and {k['credits']:,} credits")
    sc = catalog.starter_credits()
    check(f"{sc:,} starter credits" in pt and "$0" in pt, f"{path}: Free shows the starter credits")
plan = plans.PLANS["growth"]
was = (plan["usd"], plan["credits"])
plan["usd"], plan["credits"] = 101, 6100
try:
    pt = text(client().get("/pricing").get_data(as_text=True))
    check("$101 a month" in pt and "6,100 credits every month" in pt and "$99 a month" not in pt,
          "change the plan in the config and the page changes with it")
finally:
    plan["usd"], plan["credits"] = was

print("== Studio's first run: two different templates")
n = signup("new@example.com")
finish_onboarding("new@example.com")
pid = add_product(n, "new@example.com", "Ember Candle")
bal = db.balance(ws_of("new@example.com"))
one = catalog.price_credits("ad_image", batch=1)
check(bal == 2 * one, "a new account holds the credits for two single image ads")
want = catalog.first_run_pair({"product": "Ember Candle", "description": "", "tags": ""})
check(want[0]["id"] != want[1]["id"], "the classic first run picks two different templates for this product")
hh = n.get("/app").get_data(as_text=True)
boot = boot_of(hh)
check(boot["first_run"] and boot["pair"] and boot["pair_names"] == [t["name"] for t in want] and boot["prefill"]["count"] == 2,
      "Studio opens on 2 image ads and names the two looks the classic first run would pick")
check("data-pair-hint" in hh and "data-empty-step" in hh, "the composer has the place that says so, and the empty canvas its steps")
js = open(os.path.join(ROOT, "static", "studio_v2.js"), encoding="utf-8").read()
check("d.pair = true" in js and re.search(r"function pairOn\(\)\{ return !!B\.pair && S\.mode === 'image' && S\.count === 2; \}", js),
      "studio_v2.js asks for the pair only on a first run, for an image ad, when two are asked for")
payload = {"mode": "image", "product": pid, "text": "Your whole evening, in one candle.", "count": 2, "aspect": "", "pair": True}
q = jpost(n, "/app/studio/quote", payload).get_json()
check(q.get("count") == 2 and q.get("ads") == 2 and q.get("total") == 2 * one and q.get("parts") == [one, one]
      and q.get("balance") == bal and not q.get("short"), "the quote: two ads, each a single image ad's price")
check([ln["label"].split(" · ")[0] for ln in q["lines"]] == [t["name"] for t in want]
      and [ln["label"].split(" · ")[1] for ln in q["lines"]] == [t["aspect"] for t in want],
      "its two lines name the two templates, each in its own shape")
check(len(jobs_of("new@example.com")) == 0 and db.balance(ws_of("new@example.com")) == bal, "a quote makes nothing and charges nothing")
r = jpost(n, "/app/studio/generate", payload)
check(r.status_code == 409 and r.get_json().get("needs_quote") and not jobs_of("new@example.com"), "no total: nothing starts")
r = jpost(n, "/app/studio/generate", dict(payload, total=q["total"] - 1))
check(r.status_code == 409 and r.get_json().get("total") == q["total"] and not jobs_of("new@example.com")
      and db.balance(ws_of("new@example.com")) == bal, "a total that isn't the price: 409 with the real total, nothing made")
r = jpost(n, "/app/studio/generate", dict(payload, total=q["total"]))
d = r.get_json()
jobs = jobs_of("new@example.com")
check(r.status_code == 200 and d.get("ok") and d.get("queued") == 2 and "/app/studio?made=" in d.get("url", ""),
      "the confirmed total starts both and comes back to Studio")
check(len(jobs) == 2 and [j["template_id"] for j in jobs] == [t["id"] for t in want]
      and len({j["template_id"] for j in jobs}) == 2, "two jobs, two DIFFERENT templates: the classic first run's pair")
prs = [json.loads(j["params_json"]) for j in jobs]
check(all(p["batch"] == 1 and p["headline"] == payload["text"] for p in prs)
      and [p["aspect"] for p in prs] == [t["aspect"] for t in want] and prs[0]["label"].startswith(want[0]["name"])
      and prs[1]["label"].startswith(want[1]["name"]) and jobs[0]["brief"] != jobs[1]["brief"],
      "one picture each with the member's headline, each template's own scene and shape, named on its card")
bs = batches_of("new@example.com")
check(len(bs) == 1 and bs[0]["id"] == d["batch"] and bs[0]["title"] == "Your first ads" and bs[0]["credits"] == 2 * one
      and all(j["batch_id"] == bs[0]["id"] for j in jobs)
      and json.loads(bs[0]["config_json"])["templates"] == [t["id"] for t in want],
      "one batch, 'Your first ads', holding both, with both template ids on it")
check(db.balance(ws_of("new@example.com")) == bal - 2 * one == 0, "exactly the shown total is charged")
canvas = n.get(d["url"]).get_data(as_text=True)
check(all(f'data-job="{j["id"]}"' in canvas for j in jobs) and want[0]["name"] in canvas and want[1]["name"] in canvas,
      "both are cards on the canvas, each with its look's name")
with db.connect() as conn:
    db.adjust_credits(conn, ws_of("new@example.com")["id"], 5000, "test top-up")
    conn.commit()
q2 = jpost(n, "/app/studio/quote", payload).get_json()
check(q2.get("count") == 1 and q2.get("total") == catalog.price_credits("ad_image", batch=2) and "parts" not in q2,
      "after the first run the same request is an ordinary one: two versions of one image ad")
check(not boot_of(n.get("/app").get_data(as_text=True))["pair"], "and Studio no longer asks for the pair")

g = signup("glow@example.com")
finish_onboarding("glow@example.com")
gp = add_product(g, "glow@example.com", "Glow Serum for dry skin")
gq = jpost(g, "/app/studio/quote", {"mode": "image", "product": gp, "text": "Soft by morning.", "count": 2, "pair": True}).get_json()
gwant = catalog.first_run_pair({"product": "Glow Serum for dry skin", "description": "", "tags": ""})
check([ln["label"].split(" · ")[0] for ln in gq["lines"]] == [t["name"] for t in gwant]
      and {t["id"] for t in gwant} != {t["id"] for t in want}, "another kind of product gets the pair that suits it")
gq = jpost(g, "/app/studio/quote", {"mode": "image", "product": gp, "text": "Soft by morning.", "count": 2, "pair": True,
                                    "preset": "closeup"}).get_json()
check(gq["count"] == 2 and gq["lines"][0]["label"].startswith(presets.get("closeup").name)
      and gq["lines"][1]["label"].split(" · ")[0] in [t["name"] for t in gwant]
      and gq["lines"][1]["label"].split(" · ")[0] != presets.get("closeup").name,
      "a preset the member pinned stays the first of the two; the second is a different one that suits the product")
gq = jpost(g, "/app/studio/quote", {"mode": "image", "product": gp, "text": "Soft by morning.", "count": 3, "pair": True}).get_json()
check(gq.get("count") == 1 and "parts" not in gq, "asking for three is an ordinary request (the pair is for two)")
check(studio_v2.config_for({"mode": "image", "product": gp, "text": "x", "count": 2, "pair": True}).get("pair") is None,
      "the pair flag never reaches Create's config")

print("== the presenter picker: the fallback, the roster, the situation")
sys.modules.pop("presenters", None)
with appmod.app.test_request_context("/app"):
    sys.modules["presenters"] = None           # "import presenters" raises ImportError, as on a branch without it
    try:
        rows = studio_v2.roster()
    finally:
        sys.modules.pop("presenters", None)
check([r["id"] for r in rows] == [p["id"] for p in catalog.PRESENTERS] and len(rows) == 8,
      "no presenters module: today's eight presenters")
keys = {"id", "name", "age_band", "gender", "look", "voice", "portrait", "voice_sample", "situations"}
check(all(keys <= set(r) and set(r["voice"]) == {"id", "label"} and r["voice"]["label"] and len(r["situations"]) == 1
          and {"id", "label", "still"} <= set(r["situations"][0]) for r in rows),
      "each in the roster's shape: a voice, one situation (the setting they are described in), no portrait needed")
by = {r["id"]: r for r in rows}
check(by["maya"]["gender"] == "female" and by["jordan"]["gender"] == "male" and by["sam"]["gender"] == ""
      and by["maya"]["age_band"] == "20s" and by["elena"]["age_band"] == "50s"
      and by["maya"]["voice"]["id"] == catalog.default_voice("maya") and by["maya"]["situations"][0]["label"] == "Bright, lived-in kitchen",
      "gender and age band are read from the description (none where it doesn't say), the voice is the default one")
fake = types.ModuleType("presenters")
fake.roster = lambda: [
    {"id": "maya", "name": "Maya", "age_band": "young adult", "gender": "female", "look": "curly dark hair",
     "voice": {"id": "warm_f", "label": "Warm"}, "portrait": "/static/presenters/maya.jpg", "voice_sample": "/static/voices/warm_f.mp3",
     "situations": [{"id": "kitchen", "label": "Kitchen", "still": "/static/presenters/maya-kitchen.jpg"},
                    {"id": "car", "label": "In the car", "still": None}, {"label": "no id"}]},
    {"id": "jordan", "name": "Jordan", "voice": {"id": "chatty_m", "label": "Chatty"}, "situations": []},
    "junk", {"name": "no id"}]
fake.get = lambda pid: next((r for r in fake.roster() if isinstance(r, dict) and r.get("id") == pid), None)
sys.modules["presenters"] = fake
try:
    with appmod.app.test_request_context("/app"):
        rows = studio_v2.roster()
    check([r["id"] for r in rows] == ["maya", "jordan"] and rows[0]["portrait"].endswith("maya.jpg")
          and rows[0]["voice_sample"].endswith("warm_f.mp3") and [s["id"] for s in rows[0]["situations"]] == ["kitchen", "car"]
          and rows[0]["situations"][1]["still"] == "" and rows[1]["portrait"] == "" and rows[1]["situations"] == []
          and rows[1]["age_band"] == "", "with the module: its roster, portraits, voice samples and situations, cleaned")
    pb = boot_of(n.get("/app?mode=presenter").get_data(as_text=True))
    check([p["id"] for p in pb["presenters"]] == ["maya", "jordan"] and pb["presenters"][0]["situations"][0]["still"].endswith("maya-kitchen.jpg"),
          "Studio's picker gets that roster")
    fake.roster = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    with appmod.app.test_request_context("/app"):
        check(len(studio_v2.roster()) == 8, "a roster that fails falls back to today's eight")
    fake.roster = lambda: []
    with appmod.app.test_request_context("/app"):
        check(len(studio_v2.roster()) == 8, "so does an empty one")
finally:
    sys.modules.pop("presenters", None)
hh = n.get("/app?mode=presenter").get_data(as_text=True)
check(len(boot_of(hh)["presenters"]) == 8 and 'data-pf-pills="gender"' in hh and 'data-pf-pills="age_band"' in hh
      and "data-pres-none" in hh, "the picker's page has the gender and age filters and an empty state")
check(all(s in js for s in ("data-voice-play", "role=\"radiogroup\" aria-label=\"Where ", "d.situation =", "d.situations = sits",
                            "aria-pressed=\"' + (playing === p.id)")),
      "studio_v2.js draws the voice preview and the situation chips and sends the chosen situation")
cfg = studio_v2.config_for({"mode": "presenter", "product": pid, "text": "one two three", "presenters": ["maya"],
                            "situation": "kitchen", "situations": {"maya": "kitchen"}})
check(cfg["presenters"] == ["maya"] and cfg["situation"] == "kitchen" and cfg["situations"] == {"maya": "kitchen"},
      "the chosen presenter and situation go into the generate config as `presenters` and `situation`")
talk = {"mode": "presenter", "product": pid, "text": "I light this when I get home.", "presenters": ["maya"]}
qa = jpost(n, "/app/studio/quote", talk).get_json()
qb = jpost(n, "/app/studio/quote", dict(talk, situation="a-situation-nobody-knows", situations={"maya": "nope", "x": 3})).get_json()
check(qa.get("count") == 1 and qb.get("count") == 1 and qa["total"] == qb["total"] and not qb.get("error"),
      "a situation Create doesn't know changes nothing: the same one video at the same price")

print("== More: real examples and use tags when the preset records have them")
with appmod.app.test_request_context("/app"):
    gal = studio_v2.gallery(None)
    check(all(not x["example"] and x["preview"] and x["uses"] == [] for x in gal)
          and [p["by"] for p in studio_v2.gallery_pills(gal)] == ["group"] * 3,
          "today: every card keeps its placeholder and the pills are the three groups")
    presets.example_url = lambda pid: {"hero": "/media/examples/hero.jpg", "turn": "/media/examples/turn.mp4"}.get(pid)
    real_extra = presets.get("offer").extra
    object.__setattr__(presets.get("offer"), "extra", dict(real_extra, use="offer"))
    object.__setattr__(presets.get("hero"), "extra", dict(presets.get("hero").extra, use=["launch", "offer"]))
    try:
        gal = studio_v2.gallery(None)
        byid = {x["id"]: x for x in gal}
        check(byid["hero"]["preview"] == "/media/examples/hero.jpg" and byid["hero"]["example"] and byid["hero"]["preview_kind"] == "image"
              and byid["turn"]["preview_kind"] == "video" and not byid["flatlay"]["example"] and byid["flatlay"]["preview"].endswith(".jpg"),
              "presets.example_url is used where it answers; the others keep their placeholder")
        check(byid["offer"]["uses"] == ["offer"] and byid["hero"]["uses"] == ["launch", "offer"]
              and studio_v2.gallery_pills(gal) == [{"by": "use", "id": "launch", "label": "Launch"}, {"by": "use", "id": "offer", "label": "Offer"}],
              "with use tags on the records, the pills are the uses")
        presets.example_url = lambda pid: (_ for _ in ()).throw(RuntimeError("x"))
        check(not studio_v2.gallery(None)[0]["example"], "an example lookup that fails keeps the placeholder")
    finally:
        del presets.example_url
        object.__setattr__(presets.get("offer"), "extra", real_extra)
        object.__setattr__(presets.get("hero"), "extra", {k: v for k, v in presets.get("hero").extra.items() if k != "use"})
check("x.uses" in js and "data-gby" in hh, "studio_v2.js filters by use when the pills say so")

print("== Workflow: the planner's box and packs of presets")
wf = n.get("/app/studio/workflow")
wh = wf.get_data(as_text=True)
wb = boot_of(wh)
check(wf.status_code == 200 and "data-wf" in wh and 'aria-current=page>Workflow' in wh and "Describe what you want" in wh,
      "the Workflow tab is there, lit in the sidebar")
form = re.search(r'<form method="post" action="([^"]+)" class="wf-box" data-plan-form>(.*?)</form>', wh, re.S)
import planner  # noqa: E402
check(form and form.group(1) == "/app/campaigns" and f'name="product" value="{pid}"' in form.group(2)
      and 'name="notes"' in form.group(2) and 'name="offer" value="Ember Candle"' in form.group(2) and 'name="csrf"' in form.group(2),
      "the describe box is the campaign planner's own form: its route, this product, the description as its notes")
check(f"{planner.cap_credits():,} credits" in text(wh) or plans.required("planner")["name"] in text(wh),
      "its price note (up to the planner's cap) or the plan it comes with is on the page before it can be sent")
prices = catalog.from_prices()
packs = {p["id"]: p for p in wb["packs"]}
check(set(packs) == {"launch", "offer", "marketplace"} and packs["launch"]["counts"] == "2 image ads + 1 voiceover video"
      and packs["launch"]["from"] == 2 * prices["ad_image"] + prices["ugc_video"]
      and packs["offer"]["from"] == 3 * prices["ad_image"] == packs["marketplace"]["from"],
      "three packs, each from the prices of the presets inside it")
check(all(x.get("preset") is None or presets.get(x["preset"]) is not None for p in studio_v2.PACKS for x in p["parts"]),
      "every pack is built from presets that exist")
lp = {"pack": "launch", "product": pid, "fields": {"headline": "Your whole evening, in one candle.",
                                                   "script": "Ember Candle. Soy wax with amber and cedar."}}
q = jpost(n, "/app/studio/quote", lp).get_json()
talk8 = catalog.price_credits("ugc_video", duration=catalog.TOOLS["ugc_video"]["durations"][0])
check(q.get("count") == 3 and q.get("ads") == 3 and q.get("parts") == [one, one, talk8] and q.get("total") == 2 * one + talk8
      and q.get("videos") and len(q["lines"]) == 3 and q["lines"][1]["label"].startswith("Clean headline")
      and q["lines"][2]["label"].startswith("Voiceover"), "the Launch pack's quote: two image ads and a voiceover, each at its own price")
check(jpost(n, "/app/studio/quote", {"pack": "launch", "product": pid, "fields": {"headline": "x"}}).get_json().get("error", "").startswith("Write the voiceover script"),
      "a pack with a field left empty says which, and prices nothing")
check(jpost(n, "/app/studio/quote", {"pack": "nope", "product": pid}).get_json().get("count") == 0, "an unknown pack prices nothing")
before, bal = len(jobs_of("new@example.com")), db.balance(ws_of("new@example.com"))
r = jpost(n, "/app/studio/generate", lp)
check(r.status_code == 409 and len(jobs_of("new@example.com")) == before, "a pack without the total: nothing starts")
r = jpost(n, "/app/studio/generate", dict(lp, total=q["total"] + 5))
check(r.status_code == 409 and r.get_json().get("total") == q["total"] and len(jobs_of("new@example.com")) == before
      and db.balance(ws_of("new@example.com")) == bal, "a wrong total: 409 with the real one, nothing made or charged")
with db.connect() as conn:
    conn.execute("UPDATE users SET email_verified_at=? WHERE email='new@example.com'", (db.now(),))
    conn.commit()
r = jpost(n, "/app/studio/generate", dict(lp, total=q["total"]))
d = r.get_json()
new_jobs = jobs_of("new@example.com")[before:]
check(r.status_code == 200 and d.get("ok") and d.get("queued") == 3 and len(new_jobs) == 3
      and sorted(j["tool"] for j in new_jobs) == ["ad_image", "ad_image", "ugc_video"],
      "the confirmed total starts all three: two image ads and one video")
b = [x for x in batches_of("new@example.com") if x["id"] == d["batch"]][0]
check(len(batches_of("new@example.com")) == 2 and b["title"] == "Launch pack" and b["credits"] == q["total"]
      and all(j["batch_id"] == b["id"] for j in new_jobs) and json.loads(b["config_json"]).get("pack") == "launch",
      "in one batch called 'Launch pack'")
check(db.balance(ws_of("new@example.com")) == bal - q["total"], "and exactly the shown total is charged")
check(len({j["template_id"] for j in new_jobs if j["tool"] == "ad_image"}) == 2, "the two image ads are two different presets")
op = {"pack": "offer", "product": pid, "fields": {"headline": "Your whole evening.", "badge": "Free shipping this week"}}
oq = jpost(n, "/app/studio/quote", op).get_json()
check(oq.get("count") == 0 and oq.get("error", "").startswith("Offer"),
      "the Offer pack refuses a badge that isn't the product's saved price or offer (the preset's own rule)")
mq = jpost(n, "/app/studio/quote", {"pack": "marketplace", "product": pid, "fields": {"headline": "Ember Candle"}}).get_json()
check(mq.get("count") == 3 and mq.get("total") == 3 * one and mq["lines"][0]["label"].startswith("Marketplace image"),
      "the Marketplace set: three image ads, the white-background one first")
with appmod.app.test_request_context("/app"):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    check(studio_v2.starter_script(row) == "" and studio_v2.starter_script(None) == "",
          "a product with no description gets no draft script (nothing is invented)")
    row = dict(row, description="A soy wax candle with amber and cedar. Hand poured in small batches. " + "Long sentence " * 40 + ".")
    draft = studio_v2.starter_script(row)
    check(draft == "Ember Candle. A soy wax candle with amber and cedar. Hand poured in small batches.",
          "the draft script is the product's own name and description, whole sentences that fit a short video")

print("== the look reaches the standalone pages")
for name in ("share.html", "unsubscribe.html", "weekly_unsub.html"):
    src = open(os.path.join(ROOT, "web", name)).read()
    check('{% if ui_v2 %}<style>{% include "v2/_solo.css" %}</style>{% endif %}' in src, f"{name} takes the neutral tokens behind the switch")
for name in ("page_public.html", "form_public.html", "form_errors.html"):
    src = open(os.path.join(ROOT, "web", name)).read()
    check('{% if ui_v2 %}<style>{% include "v2/_pp.css" %}</style>{% endif %}' in src, f"{name} takes them too, keeping the member's own button colour")
css = open(os.path.join(ROOT, "static", "ui_v2.css")).read()
check(re.search(r"--accent:var\(--v2-inv\)", css) and "--v2-spark:#BDFA25" in css,
      "the stylesheet turns the old accent fills to ink and keeps the lime for small marks")


def _lum(hx):
    v = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    v = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in v]
    return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]


def ratio(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


light, dark = css.split('html[data-ui="v2"]:root{', 1)[1].split("}", 1)[0], css.split('html[data-ui="v2"][data-theme="dark"]:root,', 1)[1].split("}", 1)[0]


def tok(block, name):
    return re.search(rf"--{name}:(#[0-9A-Fa-f]{{6}})", block).group(1)


for name, block, grounds in (("light", light, ("bg", "panel", "v2-sunk")), ("dark", dark, ("bg", "panel", "v2-raise"))):
    for fg in ("ink", "muted", "dim"):
        worst = min(ratio(tok(block, fg), tok(block, g_)) for g_ in grounds)
        check(worst >= 4.5, f"{name}: --{fg} text is at least 4.5:1 on every surface ({worst:.2f})")
check("prefers-reduced-motion" in js and "@keyframes" not in css, "motion: none added by the look; the gallery's clips stay still when asked")

os.environ.pop("UI_V2", None)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
