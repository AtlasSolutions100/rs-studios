"""Site copy (roadmap M5.8, re-audit L-4): the hero sells the whole flow, one pricing
sum on /pricing and Billing, the sign-up page, the code box, a real 404, the first
empty Library, and card-on-file copy that says the card plainly when the card step
is on. Then the public pages' metadata (M5.7): og:image on /p/ pages, the font CSP,
image sizes, the Google Ads tag only where conversions happen, /docs/mcp's head. Renders pages on a throwaway database; nothing leaves the machine.

    python tests/test_site_copy.py      (tests/test_app.py runs it too)
"""
import os
import re
import shutil
import sys
import tempfile
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_sitecopy_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="0", RS_NUDGE_LOOP="0")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "STRIPE_SECRET_KEY",
          "STRIPE_WEBHOOK_SECRET", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import plans  # noqa: E402
import public_site  # noqa: E402

passed = 0
DASHES = ("—", "–")


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def _no_network(*a, **kw):
    raise AssertionError("no real HTTP in these tests")


billing._get = billing._post = _no_network
appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def page(c, path):
    return c.get(path).get_data(as_text=True)


def text(html):
    html = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})
    assert r.status_code in (302, 303), r.status_code
    return c


r_ = catalog.rates()
STARTER = plans.PLANS["starter"]

# ================================================================ hero
print("== hero: the whole flow")
home = page(client(), "/")
ht = text(home)
check("Ads, copy, plan and page from one product link" in home and "Get the whole campaign." in ht,
      "home hero: eyebrow and h1 name the whole campaign")
check("writes the copy for each platform, plans the budget and the first 2-week test, builds the landing page and "
      "walks you through launch in Ads Manager" in ht, "the lede names the copy, plan, page and launch")
check(f"an image ad is {r_['image']} credits, an 8-second talking video {r_['talk8']:,}" in ht, "the lede prices come from the rate card")
check('data-cta="hero">Make 2 ads with your free credits</a>' in home and 'href="#step-plan">See a finished plan' in home,
      "CTA 'Make 2 ads with your free credits' and 'See a finished plan' to the plan step")
check(f"come with a plan, from ${STARTER['usd']} monthly" in ht and "Nothing is charged." in ht,
      "the subline names the plan price (from plans) and what sign-up costs (card step off)")

# ================================================================ pricing: one sum
print("== pricing: one sum on /pricing and Billing")


def usd(x):
    return f"${x:.2f}" if x < 1 else (f"${round(x, 1):.2f}".replace(".00", "") if x < 10 else f"${round(x)}")


pr = page(client(), "/pricing")
prt = text(pr)
check("<h1>Pricing</h1>" in pr and "Every plan is a monthly credit allowance plus the campaign tools." in prt,
      "/pricing: h1 'Pricing' and the plain lede")
lines = {}
for p in plans.PAID:
    counts = (f"About {p['credits'] // r_['image']:,} image ads, or {p['credits'] // r_['video5']:,} product videos, "
              f"or {p['credits'] // r_['talk8']:,} talking videos, in any mix, each month.")
    lines[p["code"]] = counts
    check(counts in prt, f"/pricing {p['name']}: {counts}")
check("Per finished ad" not in prt and "Image ad about" not in prt, "/pricing: counts only, no dollars per ad (Koby, Nov 8)")
check(f"Start {STARTER['name']}, ${STARTER['usd']}/mo" not in pr and "Not open yet" in pr and "Opens soon" not in pr,
      "no Stripe: plan buttons say 'Not open yet' with a reason, not 'Opens soon'")
check("Plan checkout isn't switched on yet." in prt, "and say why")
check("44 credits to start: 2 image ads on your own product. Nothing is charged." in prt, "Free card: what sign-up gives")

u = signup("billing-copy@example.com")
bill = page(u, "/app/billing")
bt = text(bill)
for code, counts in lines.items():
    check(counts in bt, f"Billing {plans.PLANS[code]['name']}: the same line as /pricing")
check("Per finished ad" not in bt and "Image ad about" not in bt, "Billing: counts only, no dollars per ad")
check("≈" not in bt.split("Plans")[1], "Billing plan cards: no separate '≈ N videos' sum")
check('placeholder="Code from our email"' in bill and "CFY-XXXX-XXXX" not in bill, "code box placeholder: 'Code from our email'")
r = u.post("/app/billing/code", data={"csrf": csrf(u), "code": "trial30"}, follow_redirects=True)
rt = text(r.get_data(as_text=True))
check("We couldn't find TRIAL30. Check the spelling, or enter it on the checkout page." in rt and "CFY-ABCD" not in rt,
      "an unknown discount code: 'We couldn't find TRIAL30', not 'it looks like CFY-...'")
r = u.post("/app/billing/code", data={"csrf": csrf(u), "code": "CFY-ZZZZ-ZZZZ"}, follow_redirects=True)
check("know that code" in r.get_data(as_text=True), "a free-month code shape still goes to the free-month codes")

