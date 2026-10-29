"""Brands & products, the creation flow and the setup assistant, on a
throwaway database with the generation and writing APIs faked.

    python tests/test_products.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_prod_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_MARKUP="2.0", RS_PLAN_SYNC="0")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ---------------------------------------------------------------- legacy database
# The schema as it was before products: brand kit columns on workspaces and
# brands, workspace-wide photos. db.init() must upgrade it in place.
print("== migration from the single brand kit")
LEGACY = """
CREATE TABLE workspaces (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0,
  brand_name TEXT, product_name TEXT, claims TEXT, audience TEXT, created_at TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free', plan_status TEXT, plan_override TEXT, plan_credits INTEGER NOT NULL DEFAULT 0,
  plan_granted INTEGER NOT NULL DEFAULT 0, plan_period_end TEXT, plan_cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
  stripe_customer_id TEXT, stripe_subscription_id TEXT, brand_id INTEGER);
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT, pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, workspace_id INTEGER NOT NULL, created_at TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member');
CREATE TABLE photos (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, path TEXT NOT NULL,
  filename TEXT, content_type TEXT, remote_url TEXT, created_at TEXT NOT NULL);
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  tool TEXT NOT NULL, template_id TEXT, brief TEXT, params_json TEXT, request_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
  error TEXT, credits INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0);
CREATE TABLE brands (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, brand_name TEXT,
  product_name TEXT, claims TEXT, audience TEXT, created_at TEXT NOT NULL);
CREATE TABLE copy_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER,
  brand_id INTEGER, kind TEXT NOT NULL, platform TEXT, input_json TEXT, output_json TEXT,
  credits INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER,
  brand_id INTEGER, name TEXT NOT NULL, brief_json TEXT, status TEXT NOT NULL DEFAULT 'planning', plan_json TEXT,
  steps_json TEXT, credits INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
T = "2026-10-01T00:00:00+00:00"
old = sqlite3.connect(os.path.join(TMP, "rs.db"))
old.executescript(LEGACY)
# Workspace 1: the owner's real shape (Scale comp, one brand kit, photos, ads, copy, a campaign).
old.execute("INSERT INTO workspaces (id, name, credits, brand_name, product_name, claims, audience, created_at, "
            "plan_override, plan_credits, brand_id) VALUES (1,'Studio',500,'Sunrise Co','Sunrise Blend',"
            "'Natural caffeine\n30 servings','Coffee lovers 25-44',?,'scale',18000,1)", (T,))
old.execute("INSERT INTO brands (id, workspace_id, brand_name, product_name, claims, audience, created_at) "
            "VALUES (1,1,'Sunrise Co','Sunrise Blend','Natural caffeine\n30 servings','Coffee lovers 25-44',?)", (T,))
for i in range(3):
    old.execute("INSERT INTO photos (workspace_id, path, filename, content_type, created_at) VALUES (1,?,?,?,?)",
                (os.path.join(TMP, f"p{i}.png"), f"bag{i}.png", "image/png", T))
for i in range(2):
    old.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, status, credits, created_at, updated_at) "
                "VALUES (1,1,'ad_image','Bag on a counter','done',22,?,?)", (T, T))
old.execute("INSERT INTO copy_runs (workspace_id, brand_id, kind, platform, created_at) VALUES (1,1,'ad','meta',?)", (T,))
old.execute("INSERT INTO campaigns (workspace_id, brand_id, name, created_at, updated_at) VALUES (1,1,'Fall',?,?)", (T, T))
# Workspace 2: two brands (the second without a product), a job that can't be attributed.
old.execute("INSERT INTO workspaces (id, name, brand_name, product_name, created_at, brand_id) "
            "VALUES (2,'Two',NULL,NULL,?,3)", (T,))
