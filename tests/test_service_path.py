"""The service-business entrance (service_path.py, roadmap M5.2; UX-13, UX-14, UX-15, UX-16), on a throwaway database:

  * RS_SERVICE_PATH off (default): no "What do you sell?", the first run still needs a product photo, the importer
    ignores sells=service and fills 1 photo
  * reading a business website without AI: the business name (not the page title), logo, colours, up to 3 photos,
    up to 5 claims in the site's words (suggested, never approved), service area, headline; the page title split
  * flag on: the first run asks "What do you sell?"; "A service" imports the website, marks the product a service and
    makes the 2 free image ads with no photo (the logo, or nothing), told it's a service; a photo template asks for
    a photo; the product first run fills 3 photos and suggested claims
  * the planner: a service-area field, the service brief, a Google keyword list (the model's, else a starter list),
    and the page, form and follow-up steps linked in a leads plan; the plan page shows both
  * every new line of copy passes the compliance rules and has no em or en dash

    python tests/test_service_path.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_service_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test",
                  RS_DISK_MIN_GB=os.environ.get("RS_DISK_MIN_GB", "0.2"), ANTHROPIC_API_KEY="sk-ant-test")
for k in ("RS_SERVICE_PATH", "FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import llm  # noqa: E402
import mailer  # noqa: E402
import planner  # noqa: E402
import service_path  # noqa: E402
import worker  # noqa: E402

passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TWO = catalog.price_credits("ad_image", batch=2)


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "re_1"}


mailer.requests.post = lambda url, json=None, headers=None, timeout=0, **kw: _Resp()
_ip = [0]


def client(email):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def ws_of(email):
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()


def png(n=400, rgb=(240, 232, 220)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (n, n), rgb).save(b, "PNG")
    return b.getvalue()


SITE = """<!doctype html><html><head><title>Northside Heating & Air | AC repair and furnace service in Plano, TX</title>
<meta name="description" content="Same-day AC repair, furnace tune-ups and new system installs in Plano and Allen. Free quotes.">
<meta name="theme-color" content="#1f4e79"><style>.btn{background:#f2a900}</style>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"HVACBusiness","name":"Northside Heating & Air",
"logo":"https://northside-hvac.example/img/logo.png","telephone":"+1-972-555-0142","areaServed":["Plano","Allen"]}</script>
</head><body><img src="/img/logo.png" alt="Northside logo"><h1>AC repair in Plano, same day</h1>
<img src="/img/tech-at-work.jpg" alt="A technician"><img src="/img/service-van.jpg" alt=""><img src="/img/new-install.jpg" alt="">
<img src="/img/fourth.jpg" alt=""><p>Licensed technicians. Upfront prices. Free quotes on new systems.</p>
<p>The best HVAC company in Texas!</p><a href="/quote">Get a free quote</a></body></html>"""
STORE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Ember Candle",
"description":"A soy wax candle with amber and cedar. Hand poured in small batches. 50 hour burn time.",
"image":["https://cdn.hearth.example/1.jpg","https://cdn.hearth.example/2.jpg","https://cdn.hearth.example/3.jpg",
"https://cdn.hearth.example/4.jpg"],"brand":{"@type":"Brand","name":"Hearth & Co"}}</script></head>
<body><h1>Ember Candle</h1><p>Amber and cedar. Soy wax. 50 hour burn.</p></body></html>"""


def fake_fetch(url, accept=None):
    if "northside" in url:
        return {"url": url, "status": 200, "content_type": "text/html", "text": SITE}
    if "hearth" in url:
        return {"url": url, "status": 200, "content_type": "text/html", "text": STORE}
    return {"url": url, "status": 403, "content_type": "text/html", "text": ""}


fetcher.fetch = fake_fetch
fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {
    "url": url, "status": 200, "content_type": "image/png",
    "data": png(120, (31, 78, 121)) if "logo" in url else png()}

