""""Import from a link" against stores that refuse apps, on a throwaway
database with every HTTP answer faked below the fetcher (so the real SSRF
guard, redirects, content-type and size handling and the request headers all
run).

What it pins down:
  * the store's published product data is read first and honestly: Shopify's
    <product-url>.js, then the page's JSON-LD Product / Open Graph tags;
  * the first refusal (401/403/429 or a bot challenge) stops the import: no
    retry, no other user-agent, no other endpoint;
  * a refused import opens a 30-second by-hand form, prefilled from the link
    (brand from the domain, name from the path), with photo upload and a
    "paste the product page text" box read by the same AI step;
  * the SSRF guard and the honest user-agent are unchanged;
  * every try is logged and counted on the admin system card.

    python tests/test_import.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_import_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_MARKUP="2.0")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import llm  # noqa: E402
import product_import  # noqa: E402
import products  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


HONEST_UA = "Mozilla/5.0 (compatible; CampaignifyBot/1.0; +https://campaignify.ai)"

# ---------------------------------------------------------------- fake HTTP, below the fetcher
ROUTES = {}      # (host, path) -> (status, content type, body bytes, extra headers)
REQS = []        # every request that reached the "network"
STORES = {"www.glow-co.com", "glow-co.com", "hearthcandles.com", "plainstore.com", "wallstore.com", "pdfstore.com",
          "redirector.com", "challenge-shop.com", "slowshop.com"}
_real_resolve = fetcher.resolve


def fake_resolve(host, port):
    return "93.184.216.34" if host in STORES else _real_resolve(host, port)


class FakeResp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self._b = status, headers, body

    def read1(self, n=65536, decode_content=True):
        chunk, self._b = self._b[:n], self._b[n:]
        return chunk

    def release_conn(self):
        pass


class FakePool:
    def __init__(self, scheme, host, port, ip):
        self.scheme, self.host, self.port, self.ip = scheme, host, port, ip

    def urlopen(self, method, path, headers=None, **kw):
        REQS.append({"host": self.host, "path": path, "ip": self.ip, "headers": dict(headers or {})})
        st, ctype, body, extra = ROUTES.get((self.host, path.split("?")[0]), (404, "text/html", b"<html>Not found</html>", {}))
        return FakeResp(st, dict({"Content-Type": ctype}, **extra), body)

    def close(self):
        pass


fetcher.resolve = fake_resolve
fetcher._open = lambda scheme, host, port, ip: FakePool(scheme, host, port, ip)


def route(host, path, status=200, ctype="text/html; charset=utf-8", body="", **headers):
    ROUTES[(host, path)] = (status, ctype, body.encode() if isinstance(body, str) else body, headers)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1", "name": email.split("@")[0]})
    c.get("/app/products")          # makes the workspace's first brand
    with db.connect() as conn:
        ws = conn.execute("SELECT w.* FROM users u JOIN workspaces w ON w.id=u.workspace_id WHERE u.email=?",
                          (email,)).fetchone()
    return c, ws


def png(size=400):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (size, size), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


def attempts():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM import_attempts ORDER BY id").fetchall()]


def newest_product(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC", (ws_id,)).fetchone()


def import_(c, ws, url):
    REQS.clear()
    return c.post("/app/products/import", data={"csrf": csrf(c), "url": url, "brand": str(ws["brand_id"])})


# ---------------------------------------------------------------- the helpers
print("== helpers")
check(fetcher.UA == HONEST_UA, "the fetcher's user-agent is still the honest CampaignifyBot one")
check(product_import.shopify_endpoints("https://www.glow-co.com/products/vitamin-c-serum?variant=42") == [
    "https://www.glow-co.com/products/vitamin-c-serum.js", "https://www.glow-co.com/products/vitamin-c-serum.json"]
      and product_import.shopify_endpoints("https://www.glow-co.com/collections/all/products/serum/") == [
    "https://www.glow-co.com/collections/all/products/serum.js", "https://www.glow-co.com/collections/all/products/serum.json"]
      and product_import.shopify_endpoints("https://hearthcandles.com/p/ember") == [], "Shopify's documented endpoints, only for /products/<handle>")
for url, want in (("https://www.glow-co.com/products/x", "Glow Co"), ("shop.hearthcandles.com", "Hearthcandles"),
                  ("https://sunrise-blend.co.uk/p", "Sunrise Blend"), ("https://glow-co.myshopify.com/products/x", "Glow Co"),
                  ("http://93.184.216.34/", "")):
    check(product_import.brand_guess(url) == want, f"brand guess from {url!r}: {want!r}")
for url, want in (("https://www.glow-co.com/products/vitamin-c-serum?variant=1", "Vitamin C Serum"),
                  ("https://x.com/products/ember_candle-8oz", "Ember Candle 8oz"), ("https://x.com/", ""),
                  ("https://x.com/products/9f86d081884c7d659a2feaa0c55ad015", "")):
    check(product_import.name_guess(url) == want, f"name guess from {url!r}: {want!r}")
CHALLENGE = "<html><head><title>Just a moment...</title></head><body><div id=\"challenge-running\"></div></body></html>"
REAL = ('<html><head><title>Serum</title><script type="application/ld+json">{"@type":"Product","name":"Serum"}</script>'
        '<script src="https://www.google.com/recaptcha/api.js"></script></head><body><div class="g-recaptcha"></div></body></html>')
check(product_import.refused({"status": 403, "text": ""}) and product_import.refused({"status": 429, "text": ""})
      and product_import.refused({"status": 503, "text": CHALLENGE}) and product_import.refused({"status": 200, "text": CHALLENGE}),
      "403, 429 and a challenge page (any status) count as the store refusing")
check(not product_import.refused({"status": 200, "text": REAL}) and not product_import.refused({"status": 404, "text": "nope"}),
      "a real product page with a captcha on its review form, or a 404, is not a refusal")

# ---------------------------------------------------------------- structured data first
print("== structured data first")
SHOP_JS = json.dumps({"id": 1, "title": "Vitamin C Serum", "handle": "vitamin-c-serum", "vendor": "Glow Co",
                      "description": "<p>A lightweight vitamin C serum for dull skin.</p><ul><li>30 ml glass bottle</li>"
                                     "<li>15% vitamin C</li></ul>",
                      "price": 3400, "tags": ["Skincare", "Serum"],
                      "images": ["//cdn.shopify.com/s/files/1/serum-front.jpg", "//cdn.shopify.com/s/files/1/serum-box.jpg"],
                      "variants": [{"id": 9, "price": 3400}]})
route("www.glow-co.com", "/products/vitamin-c-serum.js", 200, "application/json; charset=utf-8", SHOP_JS)
route("www.glow-co.com", "/products/vitamin-c-serum", 403, "text/html", "<html><title>403 Forbidden</title></html>")
u1, w1 = signup("shopify@example.com")
r = import_(u1, w1, "https://www.glow-co.com/products/vitamin-c-serum?variant=9")
p1 = newest_product(w1["id"])
check(r.status_code == 302 and p1 is not None and r.headers["Location"].endswith(f"/app/products/{p1['id']}"),
      "Shopify's .js product data makes the product even though the page itself answers 403")
check(p1["name"] == "Vitamin C Serum" and p1["price"] == "$34" and "lightweight vitamin C serum" in p1["description"]
      and p1["tags"] == "skincare, serum", "name, price (from cents), description and tags from the store's product data")
own, _ = products.import_candidates(p1)
check(own == ["https://cdn.shopify.com/s/files/1/serum-front.jpg", "https://cdn.shopify.com/s/files/1/serum-box.jpg"],
      "the store's own product photos are offered")
with db.connect() as conn:
    check(products.get_brand(conn, w1["id"], w1["brand_id"])["brand_name"] == "Glow Co", "the empty brand takes the store's vendor name")
check([q["path"] for q in REQS] == ["/products/vitamin-c-serum.js", "/products/vitamin-c-serum?variant=9"],
      "two requests: the .js data, then the page once (its 403 isn't retried)")
check(all(q["headers"].get("User-Agent") == HONEST_UA for q in REQS), "every request carries the honest user-agent")
check(attempts()[-1]["method"] == "shopify-json" and attempts()[-1]["status"] == "ok"
      and attempts()[-1]["domain"] == "glow-co.com", "logged: glow-co.com, shopify-json, ok")

LD_PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth</title>
<meta property="og:image" content="https://cdn.hearthcandles.com/ember-hero.jpg">
<script type="application/ld+json">{"@type":"Product","name":"Ember Candle","description":"A soy wax candle.",
"image":"https://cdn.hearthcandles.com/ember-1.jpg","brand":{"@type":"Brand","name":"Hearth"},
"offers":{"@type":"Offer","price":"28.00","priceCurrency":"USD"}}</script></head>
<body><h1>Ember Candle</h1><p>Hand-poured soy wax. Burns for about 50 hours.</p></body></html>"""
route("hearthcandles.com", "/p/ember", 200, "text/html; charset=utf-8", LD_PAGE)
u2, w2 = signup("jsonld@example.com")
r = import_(u2, w2, "https://hearthcandles.com/p/ember")
p2 = newest_product(w2["id"])
check(p2["name"] == "Ember Candle" and p2["price"] == "$28.00" and len(REQS) == 1, "a JSON-LD Product page imports in one request")
check(attempts()[-1]["method"] == "jsonld" and attempts()[-1]["status"] == "ok", "logged as jsonld")
OG_PAGE = """<html><head><title>Plain Store</title><meta property="og:title" content="Linen Throw">
<meta property="og:description" content="A washed linen throw blanket."><meta property="og:image" content="https://plainstore.com/i/throw.jpg">
<meta property="product:price:amount" content="89"><meta property="product:price:currency" content="USD"></head>
<body><p>Washed linen. 130 x 170 cm.</p></body></html>"""
route("plainstore.com", "/linen-throw", 200, "text/html", OG_PAGE)
r = import_(u2, w2, "https://plainstore.com/linen-throw")
p3 = newest_product(w2["id"])
check(p3["name"] == "Linen Throw" and p3["price"] == "$89" and "washed linen throw" in p3["description"],
      "Open Graph tags fill the product when there's no JSON-LD")