old.execute("INSERT INTO brands (id, workspace_id, brand_name, product_name, created_at) VALUES (2,2,'Alpha','Alpha Tea',?)", (T,))
old.execute("INSERT INTO brands (id, workspace_id, brand_name, product_name, created_at) VALUES (3,2,'Beta',NULL,?)", (T,))
old.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) VALUES (2,1,'ad_image','done',?,?)", (T, T))
# Workspace 3: brand kit only on the workspace row (no brand row yet).
old.execute("INSERT INTO workspaces (id, name, brand_name, product_name, claims, created_at) VALUES "
            "(3,'Three','Glow Co','GlowSerum','30 ml',?)", (T,))
old.commit()
old.close()

import db  # noqa: E402
db.init()
with db.connect() as conn:
    w1 = conn.execute("SELECT * FROM workspaces WHERE id=1").fetchone()
    p1 = conn.execute("SELECT * FROM products WHERE workspace_id=1").fetchall()
    ph = conn.execute("SELECT * FROM photos WHERE workspace_id=1").fetchall()
    j1 = conn.execute("SELECT * FROM jobs WHERE workspace_id=1").fetchall()
    cr = conn.execute("SELECT * FROM copy_runs").fetchone()
    cp = conn.execute("SELECT * FROM campaigns").fetchone()
    b1 = conn.execute("SELECT * FROM brands WHERE id=1").fetchone()
check(len(p1) == 1 and p1[0]["name"] == "Sunrise Blend" and p1[0]["claims"] == "Natural caffeine\n30 servings"
      and p1[0]["audience"] == "Coffee lovers 25-44" and p1[0]["brand_id"] == 1, "workspace 1's brand kit became its first product")
check(w1["product_id"] == p1[0]["id"] and b1["product_id"] == p1[0]["id"], "that product is active")
check(all(x["product_id"] == p1[0]["id"] for x in ph) and p1[0]["primary_photo_id"] == max(x["id"] for x in ph),
      "every photo moved to it; the newest is the primary photo")
check(all(j["product_id"] == p1[0]["id"] and j["brand_id"] == 1 for j in j1), "existing ads are tagged with the product")
check(cr["product_id"] == p1[0]["id"] and cp["product_id"] == p1[0]["id"], "copy runs and campaigns are tagged too")
check(w1["brand_name"] == "Sunrise Co" and w1["product_name"] == "Sunrise Blend" and w1["credits"] == 500
      and w1["plan_credits"] == 18000 and w1["plan_override"] == "scale", "workspace columns, credits and plan untouched")
with db.connect() as conn:
    p2 = conn.execute("SELECT * FROM products WHERE workspace_id=2").fetchall()
    w2 = conn.execute("SELECT * FROM workspaces WHERE id=2").fetchone()
    j2 = conn.execute("SELECT * FROM jobs WHERE workspace_id=2").fetchone()
    w3 = conn.execute("SELECT * FROM workspaces WHERE id=3").fetchone()
    p3 = conn.execute("SELECT * FROM products WHERE workspace_id=3").fetchall()
check(len(p2) == 1 and p2[0]["brand_id"] == 2 and w2["product_id"] is None and j2["product_id"] is None,
      "two brands: only the kit with a product becomes one; work isn't guessed at")
check(w3["brand_id"] and len(p3) == 1 and p3[0]["name"] == "GlowSerum", "a workspace without a brand row gets one, and its product")
db.init()
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3, "running the migration again adds nothing")

# ---------------------------------------------------------------- the app
import app as appmod  # noqa: E402
import catalog  # noqa: E402
import fetcher  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
import plans  # noqa: E402
import products  # noqa: E402
import worker  # noqa: E402

calls = {"submit": [], "upload": 0}
results = {}


def fake_submit(model, inputs, webhook_url=None):
    rid = f"req{len(calls['submit']) + 1}"
    calls["submit"].append((model, inputs))
    results[rid] = {"status": "in_progress"}
    return rid


def fake_upload(data, ctype):
    calls["upload"] += 1
    return f"https://files.example/{calls['upload']}.png"


hf.submit, hf.status, hf.upload = fake_submit, (lambda rid: results[rid]), fake_upload
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.1.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png(size=64, color=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (size, size), color).save(b, "PNG")
    b.seek(0)
    return b