# ================================================================ auth pages, empty state
print("== sign-up, auth, library")
for path in ("/signup", "/login", "/forgot"):
    h = page(client(), path)
    check("creatives" not in h.lower(), f"{path}: no 'creatives'")
su = page(client(), "/signup")
sut = text(su)
check("Ads from your product page" in su and ">Create my account</button>" in su and "Password, 8 or more characters" in su,
      "sign-up: tagline, 'Create my account', password label")
check(f"Pick a look. {len(catalog.first_run_templates())} templates, brief written for you. Your 2 ads are ready in about a minute." in sut
      and "Then the campaign." in sut, "sign-up steps: the template count comes from the catalog")
check("Nothing is charged and nothing renews. The 44 free credits never expire." in sut, "sign-up footer (card step off)")

fresh = page(u, "/app/library")
check("Your ads will live here" in fresh and 'class="fchips"' not in fresh and "Make 2 ads with your free credits" in fresh,
      "first Library: the empty state, no filter rows, the free-ads button")
check("Your ads will live here" not in page(u, "/app/library?status=failed"), "a filtered empty Library keeps the small line")
check("Start from a brief we wrote; edit anything." in open(os.path.join(HERE, "..", "web", "studio.html")).read(),
      "Home templates line makes no 'already work' claim")

# ================================================================ 404
print("== 404")
r = client().get("/nope-not-here")
nt = text(r.get_data(as_text=True))
check(r.status_code == 404 and "That page isn't here." in nt and "It may have moved, or the link was cut short." in nt
      and "requested URL was not found" not in nt, "404: our words, not Werkzeug's")
h = page(u, "/app/nope")
check("That page isn't here." in h and 'href="/app/library"' in h and 'href="/app"' in h, "404 signed in: Home and Library")
r = client().get("/p/no-such-page")
check(r.status_code == 404 and "That page isn't here." in r.get_data(as_text=True), "an unknown /p/ page: the same 404")

# ================================================================ dashes
for path, c in (("/", client()), ("/pricing", client()), ("/signup", client()), ("/nope", client()),
                ("/app/billing", u), ("/app/library", u)):
    t = text(page(c, path))
    check(not any(d in t for d in DASHES), f"{path}: no em or en dashes")
for name in ("landing.html", "pricing.html", "_pricing.html", "signup.html", "auth_base.html", "error.html",
             "billing.html", "library.html", "product_edit.html"):
    txt = open(os.path.join(HERE, "..", "web", name)).read()
    check(not any(d in txt for d in DASHES), f"web/{name}: no em or en dashes")

# ================================================================ L-4: the card step on
print("== card step on: the copy says so")
os.environ.update(RS_CARD_REQUIRED="1", STRIPE_SECRET_KEY="sk_test_copy", STRIPE_WEBHOOK_SECRET="whsec_copy")
LINE = "Card needed to start, never charged until you pick a plan or buy a pack. One free trial per person."
for path in ("/", "/pricing", "/signup", "/features", "/ai-ugc-ad-generator"):
    t = text(page(client(), path))
    check(LINE in t and "No card" not in t, f"{path}: '{LINE[:40]}...' and no 'No card'")
t = text(page(client(), "/llms.txt"))
check(t.count(LINE) == 2 and "No card" not in t, "llms.txt: the card line in both places")
t = text(page(client(), "/arcads-alternative"))
check("A card is needed at sign-up, but nothing is charged until you pick a plan or buy a pack. Free sign-up includes 44 "
      "credits (2 image ads). One free trial per person." in t, "Arcads table: our 'Trying it' cell says a card is needed too")
t = text(page(client(), "/predis-ai-alternative"))
check("A card is needed at sign-up, but nothing is charged until you pick a plan or buy a pack, and you start" in t
      and "One free trial per person." in t, "Predis.ai: no contrast with its card at sign-up")
for slug in ("blaze-alternative", "heygen-alternative", "topview-alternative", "makeugc-alternative",
             "adcreative-ai-alternative", "creatify-alternative"):
    s = public_site.solution(slug)
    ours = s["table"]["rows"][0][2]
    check("card" in ours.lower() and "One free trial per person." in ours, f"{slug}: our 'Trying it' cell names the card")
    check(all("No. A card is needed at sign-up" in a for q, a in s.get("compare", []) if "trial" in q.lower()),
          f"{slug}: the trial question says a card is needed")
g = public_site.guide("best-ai-ugc-ad-generators")
ours = next(row for row in g["table"]["rows"] if row[0] == "Campaignify")
check("card needed to start" in ours[2], "best AI UGC tools table: Campaignify's free column says a card is needed")
g = public_site.guide("credit-based-ai-ad-pricing")
check(any("card needed to start" in x for _, _, blocks in g["sections"] for b in blocks if isinstance(b, tuple)
          for x in (b[1] if isinstance(b[1], list) else [b[1]])), "credit pricing guide: free sign-up names the card")
for key in ("signup_charge", "nothing_charged", "with_nothing_charged"):
    check("card" in public_site.facts()[key].lower(), f"facts()[{key!r}] names the card")