check(attempts()[-1]["method"] == "og", "logged as og")

# ---------------------------------------------------------------- refused: stop, and the manual path
print("== a store that refuses apps")
route("wallstore.com", "/products/night-cream.js", 403, "text/html", "<html><title>Access denied</title></html>")
route("wallstore.com", "/products/night-cream.json", 200, "application/json", json.dumps({"product": {"title": "Should not be read"}}))
route("wallstore.com", "/products/night-cream", 200, "text/html", LD_PAGE)
u3, w3 = signup("blocked@example.com")
with db.connect() as conn:
    n_before = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?", (w3["id"],)).fetchone()[0]
r = import_(u3, w3, "https://wallstore.com/products/night-cream")
check([q["path"] for q in REQS] == ["/products/night-cream.js"],
      "the first 403 ends it: the .json and the page are never asked for, nothing is retried")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?", (w3["id"],)).fetchone()[0] == n_before,
          "no product is made from a refused store")
loc = r.headers["Location"]
q = parse_qs(urlsplit(loc).query)
check(r.status_code == 302 and urlsplit(loc).path == "/app/products/new" and q.get("manual") == ["blocked"]
      and q.get("src") == ["https://wallstore.com/products/night-cream"], "it opens the by-hand form for that link")
html = u3.get(loc).get_data(as_text=True)
check("This store doesn&#39;t let apps read its pages automatically." in html or
      "This store doesn't let apps read its pages automatically." in html, "the friendly explanation is shown")
