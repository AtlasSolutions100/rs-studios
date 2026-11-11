"""The public site's SEO and the Google Ads tag on a throwaway database: titles and
descriptions, canonical URLs, share cards, JSON-LD, the sitemap, noindex on private
routes, internal links, the answer-first pages, first-touch attribution and the
sign_up / purchase conversions. Stripe is faked; nothing leaves the machine.

    python tests/test_public_site.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_site_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_test_only")
# Live prices (test_app.py's fixtures pin older vendor rates), so the sign-up offer copy matches the site.
for k in ("GADS_SIGNUP_LABEL", "GADS_PURCHASE_LABEL", "GADS_TAG_ID", "RS_SITE_URL", "RS_VIDEO_USD_PER_SECOND",
          "RS_TALKING_USD_PER_SECOND", "RS_MARKUP", "RS_STARTER_CREDITS", "SIGNUP_CREDITS_ADS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import gads  # noqa: E402
import plans  # noqa: E402
import public_site  # noqa: E402

stripe = {}  # Stripe API reads (billing._get) by path; nothing goes to Stripe


def fake_get(path, params=None):
    if path in stripe:
        if isinstance(stripe[path], Exception):
            raise stripe[path]
        return stripe[path]
    return {"data": []}


billing._get = fake_get
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client(**headers):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    for k, v in headers.items():
        c.environ_base["HTTP_" + k.upper().replace("-", "_")] = v
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


class Page(HTMLParser):
    """The bits of a page SEO cares about."""

    def __init__(self, html):
        super().__init__()
        self.title, self.meta, self.links, self.h1, self.imgs, self.hrefs, self.ids = "", {}, {}, 0, [], [], set()
        self.ld, self.summaries, self._in, self._buf = [], [], None, ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "title":
            self._in, self._buf = "title", ""
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and a.get("rel"):
            self.links[a["rel"]] = a.get("href", "")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in, self._buf = "ld", ""
        elif tag == "details":
            self._plan = "plan-more" in (a.get("class") or "")   # the pricing cards' feature accordion isn't an FAQ
        elif tag == "summary" and not getattr(self, "_plan", False):
            self._in, self._buf = "summary", ""

    def handle_data(self, data):
        if self._in:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "title" and self._in == "title":
            self.title = self._buf.strip()
        elif tag == "script" and self._in == "ld":
            self.ld.append(self._buf)
        elif tag == "summary" and self._in == "summary":
            self.summaries.append(self._buf.strip())
        else:
            return
        self._in = None


def graph(page):
    nodes = []
    for raw in page.ld:
        doc = json.loads(raw)
        nodes += doc.get("@graph", [doc])
    return nodes


def node(nodes, typ):
    return next((n for n in nodes if n.get("@type") == typ), None)


GTAG_EVENT = re.compile(r"gtag\('event', 'conversion', (\{.*?\})\);")


def conversions(html):
    return [json.loads(m) for m in GTAG_EVENT.findall(html)]


SITE = "https://campaignify.ai"
pub = client()

print("== titles, descriptions, canonical, share cards")
paths = public_site.sitemap_paths()
check(paths[:3] == ["/", "/features", "/pricing"] and len(paths) == 42 and "/for-reps" in paths and "/llm-info" in paths
      and "/guides" in paths and "/best-ai-ugc-ad-generators" in paths,
      "the sitemap list: home, features, pricing, 25 answer-first pages, guides and the roundup, the hook tool, for reps, llm-info, "
      "privacy, terms, messaging policy")
pages = {}
for p in paths:
    r = pub.get(p)
    check(r.status_code == 200 and "X-Robots-Tag" not in r.headers, f"{p} renders and isn't noindexed")
    pages[p] = Page(r.get_data(as_text=True))
titles = [pg.title for pg in pages.values()]
descs = [pg.meta.get("description", "") for pg in pages.values()]
check(len(set(titles)) == len(titles) and all(0 < len(t) <= 60 for t in titles), "every public title is unique and ≤ 60 characters")
check(len(set(descs)) == len(descs) and all(50 <= len(d) <= 155 for d in descs), "every meta description is unique and ≤ 155 characters")
for p, pg in pages.items():
    url = SITE + p
    check(pg.links.get("canonical") == url and pg.meta.get("og:url") == url, f"{p}: canonical and og:url are {url}")
    check(pg.meta.get("robots", "").startswith("index") and pg.h1 == 1, f"{p}: indexable, exactly one H1")
    check(pg.meta.get("og:title") == pg.title and pg.meta.get("twitter:card") == "summary_large_image"
          and pg.meta.get("og:image") == SITE + "/static/img/og-campaignify.png" and pg.meta.get("twitter:image")
          and pg.meta.get("og:image:alt"), f"{p}: Open Graph and Twitter card with the share image")
    check(all((im.get("alt") is not None) for im in pg.imgs), f"{p}: every image has alt text")
from PIL import Image  # noqa: E402
with Image.open(os.path.join(ROOT, "static", public_site.OG_IMAGE)) as im:
    check(im.size == (1200, 630) and im.format == "PNG", "the share image is a 1200×630 PNG")
h1s = {re.sub(r"<[^>]+>", "", re.search(r"<h1[^>]*>(.*?)</h1>", pub.get(p).get_data(as_text=True), re.S).group(1))
       for p in paths}
check(len(h1s) == len(paths), "no two public pages share an H1")

print("== JSON-LD")
for p, pg in pages.items():
    nodes = graph(pg)    # raises if any block isn't valid JSON
    check(node(nodes, "Organization") and node(nodes, "WebPage")["url"] == SITE + p, f"{p}: JSON-LD parses, Organization + WebPage")
home = graph(pages["/"])
app_node = node(home, "SoftwareApplication")
prices = {o["name"]: o["price"] for o in app_node["offers"]}
check(prices == {"Free": "0", **{f"{p['name']} plan": str(p["usd"]) for p in plans.PAID}},
      "SoftwareApplication offers come from plans.py (Free, Starter, Growth, Scale)")
saved = plans.PLANS["starter"]["usd"]
plans.PLANS["starter"]["usd"] = 41
nodes = graph(Page(pub.get("/pricing").get_data(as_text=True)))
check(any(o["price"] == "41" for o in node(nodes, "SoftwareApplication")["offers"])
      and "$41" in Page(pub.get("/pricing").get_data(as_text=True)).title, "change a plan's price and the schema and the pricing title follow")
plans.PLANS["starter"]["usd"] = saved
for p in ["/", "/pricing"] + [f"/{s}" for s in public_site.SOLUTION_ORDER]:
    faq = node(graph(pages[p]), "FAQPage")
    qs = [q["name"] for q in faq["mainEntity"]]
    check(qs and qs == pages[p].summaries and all(q["acceptedAnswer"]["text"] for q in faq["mainEntity"]),
          f"{p}: FAQPage matches the visible FAQ")
check(all(node(graph(pages[p]), "FAQPage") is None for p in ("/privacy", "/terms", "/features")),
      "no FAQPage where there's no visible FAQ")
check("[terms](" not in pages["/"].ld[0] and "See the terms." in pages["/"].ld[0], "FAQ answers in JSON-LD are plain text")
check("</" not in "".join(pg.ld[0] for pg in pages.values()), "JSON-LD can't close its script tag")

print("== sitemap, robots, llms.txt")
sm = pub.get("/sitemap.xml").get_data(as_text=True)
locs = re.findall(r"<loc>(.*?)</loc>", sm)
check(locs == [SITE + p for p in paths], "sitemap.xml lists exactly the public pages on https://campaignify.ai")
mods = re.findall(r"<lastmod>(\d{4}-\d{2}-\d{2})</lastmod>", sm)
check(len(mods) == len(paths) and len(set(mods)) == 1 and mods[0] == public_site.lastmod(), "every sitemap entry carries a <lastmod> date")
check(not any(x in sm for x in ("/app", "/admin", "/s/", "/login", "/signup", "/p/", "/l/")),
      "the sitemap leaves out the app, admin, share links, auth and members' pages")
rt = pub.get("/robots.txt").get_data(as_text=True)
check(f"Sitemap: {SITE}/sitemap.xml" in rt and "Disallow: /app" in rt and "Disallow: /admin" in rt,
      "robots.txt points at the sitemap and keeps the app out")
lt = pub.get("/llms.txt").get_data(as_text=True)
check(all(f"{SITE}/{s}" in lt for s in public_site.SOLUTION_ORDER) and f"{SITE}/pricing" in lt and "$39 a month" in lt,
      "llms.txt describes the product and links every public page")
rr = public_site.catalog.rates()
check(f"Image ad: {rr['image']} credits" in lt and f"{rr['talk8']} credits for 8 seconds" in lt and f"{rr['video5']} credits for 5 seconds" in lt
      and f"{rr['motion_per_s']} credits per second" in lt, "llms.txt lists the credit rate of every tool")
li = pub.get("/llm-info").get_data(as_text=True)
check(f"Image ad: {rr['image']} credits" in li and "What each one costs" in li and f'href="{SITE}/pricing"' in li
      and "<h2" in li, "/llm-info mirrors llms.txt as HTML with headings and links")

print("== internal links")
seen = {}
for p, pg in pages.items():
    for href in pg.hrefs:
        u = urlsplit(href)
        if u.scheme in ("mailto", "tel") or (u.netloc and u.netloc != "campaignify.ai"):
            continue
        path = u.path or p
        if path not in seen:
            seen[path] = pub.get(path, headers={"Accept": "text/html,application/xhtml+xml"})   # as a browser asks (/mcp is a page only then)
        r = seen[path]
        check(r.status_code == 200, f"{p} → {href} resolves ({r.status_code})") if r.status_code != 200 else None
        if u.fragment:
            target = pages.get(path) or Page(r.get_data(as_text=True))
            if u.fragment not in target.ids:
                check(False, f"{p} → {href}: #{u.fragment} exists")
check(len(seen) >= 12, f"every internal link on the public pages resolves, anchors included ({len(seen)} paths)")
foot = set(pages["/privacy"].hrefs)
check(all(f"/{s}" in foot for s in public_site.SOLUTION_ORDER) and "/pricing" in foot, "the footer links every answer-first page and pricing")
for s in public_site.SOLUTION_ORDER:
    others = [o for o in public_site.SOLUTION_ORDER if o != s]
    related = public_site.SOLUTIONS[s]["related"]
    body = pub.get(f"/{s}").get_data(as_text=True)
    rel_hrefs = [public_site.card(o)["href"] for o in related]
    check(all(body.count(f'href="/{o}"') >= 1 for o in others) and all(body.count(f'href="{h}"') >= 2 for h in rel_hrefs)
          and 'href="/signup"' in body, f"/{s} links every other answer-first page (footer) and its related three in the body, and sign-up")

print("== answer-first pages")
for s in public_site.SOLUTION_ORDER:
    sol = public_site.solution(s)
    check(len(sol["steps"]) == 3 and 3 <= len(sol["faq"]) <= 5 and (sol.get("gets") or sol.get("compare"))
          and not re.search(r"\{[a-z_0-9]+\}", json.dumps(sol)), f"/{s}: three steps, what you get, 3-5 FAQs, every number filled in")
print("== comparison pages: every competitor fact sourced and dated")
for s in ("arcads-alternative", "creatify-alternative", "makeugc-alternative", "adcreative-ai-alternative"):
    sol = public_site.solution(s)
    t = sol["table"]
    body = pub.get(f"/{s}").get_data(as_text=True)
    sec = body.split('<section class="lp-sec" id="compare">')[1].split("</section>")[0]
    check(t["source"].startswith("https://") and re.match(r"\d{4}-\d{2}-\d{2}$", t["date"]) and len(t["rows"]) == 3
          and sec.count(t["source"]) >= 2 and f"as of {t['date']}" in sec.lower() and f"isn't affiliated with {t['competitor']}" in sec,
          f"/{s}: a 3-row table, the competitor's column sourced with a URL and an 'as of' date, no affiliation")
    check(sol["h1"].startswith("Campaignify is a") and "alternative" in sol["h1"], f"/{s}: a declarative H1")
    check(any("not listed" in row[1] or "not stated" in row[1] for row in t["rows"]) or s == "arcads-alternative",
          f"/{s}: absences are written as 'not listed', never guessed")
arc = public_site.solution("arcads-alternative")["table"]
check(arc["source"] == "https://help.arcads.ai/en/articles/17595066-how-does-arcads-pricing-work"
      and "72" in arc["rows"][0][1] and "card is required" in arc["rows"][0][1] and "Starter monthly plan starts automatically" in arc["rows"][0][1]
      and "Not listed on the public site" in arc["rows"][1][1] and "800 credits" in arc["rows"][2][1]
      and "Nothing is charged at sign-up" in arc["rows"][0][2] and "740" in arc["rows"][2][2],
      "Arcads: the 72-hour card trial, unpublished prices and 800 credits per talking video, against nothing charged and 740 credits")
check("Higgsfield" not in "".join(pub.get(p).get_data(as_text=True) for p in paths), "the vendor isn't named on the public site")
for s in ("ai-ugc-ad-generator", "ai-product-ads-for-shopify", "ai-marketing-platform-for-small-business"):
    body = pub.get(f"/{s}").get_data(as_text=True)
    hero = body.split('<section class="lp-sec" id="how">')[0]
    check("sol-clips" in hero and hero.count("<video") == 3 and "lp-snd" in hero and ".jpg" in hero and "740 credits" in hero
          and "22 credits" in hero and "Shown before you press Generate" in hero,
          f"/{s}: the hero plays three clips with a sound button and shows the credit prices under the H1")
shop = pub.get("/ai-product-ads-for-shopify").get_data(as_text=True)
check("Sidekick writes your descriptions. Campaignify makes the ads." in shop and "Shopify ad creator" in shop
      and "product video for Shopify" in shop and "photo_to_video.mp4" in shop,
      "the Shopify page: the Sidekick line, the 'Shopify ad creator' and 'product video for Shopify' phrases, a product-video clip")
for s in public_site.SOLUTION_ORDER:
    body = pub.get(f"/{s}").get_data(as_text=True)
    h2s = re.findall(r'<h2 class="lp-h2">(.*?)</h2>', body)
    q = {"ai-ugc-ad-generator": "ugc ad generator", "ai-product-ads-for-shopify": "shopify", "ai-ad-copy-generator": "ad copy generator",
         "ai-marketing-platform-for-small-business": "small business"}.get(s, s.split("-")[0])
    check(sum(q in h.lower() for h in h2s) >= 2, f"/{s}: the H2s carry the page's keyword ({q})")
print("== the small business page")
SMB = "/ai-marketing-platform-for-small-business"
smb_pg, smb = pages[SMB], pub.get(SMB).get_data(as_text=True)
smb_sol = public_site.solution(SMB[1:])
import html as _html  # noqa: E402
smb_text = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", smb.split("<main")[1].split('<footer')[0])))
rr = public_site.catalog.rates()
check(smb_pg.title == "AI Marketing Platform for Small Business · Campaignify"
      and re.search(r"<h1[^>]*>AI marketing platform for small business</h1>", smb)
      and "small business" in smb_pg.meta["description"].lower() and "$39 a month" in smb_pg.meta["description"],
      f"{SMB}: the keyword in the title, the H1 and the description, with the plan price")
check("local shops, service businesses and small online stores" in smb_text and "without hiring an agency" in smb_text,
      f"{SMB}: answers who it's for first (local shops, service businesses, small online stores)")
check("You launch them in each platform's ads manager" in smb_text and "coming soon, starting with Meta" in smb_text
      and "Does Campaignify post or run my ads? No." not in smb_text,
      f"{SMB}: says you launch the ads in each platform's ads manager, and that launching from Campaignify is coming")
check(not re.search(r"\b(we|campaignify|it) (post|posts|run|runs|publish|publishes) (your|the) ads\b", smb_text, re.I),
      f"{SMB}: never claims the app posts or runs ads")
check(f"{public_site.catalog.starter_credits()} credits, enough for 2 image ads" in smb_text
      and all(f"{rr[k]} credits" in smb_text for k in ("image", "talk8")) and f"product video {rr['video5']}" in smb_text,
      f"{SMB}: free sign-up with 2 image ads (44 credits) and per-item costs in credits")
plan_usd = {str(p["usd"]) for p in plans.PAID} | {str(k["usd"]) for k in public_site.catalog.PACKS}
check(set(re.findall(r"\$(\d+)", smb_text)) <= plan_usd and "$39" in smb_text,
      f"{SMB}: dollars only on plan and pack prices, everything else in credits")
for word, feat in (("copy studio", "copy"), ("campaign planner", "planner"), ("smart links", "links"), ("Flows", "flows"),
                   ("forms", "forms"), ("SEO & AEO optimizer", "seo"), ("ad-account setup hub", "adhub"),
                   ("email sending in flows", "flows_email"), ("landing page", "pages")):
    check(word in smb_text and plans.FEATURES[feat]["shipped"], f"{SMB}: mentions {word}, a shipped feature")
check(not any(w in smb_text.lower() for w in ("testimonial", "customers love", "trusted by", "5-star", "rated "))
      and "doesn't promise sales, leads or views" in smb_text, f"{SMB}: no testimonials or result claims")
check("\u2014" not in json.dumps(smb_sol, ensure_ascii=False), f"{SMB}: no em dashes in the copy")
check(smb.count('href="/ai-ugc-ad-generator"') >= 2 and f'href="{SMB}"' in pub.get("/").get_data(as_text=True),
      f"{SMB}: linked from the home footer and links its related pages")
still = pub.get("/arcads-alternative").get_data(as_text=True)
check('srcset="/static/site/ugc-bakery-sm.jpg 270w, /static/site/ugc-bakery.jpg 540w"' in still and 'sizes="' in still,
      "presenter stills carry a srcset with a 270px version")
check("Higgsfield" not in "".join(pub.get(p).get_data(as_text=True) for p in paths), "the vendor isn't named on the public site")

print("== search engine verification, IndexNow, llms-full.txt, dashes")
home_html = pub.get("/").get_data(as_text=True)
check('<meta name="google-site-verification" content="rbYLQRwbrfockDcbnBLH-pYYgBUvzXchP2Ani4Zuw_M">' in home_html
      and '<meta name="google-site-verification" content="TaTQ1t3pKtoG4y_te9_Z-e2mTFW8uoyhw4Yw6z6yE8E">' in home_html
      and '<meta name="msvalidate.01" content="97386D3157EE9C0604AE6D8A368F083C">' in home_html,
      "the home page carries both Google Search Console tags and the Bing verification tag")
key = public_site.INDEXNOW_KEY
kr = pub.get(f"/{key}.txt")
check(re.fullmatch(r"[0-9a-f]{8,128}", key) and kr.status_code == 200 and kr.get_data(as_text=True) == key
      and kr.mimetype == "text/plain", "the IndexNow key file is served at /<key>.txt with just the key")
import indexnow  # noqa: E402
urls = indexnow.sitemap_urls(sm)
body = public_site.indexnow_body(urls + ["https://elsewhere.example/x"])
check(urls == [SITE + p for p in paths] and body["host"] == "campaignify.ai" and body["key"] == key
      and body["keyLocation"] == f"{SITE}/{key}.txt" and body["urlList"] == urls,
      "the IndexNow ping sends every sitemap URL (and only ours) with the key location")
lf = pub.get("/llms-full.txt")
lft = lf.get_data(as_text=True)
check(lf.status_code == 200 and lft.startswith("# Campaignify") and all(f"{SITE}/{s}" in lft for s in public_site.SOLUTION_ORDER)
      and all(f"Q: {q}" in lft for q, _ in public_site.home_faq()) and "[terms](" not in lft,
      "llms-full.txt: the summary plus every answer-first page's full text and FAQ, as plain text")
check(f"{SITE}/llms-full.txt" in lt and "Manage or cancel plan" in lt and "hello@campaignify.ai" in lt
      and "## Who it's for" in lt and all(SITE + p in lt for p in paths if p != "/llm-info") ,
      "llms.txt: who it's for, how to cancel, contact, and a link to every sitemap page")
for p, pg in pages.items():
    nodes = graph(pg)
    check(not any(k in json.dumps(nodes) for k in ('aggregateRating', '"Review"', '"reviewCount"')),
          f"{p}: no ratings or reviews in the schema")
    if p != "/":
        check(node(nodes, "BreadcrumbList") and node(nodes, "WebSite"), f"{p}: WebSite and BreadcrumbList")
check(node(graph(pages["/pricing"]), "SoftwareApplication")["offers"], "pricing carries SoftwareApplication offers")
for p in ("/features", "/pricing", "/for-reps"):
    b = pub.get(p).get_data(as_text=True)
    check("Related pages" in b and all(f'href="{public_site.card(k)["href"]}"' in b for k in public_site.RELATED[p.strip("/").replace("-", "_")]),
          f"{p}: a related-pages block")
for p in paths + ["/llms.txt", "/llms-full.txt"]:
    t = pub.get(p).get_data(as_text=True)
    if p.endswith(".txt"):
        text = t
    else:
        head = t.split("</head>")[0]
        text = t.split("<main", 1)[1].split("<script>", 1)[0] + " ".join(re.findall(r'content="([^"]*)"', head)) + head.split("<title>")[1].split("</title>")[0]
        text += " ".join(Page(t).ld)
    check("\u2014" not in text and "\u2013" not in text, f"{p}: no em or en dashes in the public copy")

print("== SEO launch 2: comparison, use-case and guide pages")
import html as _h  # noqa: E402
import public_pages  # noqa: E402


def main_text(path):
    t = pub.get(path).get_data(as_text=True)
    m = re.sub(r"<script.*?</script>", "", t.split("<main", 1)[1].split("<footer")[0], flags=re.S)
    return _h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m)))


new_paths = ([f"/{x}" for x in public_pages.SOLUTION_ORDER] + ["/guides"]
             + [public_site.guide_path(k) for k in public_site.GUIDE_ORDER])
check(len(new_paths) == 25 and all(p in paths for p in new_paths), "25 public pages from public_pages.py, all in the sitemap")
for p in new_paths:
    txt = main_text(p)
    n = len(txt.split())
    check(p == "/guides" or 600 <= n <= 1400, f"{p}: {n} words of real copy")
    check(not re.search(r"Rector|Stull|MarketPlace Global|\bISR\b|#1\b|Higgsfield", txt), f"{p}: stand-alone, no #1 claims")
    check(not re.search(r"\{[a-z_0-9]+\}", txt), f"{p}: every number filled in")
LEGAL = {"/terms", "/privacy", "/messaging-policy"}
for p in paths:
    if p in LEGAL:
        continue
    html = pub.get(p).get_data(as_text=True)
    check("Rector" not in html and "Stull" not in html, f"{p}: stand-alone, no Rector & Stull in the page or its JSON-LD")
for p in ("/llms.txt", "/llms-full.txt"):
    check("Rector" not in pub.get(p).get_data(as_text=True), f"{p}: stand-alone, no Rector & Stull")
for slug in ("blaze-alternative", "predis-ai-alternative", "heygen-alternative", "topview-alternative"):
    sol = public_site.solution(slug)
    t = sol["table"]
    sec = pub.get(f"/{slug}").get_data(as_text=True).split('<section class="lp-sec" id="compare">')[1].split("</section>")[0]
    check(t["source"].startswith("https://") and t["date"] == "2026-11-04" and len(t["rows"]) >= 4
          and f"as of {t['date']}" in sec.lower() and f"isn't affiliated with {t['competitor']}" in sec
          and all(u.startswith("https://") for _, u in t.get("sources", [])),
          f"/{slug}: competitor column sourced with a URL and an 'as of' date")
    check(sum("Nov 4, 2026" in r[1] for r in t["rows"]) >= 1 and sol["h1"].startswith("Campaignify is a"),
          f"/{slug}: prices 'as listed on Nov 4, 2026', a declarative H1")
    check(slug == "topview-alternative" or any("not listed" in r[1].lower() or "not mentioned" in r[1].lower() for r in t["rows"]),
          f"/{slug}: absences written as 'not listed', never guessed")
blz = public_site.solution("blaze-alternative")["table"]["rows"]
check("$79" in blz[1][1] and "$149" in blz[1][1] and "$999" in blz[1][1] and "No credit card required" in blz[0][1],
      "Blaze: the plan prices and trial terms from the research")
check("$19" in public_site.solution("predis-ai-alternative")["table"]["rows"][1][1]
      and "$29" in public_site.solution("heygen-alternative")["table"]["rows"][1][1]
      and "$29" in public_site.solution("topview-alternative")["table"]["rows"][1][1],
      "Predis.ai, HeyGen and Topview entry prices match the research")
agency = main_text("/ai-marketing-for-agencies")
check(plans.PLANS["growth"]["brands"] == 3 and plans.PLANS["scale"]["brands"] is None and "3 brands" in agency
      and "unlimited brands" in agency and plans.PLANS["scale"]["seats"] == 10 and "10 seats" in agency,
      "the agencies page matches the plan limits (3 brands on Growth, unlimited and 10 seats on Scale)")
check("Manage or cancel plan" in open(os.path.join(ROOT, "web", "billing.html")).read()
      and "Manage or cancel plan" in main_text("/guides/credit-based-ai-ad-pricing"),
      "self-serve cancel is real (Billing's button) and the pricing guide names it")
for k in public_site.GUIDE_ORDER:
    g = public_site.guide(k)
    pth = g["path"]
    pg = Page(pub.get(pth).get_data(as_text=True))
    nodes = graph(pg)
    art, faq, bc = node(nodes, "Article"), node(nodes, "FAQPage"), node(nodes, "BreadcrumbList")
    check(art and art["datePublished"] == "2026-11-04" and art["headline"] == g["h1"] and art["author"]["@id"].endswith("#org"),
          f"{pth}: Article schema with the publish date and the organization as author")
    check(faq and [q["name"] for q in faq["mainEntity"]] == pg.summaries and 3 <= len(g["faq"]) <= 5,
          f"{pth}: FAQPage matches the visible FAQ")
    check(bc and [i["name"] for i in bc["itemListElement"]][:2] == ["Home", "Guides"] and len(bc["itemListElement"]) == 3,
          f"{pth}: Home > Guides > page breadcrumbs")
    check(all(u.startswith("https://") for _, u in g["sources"]) and (not g["sources"] or "Sources" in main_text(pth)),
          f"{pth}: every source is a link")
rnd = public_site.guide("best-ai-ugc-ad-generators")
check(rnd["table"]["rows"][0][0] == "Campaignify" and len(rnd["table"]["rows"]) == 8
      and "We make Campaignify" in main_text("/best-ai-ugc-ad-generators") and "as listed on Nov 4, 2026" in rnd["answer"],
      "the roundup includes Campaignify, discloses that we make it, and dates every price")
ftc = main_text("/guides/ftc-meta-rules-for-ai-ads")
check("not legal advice" in ftc and "martech.org" in pub.get("/guides/ftc-meta-rules-for-ai-ads").get_data(as_text=True)
      and "marketingdive.com" in pub.get("/guides/ftc-meta-rules-for-ai-ads").get_data(as_text=True),
      "the FTC and Meta guide cites its sources and says it isn't legal advice")
ix = pub.get("/guides").get_data(as_text=True)
check(all(f'href="{public_site.guide_path(k)}"' in ix for k in public_site.GUIDE_ORDER)
      and node(graph(Page(ix)), "ItemList"), "/guides lists every guide, with an ItemList")
foot_html = pub.get("/privacy").get_data(as_text=True)
check('aria-label="Compare"' in foot_html and 'aria-label="Guides"' in foot_html and 'href="/guides"' in foot_html
      and all(f'href="{p}"' in foot_html for p in new_paths), "the footer links every new page; the nav links the guides")
lt2 = pub.get("/llms.txt").get_data(as_text=True)
lf2 = pub.get("/llms-full.txt").get_data(as_text=True)
check(all(SITE + p in lt2 for p in new_paths) and all(public_site.guide(k)["sections"][0][0] in lf2 for k in public_site.GUIDE_ORDER)
      and "Sources (as listed on Nov 4, 2026)" in lf2, "llms.txt links every new page; llms-full.txt carries the guides")

print("== noindex on private routes")
u = client()
r = u.post("/signup", data={"csrf": csrf(u), "email": "seo@example.com", "password": "longpassword1"})
check(r.status_code == 302, "a new account")
for p in ("/app", "/app/billing", "/app/library", "/login", "/signup", "/admin", "/s/nope", "/media/1"):
    r = (u if p.startswith("/app") else pub).get(p)
    check("noindex" in r.headers.get("X-Robots-Tag", ""), f"{p} sends X-Robots-Tag noindex ({r.status_code})")
for p in ("/app", "/app/billing", "/login"):
    pg = Page((u if p.startswith("/app") else pub).get(p).get_data(as_text=True))
    check(pg.meta.get("robots", "").startswith("noindex") and "canonical" not in pg.links, f"{p} has a noindex robots meta and no canonical")

print("== Google tag")
TAG_JS = "https://www.googletagmanager.com/gtag/js?id=AW-18543041154"
for p in ("/", "/pricing", "/ai-ugc-ad-generator", "/privacy"):
    check(TAG_JS in pub.get(p).get_data(as_text=True), f"{p} loads the Google tag")
html = u.get("/app").get_data(as_text=True)
check(TAG_JS in html and "gtag('config', \"AW-18543041154\")" in html, "the app loads and configures the Google tag")
check("region:" in html and '"GB"' in html and "globalPrivacyControl" in html, "ad cookies start denied in the EEA/UK/CH and under GPC/DNT")
member_tpls = ("share.html", "page_public.html", "form_public.html", "c_base.html")
check(all("_gtag" not in open(os.path.join(ROOT, "web", t)).read() and "base.html" not in open(os.path.join(ROOT, "web", t)).read()
          for t in member_tpls), "share links (/s/) and members' pages, forms and communities never load the tag")
os.environ["GADS_TAG_ID"] = "off"
check("googletagmanager" not in pub.get("/").get_data(as_text=True), "GADS_TAG_ID=off drops the tag")
os.environ.pop("GADS_TAG_ID")

print("== conversion labels")
check(gads.label("sign_up") == "" and gads.send_to("purchase") == "", "no labels set: no conversions")
os.environ["GADS_SIGNUP_LABEL"] = "AW-18543041154/SiGnUpLabel_1"
os.environ["GADS_PURCHASE_LABEL"] = "bad label!"
check(gads.send_to("sign_up") == "AW-18543041154/SiGnUpLabel_1" and gads.label("purchase") == "",
      "a label is read from the whole send_to value; a malformed one is ignored")
os.environ.pop("GADS_SIGNUP_LABEL")
os.environ.pop("GADS_PURCHASE_LABEL")

print("== sign_up conversion")
c = client()
r = c.post("/signup", data={"csrf": csrf(c), "email": "nolabel@example.com", "password": "longpassword1"})
check(conversions(c.get(r.headers["Location"]).get_data(as_text=True)) == [], "labels unset: sign-up fires nothing")
os.environ["GADS_SIGNUP_LABEL"] = "SiGnUpLabel_1"
c = client()
check(conversions(c.get("/").get_data(as_text=True)) == [], "no conversion on a plain visit")
r = c.post("/signup", data={"csrf": csrf(c), "email": "label@example.com", "password": "longpassword1"})
with db.connect() as conn:
    uid = conn.execute("SELECT id FROM users WHERE email='label@example.com'").fetchone()[0]
first = conversions(c.get(r.headers["Location"]).get_data(as_text=True))
check(first == [{"send_to": "AW-18543041154/SiGnUpLabel_1", "transaction_id": f"signup-{uid}"}],
      "the first page after sign-up fires the sign_up conversion")
check(conversions(c.get("/app").get_data(as_text=True)) == [], "and only once")
c2 = client()
c2.post("/signup", data={"csrf": csrf(c2), "email": "bad@", "password": "x"})
check(conversions(c2.get("/signup").get_data(as_text=True)) == [], "a failed sign-up fires nothing")
os.environ.pop("GADS_SIGNUP_LABEL")
c3 = client()
r = c3.post("/signup", data={"csrf": csrf(c3), "email": "dropped@example.com", "password": "longpassword1"})
check(conversions(c3.get(r.headers["Location"]).get_data(as_text=True)) == [], "a label removed meanwhile fires nothing")

print("== purchase conversion")
sessions = []
billing.create_checkout_session = lambda **kw: sessions.append(kw) or {"id": f"cs_pack_{len(sessions)}",
                                                                       "url": "https://checkout.stripe.test/p"}
billing.create_subscription_session = lambda **kw: sessions.append(kw) or {"id": f"cs_plan_{len(sessions)}",
                                                                           "url": "https://checkout.stripe.test/s"}
b = client()
b.post("/signup", data={"csrf": csrf(b), "email": "buyer@example.com", "password": "longpassword1"})
with db.connect() as conn:
    bws = conn.execute("SELECT workspace_id FROM users WHERE email='buyer@example.com'").fetchone()[0]
r = b.post("/app/billing/checkout", data={"csrf": csrf(b), "pack": "starter"})
check(r.status_code == 303 and sessions[-1]["success_url"].endswith("checkout=success&session_id={CHECKOUT_SESSION_ID}"),
      "pack checkout returns with Stripe's session id")
html = b.get("/app/billing?checkout=success&session_id=cs_pack_1").get_data(as_text=True)
check(conversions(html) == [] and "googletagmanager" in html, "purchase label unset: the return page fires nothing")
with db.connect() as conn:
    check(conn.execute("SELECT reported_at FROM ad_conversions WHERE ref='cs_pack_1'").fetchone()[0] is None,
          "and the checkout stays unreported for later")
os.environ["GADS_PURCHASE_LABEL"] = "PuRcHaSe_2"
ev = conversions(b.get("/app/billing?checkout=success&session_id=cs_pack_1").get_data(as_text=True))
check(ev == [{"send_to": "AW-18543041154/PuRcHaSe_2", "value": 29.0, "currency": "USD", "transaction_id": "cs_pack_1"}],
      "pack checkout success fires purchase with value, currency and the session id as transaction_id")
check(conversions(b.get("/app/billing?checkout=success&session_id=cs_pack_1").get_data(as_text=True)) == [],
      "a reload doesn't fire it again")
other = client()
other.post("/signup", data={"csrf": csrf(other), "email": "other@example.com", "password": "longpassword1"})
b.post("/app/billing/checkout", data={"csrf": csrf(b), "pack": "pro"})
check(conversions(other.get("/app/billing?checkout=success&session_id=cs_pack_2").get_data(as_text=True)) == [],
      "another workspace can't fire someone else's checkout")
check(conversions(b.get("/app/billing?checkout=success&session_id=cs_madeup").get_data(as_text=True)) == []
      and conversions(b.get("/app/billing?checkout=success").get_data(as_text=True)) == [],
      "an unknown or missing session id fires nothing")
check(conversions(b.get("/app/billing?checkout=cancel&session_id=cs_pack_2").get_data(as_text=True)) == [],
      "a canceled checkout fires nothing")

r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "growth"})
sid = f"cs_plan_{len(sessions)}"
check(r.status_code == 303 and sessions[-1]["success_url"].endswith("plan=success&session_id={CHECKOUT_SESSION_ID}"),
      "plan checkout returns with Stripe's session id")
stripe[f"/checkout/sessions/{sid}"] = {"id": sid, "status": "complete", "payment_status": "paid", "amount_total": 4950,
                                       "currency": "usd", "metadata": {"app": "campaignify", "workspace_id": str(bws)}}
ev = conversions(b.get(f"/app/billing?plan=success&session_id={sid}").get_data(as_text=True))
check(ev == [{"send_to": "AW-18543041154/PuRcHaSe_2", "value": 49.5, "currency": "USD", "transaction_id": sid}],
      "plan checkout success fires purchase at what Stripe charged (a promotion code included)")
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "scale"})
sid2 = f"cs_plan_{len(sessions)}"
stripe[f"/checkout/sessions/{sid2}"] = billing.BillingError("Stripe 500")
ev = conversions(b.get(f"/app/billing?plan=success&session_id={sid2}").get_data(as_text=True))
check(ev and ev[0]["value"] == 249.0 and ev[0]["transaction_id"] == sid2, "Stripe unreachable: the plan's price is used")
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "starter"})
sid3 = f"cs_plan_{len(sessions)}"
stripe[f"/checkout/sessions/{sid3}"] = dict(stripe[f"/checkout/sessions/{sid}"], id=sid3,
                                            metadata={"app": "crowdcut", "workspace_id": str(bws)})
check(conversions(b.get(f"/app/billing?plan=success&session_id={sid3}").get_data(as_text=True)) == [],
      "a session that isn't ours (metadata) fires nothing")
os.environ.pop("GADS_PURCHASE_LABEL")

print("== first touch")
v = client()
r = v.get("/?utm_source=google&utm_medium=cpc&utm_campaign=ugc_launch&gclid=Cj0KCQ-abc_123")
ck = r.headers.get("Set-Cookie", "")
check(ck.startswith("cfy_ft=") and "HttpOnly" in ck and "SameSite=Lax" in ck and "Max-Age=7776000" in ck,
      "a landing with UTM tags and a gclid sets a 90-day first-party cookie")
r = v.get("/pricing?utm_source=newsletter&gclid=second")
check("cfy_ft=" not in r.headers.get("Set-Cookie", ""), "a later visit doesn't overwrite the first touch")
r = v.post("/signup", data={"csrf": csrf(v), "email": "touch@example.com", "password": "longpassword1"})
with db.connect() as conn:
    ft = json.loads(conn.execute("SELECT first_touch FROM users WHERE email='touch@example.com'").fetchone()[0])
check(ft["gclid"] == "Cj0KCQ-abc_123" and ft["utm_source"] == "google" and ft["utm_campaign"] == "ugc_launch"
      and ft["lp"] == "/", "sign-up saves the first touch (gclid, UTM tags, landing page) on the account")
check("cfy_ft=;" in r.headers.get("Set-Cookie", "") or "cfy_ft=\"\"" in r.headers.get("Set-Cookie", ""),
      "and clears the cookie")
w = client()
w.get("/features?utm_source=bing")
r = w.get("/ai-ugc-ad-generator?gclid=later_click")
check("gclid=later_click" in r.headers.get("Set-Cookie", "") and "utm_source=bing" in r.headers.get("Set-Cookie", ""),
      "a click id arriving after an organic first touch is added, keeping the first UTM tags")
for hdr in ({"Sec-GPC": "1"}, {"DNT": "1"}, {"CF-IPCountry": "DE"}):
    check("cfy_ft" not in client(**hdr).get("/?gclid=x1").headers.get("Set-Cookie", ""), f"no cookie with {hdr}")
check("cfy_ft" not in client().get("/l/nope?gclid=x2&utm_source=member").headers.get("Set-Cookie", ""),
      "members' smart links and pages keep their own click ids")
r = client().post("/signup", data={"email": "x"})
check("cfy_ft" not in r.headers.get("Set-Cookie", ""), "nothing on a POST")
x = client()
x.post("/signup", data={"csrf": csrf(x), "email": "organic@example.com", "password": "longpassword1"})
with db.connect() as conn:
    check(conn.execute("SELECT first_touch FROM users WHERE email='organic@example.com'").fetchone()[0] is None,
          "no first touch, nothing stored")

print("== free sign-up copy and the paid-ad offer (SIGNUP_CREDITS_ADS)")
import adoffer  # noqa: E402
import catalog  # noqa: E402

os.environ.pop("SIGNUP_CREDITS_ADS", None)
check(adoffer.credits() == 500 and catalog.starter_credits() == 44, "the ad offer defaults to 500; everyone else 44")
check(adoffer.paid_source({"gclid": "x"}) and adoffer.paid_source({"utm_medium": "CPC"})
      and adoffer.paid_source({"utm_medium": "paid_social"}) and adoffer.paid_source({"fbclid": "y"})
      and adoffer.paid_source({"ttclid": "t"}) and adoffer.paid_source({"rdt_cid": "r"})
      and adoffer.paid_source({"twclid": "w"}) and adoffer.paid_source({"wbraid": "b"}),
      "paid-ad sources: cpc / paid* mediums and ad click ids")
check(not adoffer.paid_source({}) and not adoffer.paid_source({"utm_medium": "email", "utm_source": "newsletter"})
      and not adoffer.paid_source({"utm_source": "google"}), "organic and newsletter visits aren't")
r_ = catalog.rates()
check(r_["video5"] <= 500 < r_["talk8"], "500 covers a 5-second product video, not a talking video")


def page(c, path):
    return c.get(path).get_data(as_text=True)


def ws_of(email):
    with db.connect() as conn:
        u = conn.execute("SELECT workspace_id FROM users WHERE email=?", (email,)).fetchone()
        ws = conn.execute("SELECT credits FROM workspaces WHERE id=?", (u[0],)).fetchone()
        led = conn.execute("SELECT delta, reason FROM ledger WHERE workspace_id=? ORDER BY id", (u[0],)).fetchall()
    return ws[0], [tuple(x) for x in led]


def sign(c, email):
    return c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})


ORGANIC = ("Make 2 ads with your free credits", "Free sign-up includes 2 image ads (44 credits). Nothing is charged.")
for path in ("/", "/pricing", "/features", "/signup", "/ai-ugc-ad-generator", "/arcads-alternative",
             "/ai-product-ads-for-shopify", "/llms.txt"):
    html = page(client(), path)
    check("first ad free" not in html.lower() and "first video free" not in html.lower()
          and "video ad free" not in html, f"organic {path}: no free-video or first-ad-free promise")
for path in ("/", "/pricing", "/ai-ugc-ad-generator"):
    html = page(client(), path)
    check(ORGANIC[0] in html and ORGANIC[1] in html, f"organic {path}: 'Make 2 ads with your free credits' + what sign-up includes")
html = page(client(), "/pricing")
check('<h2 class="lp-h2">Make 2 ads with your free credits.</h2>' in html, "the pricing footer no longer says 'Make your first ad free'")
for path in ("/", "/pricing", "/ai-ugc-ad-generator", "/arcads-alternative"):
    m = Page(page(client(), path))
    check("Free sign-up includes 2 image ads" in m.meta["description"] or "Try 2 image ads free" in m.meta["description"],
          f"{path} meta description says what free sign-up includes")
check("free sign-up includes 2 image ads" in page(client(), "/llms.txt"), "llms.txt says what free sign-up includes")

# An ad visitor (gclid) sees the video offer from their first page, and sign-up grants it.
a = client()
html = page(a, "/ai-ugc-ad-generator?gclid=Ad-1")
VIDEO_CTA = "Your first video ad is on us: 500 credits"
check(VIDEO_CTA in html and "Free sign-up includes 500 credits: enough for a 5-second product video, or 22 image ads. "
      "Talking presenter videos from 740 credits." in html and ORGANIC[1] not in html,
      "ad visitor: 'Your first video ad is on us: 500 credits' on the landing page, with what it buys in credits")
check("Free sign-up includes 500 credits: enough for a 5-second product video" in page(a, "/signup"),
      "and the sign-up page says what they'll get (from the cookie)")
r = sign(a, "adbuyer@gmail.com")
bal, led = ws_of("adbuyer@gmail.com")
check(bal == 500 and led == [(500, adoffer.REASON)], "sign-up grants 500 starter credits, once (one ledger row)")
check("cfy_adg=1" in " ".join(r.headers.getlist("Set-Cookie")), "and marks the browser")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM ad_signup_grants WHERE email_norm='adbuyer@gmail.com'").fetchone()[0] == 1,
          "one ad grant recorded for that person")

# Same browser, new email: the offer doesn't come back (copy and grant agree).
a2 = appmod.app.test_client()
a2.environ_base["REMOTE_ADDR"] = "10.9.200.1"
a2.set_cookie(adoffer.BROWSER_COOKIE, "1")
a2.get("/login")
html = page(a2, "/?gclid=Ad-2")
check(VIDEO_CTA not in html and ORGANIC[0] in html, "a browser that had the offer sees image-ad copy")
sign(a2, "second-try@example.com")
check(ws_of("second-try@example.com")[0] == 44, "and gets the normal 44")

# Same person, disguised email (+tag, dots, case): 44.
b = client()
b.get("/?utm_source=meta&utm_medium=paid_social")
sign(b, "Ad.Buyer+again@gmail.com")
check(ws_of("ad.buyer+again@gmail.com")[0] == 44, "the same Gmail with dots / +tag doesn't get the offer twice")

# Organic and opted-out visitors keep 44.
o = client()
o.get("/?utm_source=newsletter&utm_medium=email")
check(ORGANIC[0] in page(o, "/pricing"), "newsletter visitor: image-ad copy")
sign(o, "newsletter@example.com")
check(ws_of("newsletter@example.com")[0] == 44, "newsletter visitor: 44")
gpc = client(**{"Sec-GPC": "1"})
check(ORGANIC[0] in page(gpc, "/?gclid=gpc"), "GPC visitor (no first-touch cookie): image-ad copy")
sign(gpc, "gpc@example.com")
check(ws_of("gpc@example.com")[0] == 44, "GPC visitor: 44")

# One network: at most IP_LIMIT grants in 30 days; the copy changes with it.
net = []
for i in range(adoffer.IP_LIMIT + 1):
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = "10.77.0.1"
    c.get("/login")
    net.append((c, VIDEO_CTA in page(c, f"/?gclid=net{i}")))
    sign(c, f"net{i}@example.com")
check(all(v for _, v in net[:-1]) and not net[-1][1], "the offer copy stops once a network has had its grants")
check([ws_of(f"net{i}@example.com")[0] for i in range(adoffer.IP_LIMIT + 1)] == [500] * adoffer.IP_LIMIT + [44],
      "and so do the grants")

# Daily cap across all accounts.
with db.connect() as conn:
    made = conn.execute("SELECT COUNT(*) FROM ad_signup_grants").fetchone()[0]
os.environ["SIGNUP_CREDITS_ADS_PER_DAY"] = str(made)
d = client()
check(ORGANIC[0] in page(d, "/?gclid=cap"), "daily cap reached: image-ad copy")
sign(d, "capped@example.com")
check(ws_of("capped@example.com")[0] == 44, "daily cap reached: 44")
os.environ.pop("SIGNUP_CREDITS_ADS_PER_DAY")

# Switched off ("0" / "off"): 44 for everyone, organic copy for ad visitors too.
for off in ("0", "off"):
    os.environ["SIGNUP_CREDITS_ADS"] = off
    e = client()
    check(ORGANIC[0] in page(e, "/?gclid=off") and adoffer.credits() == 0, f"SIGNUP_CREDITS_ADS={off}: image-ad copy")
    sign(e, f"off{off}@example.com")
    check(ws_of(f"off{off}@example.com")[0] == 44, f"SIGNUP_CREDITS_ADS={off}: 44")
# A grant too small for a video keeps image-ad copy, with the right count.
os.environ["SIGNUP_CREDITS_ADS"] = "100"
f_ = client()
html = page(f_, "/?gclid=small")
check("Make 4 ads with your free credits" in html and "video ad free" not in html, "SIGNUP_CREDITS_ADS=100: 'Make 4 ads with your free credits'")
sign(f_, "small@example.com")
check(ws_of("small@example.com")[0] == 100, "SIGNUP_CREDITS_ADS=100: 100 credits")
os.environ.pop("SIGNUP_CREDITS_ADS")

# Invites and logged-in members never see or get the offer.
import verify  # noqa: E402
with db.connect() as conn:
    ua = conn.execute("SELECT * FROM users WHERE email='adbuyer@gmail.com'").fetchone()
    uo = conn.execute("SELECT * FROM users WHERE email='newsletter@example.com'").fetchone()
    check(verify._free_what(conn, ua) == "your free Campaignify credits (your first video ad or 22 image ads)"
          and verify._free_what(conn, uo) == "your free Campaignify credits (2 image ads)",
          "the confirm-your-email message says what that account's free credits buy")
html = page(a, "/pricing?gclid=member")
check(VIDEO_CTA not in html and ORGANIC[0] in html, "a logged-in member sees the normal copy")

print("== layout at phone width")
css = open(os.path.join(ROOT, "static", "style.css")).read()
check(".sol-hero{display:grid" in css and "@media (max-width:900px){.sol-hero{grid-template-columns:1fr" in css,
      "answer-first pages stack to one column on phones")

print(f"\nall {passed} public-site checks passed ({TMP})")