os.environ.pop("RS_CARD_REQUIRED")
t = text(page(client(), "/arcads-alternative"))
check("Card needed" not in t and "card is needed" not in t and "One free trial per person" not in t,
      "card step off again: no card copy")

# ================================================================ M5.7: public pages and metadata
print("== public pages and metadata (M5.7)")
import json  # noqa: E402

import db  # noqa: E402
import pages as pagesmod  # noqa: E402
from PIL import Image  # noqa: E402

with db.connect() as conn:
    u2 = conn.execute("SELECT * FROM users WHERE email='billing-copy@example.com'").fetchone()
    ws_id = u2["workspace_id"]
    now = db.now()
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)", (ws_id, u2["id"], "ad_image", "done", now, now)).lastrowid
    img = os.path.join(TMP, "hero.png")
    Image.new("RGB", (864, 1080), (200, 120, 60)).save(img)
    vid = os.path.join(TMP, "clip.mp4")
    open(vid, "wb").write(b"not really a video")
    Image.new("RGB", (720, 1280), (40, 40, 40)).save(vid + ".jpg")
    oi = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "image", img)).lastrowid
    ov = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "video", vid)).lastrowid

    def mkpage(slug, blocks, description=""):
        conn.execute("INSERT INTO pages (workspace_id, slug, title, description, status, blocks_json, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (ws_id, slug, "Ember Candle", description, "published", json.dumps(blocks), now, now))
    mkpage("og-hero", [{"type": "hero", "headline": "Light it once", "subhead": "A soy candle with amber and cedar.", "media": oi}])
    mkpage("og-video", [{"type": "hero", "headline": "Watch it", "subhead": "", "media": None},
                        {"type": "media", "media": ov, "caption": ""}], description="Our own words.")
    mkpage("og-none", [{"type": "hero", "headline": "No media", "subhead": "", "media": None}])
    conn.commit()

r = client().get("/p/og-hero")
h = r.get_data(as_text=True)
check(f'<meta property="og:image" content="http://localhost/p/og-hero/m/{oi}">' in h
      and '<meta property="og:image:width" content="864">' in h and '<meta property="og:image:height" content="1080">' in h
      and 'name="twitter:card" content="summary_large_image"' in h, "/p/ page: og:image from the hero image, with its size (SEO-1)")
check('<meta property="og:description" content="A soy candle with amber and cedar.">' in h,
      "no description typed: og:description falls back to the hero subheadline")
check(f'src="/p/og-hero/m/{oi}" alt="Ember Candle" width="864" height="1080"' in h,
      "the hero image carries its own size (SEO-3) and the page title as alt (A11Y-7)")
check(client().get(f"/p/og-hero/m/{oi}").status_code == 200, "and the share image is public")
h = page(client(), "/p/og-video")
check(f'content="http://localhost/p/og-video/m/{ov}?poster=1"' in h and 'og:image:width" content="720"' in h
      and 'og:description" content="Our own words."' in h, "a video page shares its poster frame; a typed description wins")
check("og:image" not in page(client(), "/p/og-none"), "no media: no og:image (never a broken card)")
csp = client().get("/p/og-hero").headers["Content-Security-Policy"]
check("font-src 'self'" in csp and "gstatic" not in csp and "googleapis" not in csp,
      "public and member pages allow the self-hosted fonts (SEO-2)")
check("font-src 'self'" in pagesmod.CSP, "one CSP for pages, forms and communities")

TAG = "googletagmanager.com/gtag/js"
os.environ["GADS_TAG_ID"] = "AW-18543041154"
check(TAG in page(client(), "/") and TAG in page(client(), "/signup"), "the Google Ads tag loads on the public site and sign-up")
check(TAG in page(u, "/app") and TAG in page(u, "/app/billing"), "and on a free workspace's Home and on Billing (the conversion moments)")
for path in ("/app/library", "/app/products", "/app/create"):
    check(TAG not in page(u, path), f"not inside the app: {path} (SEO-4)")
os.environ.pop("GADS_TAG_ID")

import public_site as ps  # noqa: E402
os.environ["MCP_SERVER"] = "on"
check("/docs/mcp" in ps.sitemap_paths() and "/docs/mcp" in page(client(), "/sitemap.xml"), "/docs/mcp is in the sitemap when the MCP server is on (SEO-5)")
d = page(client(), "/docs/mcp")
check('content="index, follow, max-image-preview:large"' in d and '<link rel="canonical" href="https://campaignify.ai/docs/mcp">' in d
      and "application/ld+json" in d and 'property="og:image"' in d, "/docs/mcp has the shared head: directives, canonical, share card, JSON-LD")
check(d.count('rel="canonical"') == 1 and d.count('name="description"') == 1, "one canonical and one description")
os.environ.pop("MCP_SERVER")
check("/docs/mcp" not in ps.sitemap_paths() and 'content="noindex"' in page(client(), "/docs/mcp"),
      "MCP server off: /docs/mcp stays out of the sitemap and noindex")
css = open(os.path.join(HERE, "..", "static", "style.css")).read()
check(".lp-doc pre{white-space:pre-wrap" in css, "/docs/mcp code blocks wrap on a phone")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