check("Add the details yourself — takes 30 seconds:" in html, "with the 30-second promise")
check('value="Night Cream"' in html and 'name="brand_name"' in html and 'value="Wallstore"' in html
      and 'value="https://wallstore.com/products/night-cream"' in html, "prefilled: name from the link, brand from the domain, the link")
check('name="price"' in html and 'name="description"' in html and "Key claims" in html and 'name="photos"' in html
      and "data-drop" in html and "Drop photos here, paste one, or browse" in html and "Tap to add photos" in html,
      "fields for price, description, key claims and drag-and-drop / paste / tap photo upload")
check('name="page_text"' in html and "Or paste the product page text here" in html, "and a box to paste the page's text")
check('id="imp-url"' not in html and "Try another link" in html, "the import bar steps aside (another link is one tap away)")
check('id="details" ' not in html and 'class="panel manual"' in html, "the form is in its mobile-first manual layout")
a = attempts()[-1]
check(a["domain"] == "wallstore.com" and a["method"] == "shopify-json" and a["status"] == "blocked" and a["http_status"] == 403,
      "logged: wallstore.com, shopify-json, blocked, 403")

# a page-level challenge and a 429 are refusals too
route("challenge-shop.com", "/item/42", 503, "text/html", CHALLENGE)
r = import_(u3, w3, "https://challenge-shop.com/item/42")
check("manual=blocked" in r.headers["Location"] and len(REQS) == 1 and attempts()[-1]["status"] == "blocked",
      "a bot-challenge page: one request, then the by-hand form")