def wsrow(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword", "name": email.split("@")[0]})
    with db.connect() as conn:
        ws_id = conn.execute("SELECT workspace_id FROM users WHERE email=?", (email,)).fetchone()[0]
    return c, ws_id


def comp(ws_id, plan):
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, ws_id))
        conn.commit()


def credits(ws_id, n):
    with db.connect() as conn:
        db.adjust_credits(conn, ws_id, n, "test top-up")
        conn.commit()


print("== products")
u, uws = signup("maker@example.com")
r = u.get("/app/products")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "Add the first product" in html and "Import from a link" in html,
      "a new workspace's products hub points at the first product")
r = u.post("/app/products/new", data={"csrf": csrf(u), "name": "Glow Serum", "price": "$34", "description": "A vitamin C serum.",
                                      "claims": "30 ml\nVitamin C", "audience": "Women 30-50", "tags": "Skin, Serum, skin",
                                      "photos": [(png(), "a.png"), (png(), "b.png")]},
           content_type="multipart/form-data")
check(r.status_code == 302 and "/app/products/" in r.headers["Location"], "product created")
with db.connect() as conn:
    gp = conn.execute("SELECT * FROM products WHERE workspace_id=?", (uws,)).fetchone()
    gph = conn.execute("SELECT * FROM photos WHERE product_id=?", (gp["id"],)).fetchall()
w = wsrow(uws)
check(gp["tags"] == "skin, serum" and len(gph) == 2 and gp["primary_photo_id"] == gph[0]["id"],
      "tags normalized, photos attached, the first one is primary")
check(w["product_id"] == gp["id"] and w["product_name"] == "Glow Serum" and w["claims"] == "30 ml\nVitamin C"
      and w["audience"] == "Women 30-50", "the new product is active and mirrored into the brand kit columns")
r = u.post(f"/app/products/{gp['id']}", data={"csrf": csrf(u), "name": "Glow Serum", "claims": "Cures acne overnight"})
with db.connect() as conn:
    check(conn.execute("SELECT claims FROM products WHERE id=?", (gp["id"],)).fetchone()[0] == "30 ml\nVitamin C",
          "a health claim isn't saved")
r = u.post(f"/app/products/{gp['id']}", data={"csrf": csrf(u), "name": "Glow Serum", "offer": "20% off this week"})
with db.connect() as conn:
    check(not conn.execute("SELECT offer FROM products WHERE id=?", (gp["id"],)).fetchone()[0], "a discount offer isn't saved")
u.post(f"/app/products/{gp['id']}/photos/{gph[1]['id']}/primary", data={"csrf": csrf(u)})
with db.connect() as conn:
    check(conn.execute("SELECT primary_photo_id FROM products WHERE id=?", (gp["id"],)).fetchone()[0] == gph[1]["id"],
          "another photo can be made primary")
u.post(f"/app/photos/{gph[1]['id']}/delete", data={"csrf": csrf(u)})
with db.connect() as conn:
    check(conn.execute("SELECT primary_photo_id FROM products WHERE id=?", (gp["id"],)).fetchone()[0] == gph[0]["id"],
          "deleting the primary photo promotes the next one")
r = u.post("/app/photos", data={"csrf": csrf(u), "product": str(gp["id"]), "photos": (png(), "c.png")},
           content_type="multipart/form-data")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (gp["id"],)).fetchone()[0] == 2,
          "upload adds photos to the chosen product")
u.post("/app/products/new", data={"csrf": csrf(u), "name": "Night Cream"})
with db.connect() as conn:
    nc = conn.execute("SELECT * FROM products WHERE workspace_id=? AND name='Night Cream'", (uws,)).fetchone()
check(wsrow(uws)["product_id"] == nc["id"], "a second product under the same brand (unlimited on every plan)")
html = u.get("/app").get_data(as_text=True)
check('id="sw-panel"' in html and "Glow Serum" in html and "Night Cream" in html and "Ctrl K" in html,
      "the switcher lists the brand's products on every page")