try:
    print("== reading a business website (no AI)")
    f = service_path.site_facts(SITE, "https://northside-hvac.example/")
    check(f["name"] == "Northside Heating & Air" and f["brand"] == "Northside Heating & Air",
          "the business name comes from schema.org, not the page title with its slogan")
    check(f["logo"].endswith("/img/logo.png") and f["colors"][:2] == ["#1f4e79", "#f2a900"],
          "logo from schema.org; colours from theme-color and the site's CSS")
    check(len(f["images"]) == 4 and not any("logo" in u for u in f["images"]), "photos from the site, the logo kept apart")
    check(f["area"] == ["Plano", "Allen"] and f["headline"] == "AC repair in Plano, same day" and f["phone"],
          "service area, headline and phone")
    check(len(f["claims"]) == 5 and "Licensed technicians" in f["claims"] and "Upfront prices" in f["claims"]
          and not any("best" in c.lower() or c.lower().startswith("get ") for c in f["claims"]),
          f"5 claims in the site's own words, no superlatives or calls to action ({f['claims']})")
    check(f["services"][:2] == ["AC repair", "furnace tune-ups"], "services read from the description")
    check(service_path.business_name({}, {}, "Northside Heating & Air | AC repair in Plano", "https://x.com/") ==
          "Northside Heating & Air"
          and service_path.business_name({}, {}, "AC repair in Plano | Northside", "https://northside.com/") == "Northside"
          and service_path.business_name({}, {"og:site_name": "Bright Smiles"}, "Dentist | x", "https://a.com/")
          == "Bright Smiles", "without schema.org: og:site_name, else the part of the title that matches the domain")
    cl = service_path.page_claims({"description": "A soy wax candle with amber and cedar. Hand poured in small batches.",
                                   "text": "Ember Candle Amber and cedar. Soy wax. 50 hour burn.",
                                   "headings": ["Ember Candle"]}, "Ember Candle")
    check(cl[:2] == ["A soy wax candle with amber and cedar", "Hand poured in small batches"] and "Soy wax" not in cl
          and "Ember Candle" not in cl, f"product claims: shorter repeats and the name are dropped ({cl})")
    check(service_path.image_colors(png(100, (31, 78, 121))) == ["#1f4e79"] and service_path.color_name("#1f4e79")
          == "deep blue" and service_path.color_name("#f2a900") == "amber", "logo colours and their plain names")

    print("== decode limits (H-1): a tiny file with huge dimensions is never decoded")
    import io as _io
    import resource
    import imagesafe
    import product_import
    from PIL import Image as _Image
    _b = _io.BytesIO()
    _Image.new("1", (10000, 10000), 0).save(_b, "PNG", optimize=True)
    BOMB = _b.getvalue()                       # about 12 KB on the wire, 100 megapixels decoded
    check(len(BOMB) < 64 * 1024 and _Image.MAX_IMAGE_PIXELS == imagesafe.MAX_PIXELS,
          f"the bomb is {len(BOMB) // 1024} KB; Pillow's own pixel guard is set at startup")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    check(service_path.image_colors(BOMB) == [], "image_colors refuses it from the header (no colours, no decode)")
    _real_fetch_bytes = fetcher.fetch_bytes
    fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {"url": url, "status": 200,
                                                                 "content_type": "image/png", "data": BOMB}
    try:
        try:
            service_path.download_logo("https://northside-hvac.example/img/logo.png")
            ok = False
        except fetcher.FetchError:
            ok = True
        check(ok, "download_logo refuses a logo over 4096 x 4096 pixels")
        try:
            product_import.download_image("https://cdn.hearth.example/1.jpg")
            ok = False
        except fetcher.FetchError as e:
            ok = "too large in pixels" in str(e)
        check(ok, "product_import.download_image refuses it too (any import path)")
    finally:
        fetcher.fetch_bytes = _real_fetch_bytes
    grew = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - peak) // 1024
    check(grew < 50, f"peak memory grew {grew} MB for 3 refusals (a full decode is about 700 MB)")
    try:
        imagesafe.probe(b"x" * (imagesafe.MAX_BYTES + 1))
        ok = False
    except imagesafe.BadImage:
        ok = True
    check(ok, "a byte cap applies on top of the pixel cap")
    _b = _io.BytesIO()
    _Image.new("RGB", (4000, 3000), (31, 78, 121)).save(_b, "JPEG")
    th = imagesafe.thumbnail(_b.getvalue(), (96, 96), mode="RGBA")
    check(th.size == (96, 72) and th.mode == "RGBA", "a big JPEG thumbnails in draft mode, then converts")

    print("== flag off: nothing changes")
    a = client("off@example.com")
    home = a.get("/app").get_data(as_text=True)
    check('name="sells"' not in home and "What do you sell?" not in home and 'name="svc_template"' not in home,
          "no 'What do you sell?' on the first run")
    d = a.post("/app/start/import", json={"url": "northside-hvac.example", "sells": "service"},
               headers={"X-CSRF": csrf(a)}).get_json()
    check(d["ok"] and "more" not in d and "claims" not in d, "the importer ignores sells=service (the product importer)")
    r = a.post("/app/start", data={"csrf": csrf(a), "kind": "image", "product_name": "Northside", "template": "hero",
                                   "sells": "service", "svc_template": "svc-logo"})
    check(r.status_code == 400 and "Add one photo of your product." in r.get_data(as_text=True),
          "no photo: refused as before (UX-13 stays a known gap with the flag off)")

    os.environ["RS_SERVICE_PATH"] = "1"
    print("== flag on: the first run asks what you sell")
    s = client("owner@northside.example")
    home = s.get("/app").get_data(as_text=True)
    check("What do you sell?" in home and 'name="sells" value="product" checked' in home
          and 'name="sells" value="service"' in home and 'name="svc_template" value="svc-logo"' in home
          and "Make 2 ads with your free credits" in home, "a product or a service; product first; the button wording unchanged")
    d = s.post("/app/start/import", json={"url": "northside-hvac.example", "sells": "service"},
               headers={"X-CSRF": csrf(s)}).get_json()
    ws = ws_of("owner@northside.example")
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=?", (ws["id"],)).fetchone()
        nphotos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (p["id"],)).fetchone()[0]
        bname = conn.execute("SELECT brand_name FROM brands WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    imp = json.loads(p["import_json"])
    check(d["ok"] and d["name"] == "Northside Heating & Air" and d["brand"] == bname == "Northside Heating & Air",
          "the import fills the business name and names the brand")
    check(d["photo"] and len(d["more"]) == 2 and d["logo"] and nphotos == 4,
          "3 photos from the site plus the logo, downloaded (only the site's own images)")
    check(d["colors"] == ["#1f4e79", "#f2a900"] and d["pick"] == "svc-photo" and d["claims"] == 5
          and "deep blue and amber" in d["briefs"]["svc-logo"] and "AC repair in Plano, same day" in d["briefs"]["svc-logo"],
          "colours, the template to start on, and briefs filled with the headline and colours")
    check(imp["sells"] == "service" and imp["site"]["logo_photo_id"] == d["logo"]["id"] and service_path.is_service(p)
          and len(json.loads(p["claims_pending"])) == 5 and not (p["claims"] or "").strip(),
          "the product is a service; claims are suggestions only (none approved)")
    check(ws["credits"] == catalog.starter_credits(), "the website read is free")
    home = s.get("/app").get_data(as_text=True)
    check('name="sells" value="service" checked' in home and 'data-site' in home and "#1f4e79" in home,
          "Home remembers it's a service and shows the logo and colours")
    r = s.post("/app/start", data={"csrf": csrf(s), "sells": "service", "kind": "video", "product_name": d["name"],
                                   "svc_template": "svc-photo"})
    check(r.status_code == 400 and "This template uses a photo" in r.get_data(as_text=True),
          "a photo template without a photo asks for one (or Logo and headline)")
    r = s.post("/app/start", data={"csrf": csrf(s), "sells": "service", "kind": "video", "product_name": d["name"],
                                   "svc_template": "svc-logo"})
    check(r.status_code == 302 and r.headers["Location"].startswith("/app?made="), "Logo and headline starts with no photo")
    with db.connect() as conn:
        jobs = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (ws["id"],)).fetchall()
        prods = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
        prompt = worker.image_prompt(conn, jobs[0])
    params = json.loads(jobs[0]["params_json"])
    check(len(jobs) == 1 and jobs[0]["tool"] == "ad_image" and params["batch"] == 2 and jobs[0]["credits"] == TWO
          and params["photo_ids"] == [d["logo"]["id"]] and params["service"] and prods == 1,
          "the 2 free image ads (one job, image even though video was posted), the logo as the only reference")
    check("a service business, not a packaged product" in prompt and "Keep a logo exactly as it is" in prompt
          and "exactly as in the reference photos" not in prompt and '"AC repair in Plano, same day"' in prompt
          and '"Book a call"' in prompt, "the image model is told it's a service, with the site's headline")
    check("the logo" in catalog.image_ad_prompt("A van", "Hi", service=True).lower()
          and "packaging" in catalog.image_ad_prompt("A jar", "Hi"), "image prompts: service and product variants")

    print("== flag on, no website: a service with no photo and no logo")
    n = client("nosite@example.com")
    r = n.post("/app/start", data={"csrf": csrf(n), "sells": "service", "product_name": "Bright Smiles Dental",
                                   "svc_template": "svc-area"})
    with db.connect() as conn:
        w = ws_of("nosite@example.com")
        j = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (w["id"],)).fetchone()
        pr = conn.execute("SELECT * FROM products WHERE workspace_id=?", (w["id"],)).fetchone()
    check(r.status_code == 302 and json.loads(j["params_json"])["photo_ids"] == [] and service_path.is_service(pr),
          "a typed business name and a no-photo template: the ads start with no reference image at all")
    check(n.post("/app/start", data={"csrf": csrf(n), "sells": "service", "product_name": "",
                                     "svc_template": "svc-logo"}).status_code == 400, "a business name is required")

    print("== flag on: the product first run fills more")
    pc = client("candle@example.com")
    d = pc.post("/app/start/import", json={"url": "hearth.example/products/ember", "sells": "product"},
                headers={"X-CSRF": csrf(pc)}).get_json()
    with db.connect() as conn:
        w = ws_of("candle@example.com")
        p = conn.execute("SELECT * FROM products WHERE workspace_id=?", (w["id"],)).fetchone()
        nph = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (p["id"],)).fetchone()[0]
    check(d["name"] == "Ember Candle" and d["photo"] and len(d["more"]) == 2 and nph == 3,
          "3 of the page's own photos (was 1)")
    check(d["claims"] == 3 and "Hand poured in small batches" in json.loads(p["claims_pending"]) and not p["claims"],
          "claims suggested from the page's words (was 0), none approved")
    check(not service_path.is_service(p), "a product stays a product")

    print("== the planner: service area, keyword list, lead steps")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override='growth', credits=5000 WHERE id=?", (ws["id"],))
        conn.commit()
    html = s.get("/app/campaigns").get_data(as_text=True)
    check('name="area"' in html and 'value="Plano, Allen"' in html and '<option value="leads" selected' in html
          and 'value="google" checked' in html and "Your website or landing page" in html,
          "a service: Leads, Google and Meta, and the service area prefilled from the site")
    calls = []

    def fake(body, timeout=None):
        calls.append(body)
        step = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if step == 0:
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "content": [
                {"type": "tool_use", "id": "t0", "name": "get_brand_and_catalog", "input": {}}]}
        return {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "content": [
            {"type": "tool_use", "id": "t1", "name": "finish", "input": {
                "summary": "Leads first.", "checklist": [{"title": "Install the Google tag", "platform": "google"},
                                                         {"title": "Build the landing page and form", "platform": "all"}]}}]}
    llm._anthropic = fake
    r = s.post("/app/campaigns", data={"csrf": csrf(s), "offer": "AC repair", "goal": "leads", "budget": "$900",
                                       "platforms": ["google", "meta"], "area": "Plano and Allen"})
    check(r.status_code == 302 and "/app/campaigns/" in r.headers["Location"], "the plan runs")
    cid = int(r.headers["Location"].rsplit("/", 1)[1])
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    brief, plan = json.loads(c["brief_json"]), json.loads(c["plan_json"])
    first = calls[0]["messages"][0]["content"]
    first = first if isinstance(first, str) else " ".join(b.get("text", "") for b in first)
    check(brief["area"] == "Plano and Allen" and brief["business"] == "service" and "Service area: Plano and Allen" in first
          and "local service business" in first, "the brief carries the area and the service prompt")
    fin = [t for t in calls[0]["tools"] if t["name"] == "finish"][0]
    check("search_keywords" in fin["input_schema"]["properties"]
          and "search_keywords" not in planner.TOOLS[-1]["input_schema"]["properties"],
          "the finish tool asks for keywords on this run only")
    kws = plan["final"]["keywords"]
    check(kws["source"] == "starter" and len(kws["items"]) >= 10
          and {"term": "ac repair plano", "match": "exact"} in kws["items"]
          and {"term": "ac repair near me", "match": "phrase"} in kws["items"],
          f"the model gave no keywords: a starter list from the services and area ({len(kws['items'])} terms)")
    titles = [i["title"] for i in plan["final"]["checklist"]]
    check(titles[:3] == ["Publish the lead page", "Add the quote form to it", "Turn on the follow-up flow"]
          and "Build the landing page and form" not in titles and "Make the lead magnet" not in titles
          and [i.get("link") for i in plan["final"]["checklist"][:3]] == ["pages", "forms", "flows"],
          "the page, form and follow-up steps come first, linked; the generic page item and lead magnet go")
    html = s.get(f"/app/campaigns/{cid}").get_data(as_text=True)
    check('id="keywords"' in html and "[ac repair plano]" in html and "&#34;ac repair near me&#34;" in html
          and 'href="/app/pages">Open Pages' in html and 'href="/app/forms">Open Forms' in html
          and 'href="/app/flows">Open Flows' in html and "negative keywords" in html,
          "the plan page shows the keyword list (copyable, match types) and the linked steps")
    got = service_path.clean_keywords([{"term": "AC Repair Plano!", "match": "exact"}, "furnace repair", {"term": ""},
                                       {"term": "ac repair plano"}])
    check(got == [{"term": "ac repair plano", "match": "exact"}, {"term": "furnace repair", "match": "phrase"}],
          "the model's keyword list is cleaned and deduplicated")
    ctx = {"plan": planner.Plan(), "steps": [], "brief": {"goal": "leads", "platforms": ["meta"]}, "brand": {}}
    planner.dispatch("finish", {"summary": "x", "checklist": []}, ctx)
    check([i["title"] for i in ctx["plan"].final["checklist"]][:4] ==
          ["Publish the lead page", "Add the quote form to it", "Turn on the follow-up flow", "Make the lead magnet"]
          and "keywords" not in ctx["plan"].final, "a product leads plan keeps its lead magnet; no Google, no keywords")
    os.environ.pop("RS_SERVICE_PATH")
    ctx = {"plan": planner.Plan(), "steps": [], "brief": {"goal": "leads", "platforms": ["google"]}, "brand": {}}
    planner.dispatch("finish", {"summary": "x", "checklist": []}, ctx)
    check("keywords" not in ctx["plan"].final and "Build the landing page and form" in
          [i["title"] for i in ctx["plan"].final["checklist"]], "flag off: the plan is as before")

    print("== copy rules")
    texts = [t["name"] + " " + t["line"] + " " + t["brief"] for t in service_path.TEMPLATES]
    texts += [i["title"] + " " + i["detail"] for i in service_path.LEAD_STEPS]
    texts += [service_path.SERVICE_PROMPT, service_path.KEYWORDS_PROMPT] + service_path.image_lines("Northside")
    tpl = open(os.path.join(ROOT, "web/_first_ad.html")).read()
    texts += [x for x in __import__("re").findall(r'data-service="([^"]+)"', tpl)]
    check(all(compliance.check(t)["ok"] for t in texts), "every new line passes the compliance rules")
    check(not any(ch in t for t in texts for ch in "–—"), "no em or en dashes in the new copy")
    check(all(os.path.exists(os.path.join(ROOT, "static/previews", t["preview"] + ".jpg")) for t in service_path.TEMPLATES),
          "each service template has its preview image")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