route("slowshop.com", "/products/tea.js", 404)
route("slowshop.com", "/products/tea.json", 404)
route("slowshop.com", "/products/tea", 429, "text/html", "Too many requests")
r = import_(u3, w3, "https://slowshop.com/products/tea")
check("manual=blocked" in r.headers["Location"] and [x["path"] for x in REQS] == ["/products/tea.js", "/products/tea.json", "/products/tea"]
      and attempts()[-1]["http_status"] == 429, "a 429 on the page is a refusal (a 404 on .js / .json isn't)")
check(all(x["headers"]["User-Agent"] == HONEST_UA for x in REQS), "(still the honest user-agent)")

html = u3.get("/app/products/new?manual=blocked&src=javascript:alert(1)").get_data(as_text=True)
check("javascript:alert" not in html.split('name="src"')[0].split("manualbar")[1], "a javascript: link in src is never made a link")

# the by-hand form
r = u3.post("/app/products/new", data={
    "csrf": csrf(u3), "brand": str(w3["brand_id"]), "manual": "blocked", "src": "https://wallstore.com/products/night-cream",
    "brand_name": "Wall Store", "name": "Night Cream", "price": "$42", "description": "A rich night cream.",
    "claims": "50 ml jar\nShea butter", "url": "https://wallstore.com/products/night-cream", "page_text": "",
    "photos": [(png(), "front.png"), (png(), "side.png")]}, content_type="multipart/form-data")
pm = newest_product(w3["id"])
with db.connect() as conn:
    nph = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (pm["id"],)).fetchone()[0]
    bname = products.get_brand(conn, w3["id"], w3["brand_id"])["brand_name"]
check(r.status_code == 302 and pm["name"] == "Night Cream" and pm["price"] == "$42" and pm["claims"] == "50 ml jar\nShea butter"
      and nph == 2 and pm["primary_photo_id"], "the by-hand form makes the product with its photos and the claims typed in")
check(bname == "Wall Store", "the brand gets the name from the form")
check(attempts()[-1]["method"] == "manual" and attempts()[-1]["status"] == "ok" and attempts()[-1]["detail"] == "form, 2 photos",
      "logged as manual (form, 2 photos)")
r = u3.post("/app/products/new", data={"csrf": csrf(u3), "brand": str(w3["brand_id"]), "manual": "blocked",
                                       "src": "https://wallstore.com/products/x", "name": "", "page_text": ""})
check(r.status_code == 400 and "Give the product a name." in r.get_data(as_text=True)
      and "Add the details yourself" in r.get_data(as_text=True), "no name and no pasted text: asked for a name, form kept")