r = u.post("/app/switch", data={"csrf": csrf(u), "product": str(gp["id"]), "back": "/app/library"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/library") and wsrow(uws)["product_id"] == gp["id"],
      "one click switches the active product and goes back")
check(u.post("/app/switch", data={"csrf": csrf(u), "product": str(gp["id"]), "back": "https://evil.example/"}
             ).headers["Location"].endswith("/app"), "switch never redirects off the app")
check(u.post("/app/switch", data={"product": str(gp["id"])}).status_code == 400, "switching needs CSRF")
other, ows = signup("other@example.com")
check(other.post("/app/switch", data={"csrf": csrf(other), "product": str(gp["id"])}).status_code == 404,
      "can't switch to another workspace's product")
check(other.get(f"/app/products/{gp['id']}").status_code == 404, "can't open another workspace's product")
with appmod.app.test_request_context():
    from flask import g
    g.ws = wsrow(uws)
    g.brand_row, g.product_row = None, None
    import core
    check(core.brand(nc["id"])["product"] == "Night Cream" and core.brand(gp["id"])["claims"] == "30 ml\nVitamin C",
          "core.brand(product_id) reads any product of the workspace")

print("== brands and plan limits")
r = u.post("/app/brands", data={"csrf": csrf(u), "brand_name": "Second Co"})
with db.connect() as conn:
    check(len(products.brands(conn, uws)) == 1, "free plan: one brand, a second refused server-side")
comp(uws, "growth")
for name in ("Second Co", "Third Co", "Fourth Co"):
    u.post("/app/brands", data={"csrf": csrf(u), "brand_name": name})
with db.connect() as conn:
    bs = products.brands(conn, uws)
check(len(bs) == 3 and wsrow(uws)["brand_id"] == bs[-1]["id"] and wsrow(uws)["product_id"] is None,
      "Growth: three brands; the new brand is active and has no product yet")
html = u.get("/app/products").get_data(as_text=True)
check("Add the first product" in html and "Third Co" in html, "an empty brand points at its first product")
r = u.post("/app/products/new", data={"csrf": csrf(u), "brand": str(bs[1]["id"]), "name": "Second Thing"})
with db.connect() as conn:
    st = conn.execute("SELECT * FROM products WHERE name='Second Thing'").fetchone()
check(st["brand_id"] == bs[1]["id"] and wsrow(uws)["brand_id"] == bs[1]["id"], "a product goes under the brand it was added to")
u.post(f"/app/brands/{bs[1]['id']}/edit", data={"csrf": csrf(u), "brand_name": "Second Co", "about": "Teas", "tone": "Calm and premium"})
with db.connect() as conn:
    check(products.get_brand(conn, uws, bs[1]["id"])["tone"] == "Calm and premium", "brand details saved")
comp(uws, None)
check(plans.plan_code(wsrow(uws)) == "free", "downgraded to free")
u.post("/app/switch", data={"csrf": csrf(u), "product": str(st["id"])})
check(wsrow(uws)["brand_id"] == bs[1]["id"], "(the brand that was active stays as it was)")
u.post("/app/switch", data={"csrf": csrf(u), "brand": str(bs[0]["id"])})
check(wsrow(uws)["brand_id"] == bs[0]["id"], "the first brand stays usable after a downgrade")
r = u.post("/app/switch", data={"csrf": csrf(u), "product": str(st["id"])})
check(wsrow(uws)["brand_id"] == bs[0]["id"], "brands over the plan's limit are locked, not deleted")
check(u.post("/app/products/new", data={"csrf": csrf(u), "brand": str(bs[2]["id"]), "name": "X"}).status_code == 400,
      "no new products under a locked brand")
comp(uws, "growth")

print("== import from URL")
PRODUCT_PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<meta property="og:image" content="/img/ember-hero.jpg"><meta property="og:site_name" content="Hearth & Co">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Hearth"},
{"@type":"Product","name":"Ember Candle","description":"A soy wax candle with amber and cedar.",
 "image":["https://cdn.hearth.example/ember-1.jpg",{"url":"https://cdn.hearth.example/ember-2.jpg"}],
 "brand":{"@type":"Brand","name":"Hearth & Co"},"offers":{"@type":"Offer","price":"28.00","priceCurrency":"USD"}}]}</script>