# pasted page text, read without AI, then with it
PASTED = """Wallstore
Search  Cart
Overnight Repair Cream
$38.00
A night cream with ceramides for dry skin.
- 50 ml jar
- Ceramides and shea butter
- Fragrance free
- Cures acne overnight
"""
r = u3.post("/app/products/new", data={"csrf": csrf(u3), "brand": str(w3["brand_id"]), "manual": "blocked",
                                       "src": "https://wallstore.com/products/repair", "name": "", "page_text": PASTED})
pt = newest_product(w3["id"])
check(r.status_code == 302 and pt["name"] == "Overnight Repair Cream", "without AI the name is a first guess from the text (the line above the price)")
check(pt["price"] == "$38.00" and products.pending(pt) == [], "and the price is read; no claims are guessed without AI")

llm_bodies = []


def fake_llm(body):
    llm_bodies.append(body)
    out = {"name": "Overnight Repair Cream", "description": "A ceramide night cream for dry skin.", "price": "$38",
           "claims": ["50 ml jar", "Ceramides and shea butter", "Fragrance free", "Cures acne overnight",
                      "Dermatologist recommended"], "tags": ["skincare"], "brand": "Wallstore"}
    return {"model": body["model"], "usage": {"input_tokens": 2000, "output_tokens": 300},
            "content": [{"type": "tool_use", "id": "t", "name": "submit_product", "input": out}]}


llm._anthropic = fake_llm
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
with db.connect() as conn:
    db.adjust_credits(conn, w3["id"], 500, "test top-up")
    conn.commit()
    bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (w3["id"],)).fetchone())
html = u3.get("/app/products/new?manual=blocked&src=https://wallstore.com/products/repair").get_data(as_text=True)
check("Read by AI (up to" in html, "with AI on, the paste box says what the read costs")
r = u3.post("/app/products/new", data={"csrf": csrf(u3), "brand": str(w3["brand_id"]), "manual": "blocked",
                                       "src": "https://wallstore.com/products/repair", "name": "", "price": "$36",
                                       "claims": "50 ml jar", "page_text": PASTED, "photos": [(png(), "jar.png")]},
           content_type="multipart/form-data")
pa = newest_product(w3["id"])
with db.connect() as conn:
    bal2 = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (w3["id"],)).fetchone())
check(r.status_code == 302 and pa["name"] == "Overnight Repair Cream" and pa["price"] == "$36"
      and pa["description"] == "A ceramide night cream for dry skin.", "pasted text: the AI read fills what was left empty; typed fields win")
check(llm_bodies and "Ceramides and shea butter" in llm_bodies[-1]["messages"][0]["content"]
      and llm_bodies[-1]["model"] == llm.fast_model(), "the pasted text goes through the same fast-model read as an import")
check(pa["claims"] == "50 ml jar" and products.pending(pa) == ["Ceramides and shea butter", "Fragrance free"],
      "suggested claims wait for approval; health claims, ones the text doesn't state and ones already typed are dropped")
check(0 < bal - bal2 <= product_import.estimate(), f"the AI read is charged ({bal - bal2} credits)")
check(attempts()[-1]["method"] == "manual" and attempts()[-1]["detail"] == "text, 1 photo", "logged as manual (text, 1 photo)")
html = u3.get(f"/app/products/{pa['id']}").get_data(as_text=True)
check("Found in the page text you pasted" in html, "the claims to review say where they came from")
os.environ.pop("ANTHROPIC_API_KEY", None)

# a page that isn't a web page, or a 404: the form, with why
route("pdfstore.com", "/catalog.pdf", 200, "application/pdf", b"%PDF-1.4 ...")
r = import_(u3, w3, "https://pdfstore.com/catalog.pdf")
check("manual=failed" in r.headers["Location"] and attempts()[-1]["status"] == "failed",
      "a link that isn't a web page (content-type check unchanged) offers the form too")
html = u3.get(r.headers["Location"]).get_data(as_text=True)
check("We couldn&#39;t read that page." in html or "We couldn't read that page." in html, "saying it couldn't read it")
check("isn&#39;t a web page" in html or "isn't a web page" in html, "and why")

# ---------------------------------------------------------------- the SSRF guard is unchanged
print("== SSRF guard")
for bad in ("http://169.254.169.254/products/latest", "http://127.0.0.1/products/x", "http://localhost/products/x",
            "http://10.0.0.5/p", "https://glow-co.com:8443/products/x", "ftp://glow-co.com/products/x"):
    r = import_(u3, w3, bad)
    msg = u3.get(r.headers["Location"]).get_data(as_text=True)
    check(REQS == [] and "manual=" not in r.headers["Location"] and "Couldn" in msg and attempts()[-1]["status"] == "failed",
          f"{bad}: refused before any request, no form prefilled from it")
route("redirector.com", "/products/x.js", 404)
route("redirector.com", "/products/x.json", 404)
route("redirector.com", "/products/x", 302, "text/html", "", Location="http://10.0.0.5/admin")
r = import_(u3, w3, "https://redirector.com/products/x")
check([x["host"] for x in REQS] == ["redirector.com"] * 3 and attempts()[-1]["status"] == "failed",
      "a redirect to a private address is still blocked at the hop")
big = "<html><head><title>Big</title></head><body>" + "x" * (fetcher.MAX_BYTES + 10) + "</body></html>"
route("plainstore.com", "/big", 200, "text/html", big)
check(len(fetcher.fetch("https://plainstore.com/big")["text"]) == fetcher.MAX_BYTES, "the body is still capped at MAX_BYTES")

# ---------------------------------------------------------------- the setup assistant
print("== setup assistant")
import assistant_routes  # noqa: E402
from flask import g  # noqa: E402
with appmod.app.test_request_context():
    with db.connect() as conn:
        g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (w3["id"],)).fetchone()
        card = {"id": "c1", "kind": "propose_import", "url": "https://wallstore.com/products/night-cream", "status": "open"}
        REQS.clear()
        note, extra = assistant_routes._do_import(conn, card)
check(card["status"] == "failed" and "doesn't let apps read" in card["error"] and "30 seconds" in card["error"]
      and card["manual_url"].startswith("/app/products/new?") and "manual=blocked" in card["manual_url"]
      and len(REQS) == 1 and "don't suggest retrying" in note, "the assistant's import card links to the by-hand form")

# ---------------------------------------------------------------- the admin system card
print("== admin system card")
from werkzeug.security import generate_password_hash  # noqa: E402
with db.connect() as conn:
    conn.execute("INSERT INTO users (email, pw_hash, is_admin, workspace_id, created_at, email_verified_at, role) "
                 "VALUES ('boss@example.com', ?, 1, ?, ?, ?, 'owner')",
                 (generate_password_hash("longpassword1"), w1["id"], db.now(), db.now()))
    conn.commit()
    data = __import__("admin_funnel").imports(conn)
adm = client()
adm.post("/login", data={"csrf": csrf(adm), "email": "boss@example.com", "password": "longpassword1"})
check(data["ok"] == 3 and data["blocked"] == 4 and data["manual"] == 3 and data["failed"] == 8
      and dict(data["methods"]) == {"shopify-json": 1, "jsonld": 1, "og": 1}, f"counts by outcome and method ({data})")
check(data["blocked_domains"][0] == ("wallstore.com", 2), "the stores that refuse us most are listed")
html = adm.get("/admin").get_data(as_text=True)
check("Product imports (7 days)" in html and "<b>3</b> read" in html and "4 refused by the store" in html
      and "3 added by hand after" in html and "shopify-json 1" in html and "jsonld 1" in html and "og 1" in html
      and "wallstore.com (2)" in html, "the admin system card shows the import counts")
check(u3.get("/admin").status_code == 403, "(admin only)")

print(f"\nall {passed} import checks passed ({TMP})")