</head><body><h1>Ember Candle</h1><p>Hand-poured soy wax. Burns for about 50 hours. Cotton wick. Amber and cedar scent.</p>
<img src="/img/logo.png"><img src="data:image/png;base64,xx"><img src="https://cdn.hearth.example/ember-3.webp"></body></html>"""
facts = appmod.product_routes.product_import.extract(PRODUCT_PAGE, "https://hearth.example/p/ember")
check(facts["name"] == "Ember Candle" and facts["price"] == "$28.00" and facts["brand"] == "Hearth & Co",
      "reads name, price and brand from the page's JSON-LD")
check(facts["images"][:2] == ["https://cdn.hearth.example/ember-1.jpg", "https://cdn.hearth.example/ember-2.jpg"]
      and "https://hearth.example/img/ember-hero.jpg" in facts["images"] and not any("logo" in i for i in facts["images"])
      and not any(i.startswith("data:") for i in facts["images"]), "candidate images: absolute, no logos or data URIs")
fetched = []


def fake_fetch(url, accept=None):
    fetched.append(url)
    if "private" in url:
        raise fetcher.FetchError("That address points to a private or internal network.")
    return {"url": url, "status": 200, "content_type": "text/html", "text": PRODUCT_PAGE}


IMG_OK = png(400).getvalue()
fetched_bytes = []


def fake_fetch_bytes(url, max_bytes=0, accept=None):
    fetched_bytes.append(url)
    data = png(100).getvalue() if "ember-2" in url else IMG_OK
    return {"url": url, "status": 200, "content_type": "image/png", "data": data}


fetcher.fetch, fetcher.fetch_bytes = fake_fetch, fake_fetch_bytes
llm_bodies = []


def fake_llm(body):
    llm_bodies.append(body)
    name = (body.get("tool_choice") or {}).get("name")
    if name == "submit_product":
        out = {"name": "Ember Candle", "description": "A hand-poured soy wax candle with amber and cedar.",
               "price": "$28", "offer": "Free shipping on two", "audience": "People who love cozy evenings",
               "claims": ["Hand-poured soy wax", "Burns for about 50 hours", "Cotton wick",
                          "Reduces anxiety and stress", "Loved by 10,000 customers"],
               "tags": ["candles", "home"], "brand": "Hearth & Co"}
        return {"model": body["model"], "usage": {"input_tokens": 3000, "output_tokens": 400},
                "content": [{"type": "tool_use", "id": "t1", "name": name, "input": out}]}
    raise AssertionError(f"unexpected LLM call {name}")


llm._anthropic = fake_llm
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
credits(uws, 1000)
bal = db.balance(wsrow(uws))
r = u.post("/app/products/import", data={"csrf": csrf(u), "url": "https://private.example/", "brand": str(bs[0]["id"])})
check(r.status_code == 302 and db.balance(wsrow(uws)) == bal, "an unreachable or private link: friendly error, nothing charged")
r = u.post("/app/products/import", data={"csrf": csrf(u), "url": "https://hearth.example/p/ember", "brand": str(bs[0]["id"])})
check(r.status_code == 302 and "/app/products/" in r.headers["Location"], "import creates a product and opens it")
with db.connect() as conn:
    ip = conn.execute("SELECT * FROM products WHERE name='Ember Candle'").fetchone()
pend = products.pending(ip)
check(llm_bodies[-1]["model"] == llm.fast_model() and "Burns for about 50 hours" in llm_bodies[-1]["messages"][0]["content"],
      "the page goes to the fast model")
check(ip["price"] == "$28" and ip["url"] == "https://hearth.example/p/ember" and ip["tags"] == "candles, home"
      and ip["audience"] == "People who love cozy evenings", "name, price, link, tags and audience prefilled")
check(not ip["claims"] and pend == ["Hand-poured soy wax", "Burns for about 50 hours", "Cotton wick"],
      "claims start unapproved; health claims and ones the page doesn't state are dropped")
check(not ip["offer"], "a free-shipping offer isn't carried over")
spent = bal - db.balance(wsrow(uws))
check(spent == llm.credits_for(llm.cost_usd(llm.fast_model(), 3000, 400)) and spent <= 5,
      f"the AI read is charged at cost x markup ({spent} credits)")
html = u.get(f"/app/products/{ip['id']}").get_data(as_text=True)
check("Suggested claims" in html and "3 to review" in html and "ember-1.jpg" in html, "product page shows claims to review and photo picks")
u.post(f"/app/products/{ip['id']}/claims", data={"csrf": csrf(u), "action": "approve", "i": ["0", "2"]})
with db.connect() as conn:
    ip = products.get_product(conn, uws, ip["id"])
check(ip["claims"] == "Hand-poured soy wax\nCotton wick" and products.pending(ip) == ["Burns for about 50 hours"],
      "approving moves only the picked claims")
u.post(f"/app/products/{ip['id']}/claims", data={"csrf": csrf(u), "action": "dismiss", "i": ["0"]})
with db.connect() as conn:
    ip = products.get_product(conn, uws, ip["id"])
check(products.pending(ip) == [], "dismissed suggestions are gone")
r = u.post(f"/app/products/{ip['id']}/import-photos", data={"csrf": csrf(u), "img": [
    "https://cdn.hearth.example/ember-1.jpg", "https://cdn.hearth.example/ember-2.jpg", "http://169.254.169.254/latest"]})
with db.connect() as conn:
    iph = conn.execute("SELECT * FROM photos WHERE product_id=?", (ip["id"],)).fetchall()
    ip = products.get_product(conn, uws, ip["id"])
check(fetched_bytes == ["https://cdn.hearth.example/ember-1.jpg", "https://cdn.hearth.example/ember-2.jpg"],
      "only images the import found are downloaded")
check(len(iph) == 1 and ip["primary_photo_id"] == iph[0]["id"], "too-small images are skipped; the good one becomes primary")
html = u.get(f"/app/products/{ip['id']}").get_data(as_text=True)
cands = html.split("From the product page")[1].split("Add selected photos")[0]
check("ember-1.jpg" not in cands and "ember-3.webp" in cands, "imported photos leave the candidate list")
os.environ.pop("ANTHROPIC_API_KEY", None)
n_before = len(llm_bodies)
bal = db.balance(wsrow(uws))
u.post("/app/products/import", data={"csrf": csrf(u), "url": "https://hearth.example/p/ember", "brand": str(bs[0]["id"])})
with db.connect() as conn:
    p2 = conn.execute("SELECT * FROM products WHERE name='Ember Candle' ORDER BY id DESC").fetchone()
check(len(llm_bodies) == n_before and db.balance(wsrow(uws)) == bal and p2["price"] == "$28.00"
      and products.pending(p2) == [], "without AI, the import still reads the page's facts for free")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"

print("== library by product")
with db.connect() as conn:
    for pid in (gp["id"], gp["id"], nc["id"]):
        conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, status, credits, created_at, updated_at, product_id, "
                     "brand_id) VALUES (?,?,?,?,?,?,?,?,?,?)", (uws, 1, "ad_image", f"brief for {pid}", "done", 22, T, T, pid,
                                                                gp["brand_id"]))
    conn.commit()
html = u.get(f"/app/library?product={gp['id']}").get_data(as_text=True)
check(html.count(f"brief for {gp['id']}") == 2 and f"brief for {nc['id']}" not in html, "library filters by product")
html = u.get(f"/app/library?brand={gp['brand_id']}").get_data(as_text=True)
check(f"brief for {nc['id']}" in html and f"brief for {gp['id']}" in html, "and by brand")

print("== pages render (phase 1)")
for path in ("/app", "/app/products", "/app/brand", "/app/products/new", f"/app/products/{gp['id']}",
             f"/app/brands/{bs[0]['id']}/edit", "/app/library"):
    r = u.get(path)
    check(r.status_code == 200, f"{path} renders")
    check(not re.search(r"higgsfield|corejava|marketplace global|morning upgrade|crowdcut|rector", r.get_data(as_text=True), re.I),
          f"{path} names no vendor or other business")

print(f"\nall {passed} product checks passed ({TMP})")
