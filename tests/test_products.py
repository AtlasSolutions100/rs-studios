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
os.environ.update(RS_VIDEO_USD_PER_SECOND="0.206", RS_TALKING_USD_PER_SECOND="0.206")  # fixtures use the old round numbers
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
backups = [f for f in os.listdir(TMP) if f.startswith("rs.db.pre-products-")]
check(len(backups) == 1 and sqlite3.connect(os.path.join(TMP, backups[0])).execute(
    "SELECT product_name FROM workspaces WHERE id=1").fetchone()[0] == "Sunrise Blend", "a copy of the database was kept before migrating")

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

print("== create flow")
import planner  # noqa: E402
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
plan_prompts = []


def fake_llm2(body):
    llm_bodies.append(body)
    name = (body.get("tool_choice") or {}).get("name")
    use = {"input_tokens": 1500, "output_tokens": 700}
    if any(t.get("name") == "get_brand_and_catalog" for t in body.get("tools") or []):
        plan_prompts.append(body["messages"][0]["content"])
        return {"model": body["model"], "usage": use, "content": [{"type": "tool_use", "id": "f", "name": "finish",
                "input": {"summary": "Test one angle.", "checklist": [{"title": "Pixel"}]}}]}
    if name == "submit_angles":
        out = {"angles": [{"title": "The 2 PM slump", "hook": "Your afternoon coffee takes 30 seconds now.",
                           "insight": "Afternoons drag", "audience": "Office workers"},
                          {"title": "Lose weight", "hook": "Burn fat while you sip", "insight": "x"},
                          {"title": "Cafe line", "hook": "Skip the cafe line.", "insight": "Time", "audience": "Commuters"}]}
    elif name == "submit_script":
        out = {"hooks": ["I stopped paying seven dollars for coffee.", "I lost 10 pounds drinking this",
                         "My results after a week were crazy", "This takes thirty seconds."],
               "body": "One scoop of Glow Serum in my routine and I'm out the door. Grab yours today.",
               "on_screen": ["30 seconds", "Make $500 a week"],
               "headlines": ["Glow in a minute", "Skip the line"], "concept": "Serum on a sunny shelf.",
               "scenes": ["Slow pan across the bottle on marble.", "Cure your acne fast"]}
    else:
        return fake_llm(body)
    return {"model": body["model"], "usage": use, "content": [{"type": "tool_use", "id": "t", "name": name, "input": out}]}


llm._anthropic = fake_llm2
u.post("/app/switch", data={"csrf": csrf(u), "product": str(gp["id"])})
credits(uws, 5000)
r = u.get("/app/create?type=ugc")
html = r.get_data(as_text=True)
boot = json.loads(re.search(r'<script id="cf-boot" type="application/json">(.*?)</script>', html, re.S).group(1))
check(r.status_code == 200 and boot["product"]["id"] == gp["id"] and boot["prefill"]["type"] == "ugc"
      and len(boot["presenters"]) == 8 and boot["caps"]["talking"] and not boot["caps"]["lipsync_own_audio"],
      "Create opens on the active product with the presenters and what the backend can do")
check(boot["prices"]["ugc_video"]["10"] == catalog.price_credits("ugc_video", duration=10) if isinstance(
    list(boot["prices"]["ugc_video"])[0], str) else True, "the page carries the credit prices")


def jpost(c, url, body, with_csrf=True):
    return c.post(url, data=json.dumps(body), content_type="application/json",
                  headers={"X-CSRF": csrf(c)} if with_csrf else {})


check(jpost(u, "/app/create/angles", {"product": gp["id"], "type": "ugc"}, with_csrf=False).status_code == 400,
      "the JSON endpoints need CSRF")
bal = db.balance(wsrow(uws))
r = jpost(u, "/app/create/angles", {"product": gp["id"], "type": "ugc"})
a = r.get_json()
check([x["title"] for x in a["angles"]] == ["The 2 PM slump", "Cafe line"], "angle ideas come back; a weight-loss one is dropped")
check(a["charged"] == bal - db.balance(wsrow(uws)) and 0 < a["charged"] <= 3 and llm_bodies[-1]["model"] == llm.fast_model()
      and "Glow Serum" in llm_bodies[-1]["messages"][0]["content"] and "30 ml" in llm_bodies[-1]["messages"][0]["content"],
      f"ideas use the product and its approved claims, on the fast model, charged at cost ({a['charged']} credits)")
check(jpost(other, "/app/create/angles", {"product": gp["id"], "type": "ugc"}).status_code == 404,
      "another workspace's product can't be used")
r = jpost(u, "/app/create/script", {"product": gp["id"], "type": "ugc", "angle": a["angles"][0], "duration": 10, "n": 4})
sc = r.get_json()
check(sc["items"] == ["I stopped paying seven dollars for coffee.", "This takes thirty seconds."] and "Grab yours" in sc["body"],
      "script hooks come back; a weight-loss hook and a customer-results hook are dropped")
check(sc["on_screen"] == ["30 seconds"] and "10-second spoken script" in llm_bodies[-1]["messages"][0]["content"]
      and str(catalog.max_words(10)) in llm_bodies[-1]["messages"][0]["content"], "captions checked too; the length sets the word budget")
cfg = {"product": gp["id"], "type": "ugc", "angle": a["angles"][0], "items": sc["items"], "body": sc["body"],
       "presenters": ["maya", "marcus"], "voice": "", "duration": 10, "aspects": ["9:16"], "platforms": ["tiktok"]}
q = jpost(u, "/app/create/quote", cfg).get_json()
each = catalog.price_credits("ugc_video", duration=10)
check(q["count"] == 4 and q["total"] == 4 * each and q["each"] == each, f"2 hooks x 2 presenters = 4 videos, {4 * each} credits")
for bad, why in ((dict(cfg, presenters=[]), "presenter"), (dict(cfg, body=" ".join(["word"] * 60)), "words"),
                 (dict(cfg, items=["I lost 10 pounds"]), "weight"), (dict(cfg, items=["My results were amazing"]), "customers"),
                 (dict(cfg, product=nc["id"]), "photo"), (dict(cfg, presenters=["maya", "marcus", "priya", "sam"],
                                                               aspects=["9:16", "1:1", "16:9"]), "at most")):
    e = jpost(u, "/app/create/quote", bad).get_json()
    check(e["count"] == 0 and why in e["error"].lower(), f"refused: {e['error'][:70]}")
bal = db.balance(wsrow(uws))
r = jpost(u, "/app/create/generate", dict(cfg, total=q["total"] - 1))
check(r.status_code == 409 and db.balance(wsrow(uws)) == bal, "a total that changed is refused, nothing charged")
r = jpost(u, "/app/create/generate", dict(cfg, total=q["total"]))
gen = r.get_json()
with db.connect() as conn:
    bjobs = conn.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (gen["batch"],)).fetchall()
    sa = conn.execute("SELECT * FROM angles WHERE product_id=?", (gp["id"],)).fetchall()
    ss = conn.execute("SELECT * FROM scripts WHERE product_id=?", (gp["id"],)).fetchall()
check(gen["ok"] and len(bjobs) == 4 and db.balance(wsrow(uws)) == bal - 4 * each, "generate queues the batch and takes its credits")
pj = [json.loads(j["params_json"]) for j in bjobs]
check(all(j["tool"] == "ugc_video" and j["product_id"] == gp["id"] and j["brand_id"] == gp["brand_id"] for j in bjobs)
      and [x["presenter"] for x in pj] == ["maya", "marcus", "maya", "marcus"] and pj[1]["voice"] == "calm_m",
      "each job is tagged with the product; presenters get a fitting voice")
check(len(sa) == 1 and sa[0]["title"] == "The 2 PM slump" and len(ss) == 1 and json.loads(ss[0]["on_screen"])["items"] == sc["items"],
      "the angle and script are saved to the product for reuse")
estimates = []
hf.estimate = lambda model, inputs: (estimates.append((model, inputs)) or 2.06)
worker.requests.get = lambda url, timeout=0: type("R", (), {"content": b"x", "raise_for_status": lambda self: None})()
worker.tick()
model, inputs = calls["submit"][0]
check(model == "bytedance/seedance-2.5/reference-to-video" and inputs["generate_audio"] and inputs["duration"] == 10
      and inputs["aspect_ratio"] == "9:16" and inputs["image_urls"] and estimates, "talking videos go to reference-to-video with sound")
check("curly dark hair" in inputs["prompt"] and '"I stopped paying seven dollars for coffee. One scoop' in inputs["prompt"]
      and "Glow Serum" in inputs["prompt"] and "warm, friendly female voice" in inputs["prompt"]
      and "exactly as in the reference photos" in inputs["prompt"], "the prompt carries presenter, voice, the exact script and the product")
html = u.get(gen["url"]).get_data(as_text=True)
check("Maya · 9:16" in html and "Marcus · 9:16" in html and "Make more like this" in html and "api/jobs" in html,
      "the review grid labels each version and polls for results")
hf.estimate = lambda model, inputs: 9.99
one = dict(cfg, items=cfg["items"][:1], presenters=["maya"])
q1 = jpost(u, "/app/create/quote", one).get_json()
g1 = jpost(u, "/app/create/generate", dict(one, total=q1["total"])).get_json()
bal = db.balance(wsrow(uws))
n_sub = len(calls["submit"])
worker.tick()
with db.connect() as conn:
    over = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (g1["batch"],)).fetchone()
check(over["status"] == "failed" and "price changed" in over["error"] and db.balance(wsrow(uws)) == bal + each
      and len(calls["submit"]) == n_sub, "if the vendor's price jumped, the video isn't started and is refunded")
hf.estimate = lambda model, inputs: None
img = {"product": gp["id"], "type": "image", "angle": a["angles"][1], "items": ["Glow in a minute", "Skip the line"],
       "body": "Serum on a sunny shelf.", "aspects": ["4:5", "9:16"], "versions": 2}
q = jpost(u, "/app/create/quote", img).get_json()
check(q["count"] == 4 and q["total"] == 4 * catalog.price_credits("ad_image", batch=2), "image ads: 2 headlines x 2 shapes, 2 versions each")
r = jpost(u, "/app/create/generate", dict(img, total=q["total"])).get_json()
with db.connect() as conn:
    ij = conn.execute("SELECT * FROM jobs WHERE batch_id=?", (r["batch"],)).fetchall()
check(all(j["tool"] == "ad_image" and 'Headline: "' in j["brief"] for j in ij)
      and {json.loads(j["params_json"])["aspect"] for j in ij} == {"4:5", "9:16"}, "image jobs carry the headline and shape")
pv = {"product": gp["id"], "type": "product_video", "angle": {"title": "Shelf"}, "items": ["Slow pan across the bottle on marble."],
      "duration": 10}
q = jpost(u, "/app/create/quote", pv).get_json()
check(q["count"] == 1 and q["total"] == catalog.price_credits("photo_to_video", duration=10), "product video: one scene, 10 seconds")
os.environ["RS_TALKING"] = "0"
check("ugc" not in [t["id"] for t in catalog.ad_types()] and "pick an ad type" in
      jpost(u, "/app/create/quote", cfg).get_json()["error"].lower(), "with talking switched off, the format is hidden and refused")
os.environ.pop("RS_TALKING")
r = jpost(u, "/app/create/save", {"what": "angle", "product": gp["id"], "angle": {"title": "Desk drawer", "hook": "Keep one at work."}})
check(r.get_json()["ok"], "angles can be saved without generating")
html = u.get("/app").get_data(as_text=True)
check("Desk drawer" in html and "Start a new ad" in html and "UGC presenter" in html and "Recent batches" in html,
      "Create home shows ad types, saved angles and recent batches")

print("== planner hands off to Create")
with db.connect() as conn:
    cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, brand_id, product_id, name, brief_json, status, plan_json, "
                       "steps_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (uws, 1, gp["brand_id"], gp["id"], "Fall", "{}", "ready",
                        json.dumps({"angles": [{"name": "Morning", "insight": "Rushed", "audience": "Parents"}],
                                    "creative": [{"angle": "Morning", "hooks": ["Mornings are chaos."]}]}), "[]", T, T)).lastrowid
    photos = [{"id": x["id"]} for x in products.photos_for(conn, uws, gp["id"])]
    out = planner.propose(conn, cid, uws, {"tool": "ugc_video", "angle": "Morning", "platform": "tiktok", "duration": 8,
                                           "brief": " ".join(["long"] * 40)}, photos, [])
    check("words" in out.get("error", ""), "the planner can't propose a script too long for its length")
    out = planner.propose(conn, cid, uws, {"tool": "ugc_video", "angle": "Morning", "platform": "tiktok", "duration": 10,
                                           "presenter": "sam", "brief": "Mornings are chaos. This takes thirty seconds."},
                          photos, [])
    cad = conn.execute("SELECT * FROM campaign_ads WHERE id=?", (out["ad_id"],)).fetchone()
check(out["ok"] and out["credits"] == each and json.loads(cad["params_json"])["presenter"] == "sam",
      "the planner proposes talking videos with a presenter and a price")
html = u.get(f"/app/campaigns/{cid}").get_data(as_text=True) if plans.has(wsrow(uws), "planner") else ""
check("Open in Create" in html, "each proposed ad links into Create")
r = u.get(f"/app/create?from_ad={cad['id']}")
boot = json.loads(re.search(r'<script id="cf-boot" type="application/json">(.*?)</script>', r.get_data(as_text=True), re.S).group(1))
pf = boot["prefill"]
check(pf["type"] == "ugc" and pf["items"] == ["Mornings are chaos. This takes thirty seconds."] and pf["presenters"] == ["sam"]
      and pf["angle"]["title"] == "Morning" and pf["platforms"] == ["tiktok"] and boot["product"]["id"] == gp["id"],
      "Create opens with the proposal's product, type, script, presenter and platform")
hand = {"product": gp["id"], "type": "ugc", "angle": pf["angle"], "items": pf["items"], "presenters": ["sam"], "duration": 10,
        "aspects": ["9:16"], "from_ad": cad["id"]}
q = jpost(u, "/app/create/quote", hand).get_json()
jpost(u, "/app/create/generate", dict(hand, total=q["total"]))
with db.connect() as conn:
    check(conn.execute("SELECT status FROM campaign_ads WHERE id=?", (cad["id"],)).fetchone()[0] == "queued",
          "generating from Create marks the proposal as made")
check(u.get(f"/app/create?from_ad={cad['id']}").status_code == 200 and other.get(f"/app/create?from_ad={cad['id']}").status_code == 200
      and "Mornings are chaos" not in other.get(f"/app/create?from_ad={cad['id']}").get_data(as_text=True),
      "another workspace can't read the proposal")

print("== product pickers in every flow")
r = u.get(f"/app/new?template=hero&product={nc['id']}")
check("Night Cream" in r.get_data(as_text=True) and 'name="product"' in r.get_data(as_text=True), "templates fill from the picked product")
u.post("/app/generate", data={"csrf": csrf(u), "tool": "ad_image", "template": "hero", "brief": "Cream on a shelf",
                              "product": str(nc["id"]), "photo": str(gph[0]["id"])})
with db.connect() as conn:
    jj = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC", (uws,)).fetchone()
check(jj["product_id"] == nc["id"] and jj["brief"] == "Cream on a shelf", "the job is tagged with the picked product")
u.post("/app/generate", data={"csrf": csrf(u), "tool": "ad_image", "brief": "x", "product": str(ip["id"] + 999)})
with db.connect() as conn:
    jj2 = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC", (uws,)).fetchone()
check(jj2["product_id"] == gp["id"], "an unknown product falls back to the active one")
check("Night Cream" in u.get(f"/app/copy?product={nc['id']}").get_data(as_text=True), "copy studio fills from the picked product")
llm_bodies.clear()
u.post("/app/campaigns", data={"csrf": csrf(u), "offer": "Night Cream", "product": str(nc["id"]), "platforms": ["meta"]})
with db.connect() as conn:
    camp = conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", (uws,)).fetchone()
check(camp["product_id"] == nc["id"] and plan_prompts and "Product: Night Cream" in str(plan_prompts[-1]),
      "the planner plans for the picked product")
for path in ("/app/seo", "/app/pages", f"/app/new?tool=photo_to_video&product={gp['id']}", "/app/campaigns", "/app/copy"):
    check('data-ppick' in u.get(path).get_data(as_text=True), f"{path} has the product picker")

print("== pages render (phase 2)")
for path in ("/app", "/app/create", "/app/create?type=image", f"/app/create?product={nc['id']}", gen["url"]):
    r = u.get(path)
    check(r.status_code == 200 and not re.search(r"higgsfield|seedance|bytedance", r.get_data(as_text=True), re.I),
          f"{path} renders and never names the vendor")

print("== setup assistant")
import assistant  # noqa: E402
asst_q, asst_bodies = [], []


def tu2(name, inp, i=[0]):
    i[0] += 1
    return {"type": "tool_use", "id": f"toolu_a{i[0]}", "name": name, "input": inp}


def txt(t):
    return {"type": "text", "text": t}


def fake_asst(body):
    if any(t.get("name") == "get_setup_state" for t in body.get("tools") or []):
        asst_bodies.append(body)
        content = asst_q.pop(0) if asst_q else [txt("Okay.")]
        if (body.get("tool_choice") or {}).get("type") == "none":
            content = [b for b in content if b["type"] == "text"] or [txt("Done.")]
        return {"model": body["model"], "usage": {"input_tokens": 3000, "output_tokens": 150}, "content": content,
                "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"}
    return fake_llm2(body)


llm._anthropic = fake_asst
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
z = client()
r = z.post("/signup", data={"csrf": csrf(z), "email": "newbie@example.com", "password": "longpassword"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/setup"), "with AI on, a new account lands on the setup guide")
with db.connect() as conn:
    zws = conn.execute("SELECT workspace_id FROM users WHERE email='newbie@example.com'").fetchone()[0]
html = z.get("/app/setup").get_data(as_text=True)
check('class="asst inline panel"' in html and 'asst drawer' not in html and "never writes claims for you" in html,
      "the setup page shows the guide inline (no second drawer)")
html = z.get("/app").get_data(as_text=True)
check("data-help-open" in html and "asst drawer" in html and "assistant.js" in html, "every screen has the Help me drawer")
bal0 = db.balance(wsrow(zws))


def say(c, text=None, **kw):
    body = dict(kw, text=text) if text else kw
    return jpost(c, "/app/assistant/say", body).get_json()


asst_q[:] = [[tu2("get_setup_state", {})], [txt("Hi! What's your brand called?"), tu2("offer_choices", {"choices": ["Hearth & Co", "I'll type it"]})],
             [txt("Hi! What's your brand called?")]]
r = say(z, start=True, screen="assistant.page")
items = r["items"]
check([i["role"] for i in items][:2] == ["assistant", "card"] and items[1]["card"]["kind"] == "offer_choices"
      and items[1]["card"]["choices"][0] == "Hearth & Co", "the guide greets and offers quick replies")
check(asst_bodies[0]["model"] == llm.fast_model() and "never" in asst_bodies[0]["system"][0]["text"].lower()
      and r["progress"][0] == {"step": "brand", "done": False}, "it runs on the fast model with the rules in its system prompt")
w = wsrow(zws)
check(db.balance(w) == bal0 and w["assistant_free_used"] > 0 and r["free_left"] == assistant.free_credits() - w["assistant_free_used"],
      "setup replies come out of the free allowance, not credits")
check(say(z, start=True)["items"] == [], "opening it again doesn't greet twice")
asst_q[:] = [[tu2("create_brand", {"name": "Hearth & Co"})], [txt("Nice. Do you have a product page link?")]]
r = say(z, "It's called Hearth & Co")
with db.connect() as conn:
    zb = products.brands(conn, zws)
check(len(zb) == 1 and zb[0]["brand_name"] == "Hearth & Co" and r["progress"][0]["done"], "it names the brand (no extra brand made)")
n_prod = 0
asst_q[:] = [[tu2("propose_import", {"url": "https://hearth.example/p/ember"})], [txt("Tap Import and I'll read it.")]]
r = say(z, "Sure: https://hearth.example/p/ember")
card = [i["card"] for i in r["items"] if i["role"] == "card"][0]
with db.connect() as conn:
    n_prod = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?", (zws,)).fetchone()[0]
check(card["kind"] == "propose_import" and card["status"] == "open" and card["credits"] > 0 and n_prod == 0
      and db.balance(wsrow(zws)) == bal0, "importing is only offered on a card; nothing is read or charged yet")
asst_q[:] = [[tu2("review_suggested_claims", {})], [txt("Tick the ones on your label.")]]
r = jpost(z, "/app/assistant/action", {"card": card["id"], "choice": "confirm"}).get_json()
with db.connect() as conn:
    zp = conn.execute("SELECT * FROM products WHERE workspace_id=?", (zws,)).fetchone()
spent = bal0 - db.balance(wsrow(zws))
check(zp and zp["name"] == "Ember Candle" and not zp["claims"] and len(products.pending(zp)) == 3 and r["card"]["status"] == "done"
      and spent == r.get("import_charged") and 0 < spent <= 3, f"confirming imports the product (charged {spent} credits for the AI read)")
ccard = [i["card"] for i in r["items"] if i["role"] == "card"][0]
check(ccard["kind"] == "review_suggested_claims" and ccard["claims"][0] == "Hand-poured soy wax", "then it asks which suggested claims are true")
check(jpost(z, "/app/assistant/action", {"card": card["id"], "choice": "confirm"}).get_json()["error"].startswith("That card"),
      "a card can't be confirmed twice")
asst_q[:] = [[txt("Got it. Anything else printed on the label?")]]
jpost(z, "/app/assistant/action", {"card": ccard["id"], "choice": "confirm", "picks": [0, 2]})
with db.connect() as conn:
    zp = products.get_product(conn, zws, zp["id"])
check(zp["claims"] == "Hand-poured soy wax\nCotton wick", "only the claims the member ticked are approved")
asst_q[:] = [[tu2("save_claims", {"claims": ["Burns about 50 hours", "Clinically proven to calm you"]})], [txt("Saved.")]]
say(z, "The label also says: burns about 50 hours")
res = json.loads([b for m in asst_bodies[-1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                  for b in m["content"] if b.get("type") == "tool_result"][-1]["content"])
with db.connect() as conn:
    zp = products.get_product(conn, zws, zp["id"])
check(res["saved"] == ["Burns about 50 hours"] and "didn't type" in res["refused"][0]
      and zp["claims"].endswith("Burns about 50 hours"), "it saves claims the member typed and refuses ones it made up")
asst_q[:] = [[tu2("save_claims", {"claims": ["It cures insomnia"]})], [txt("I can't save that one.")]]
say(z, "It cures insomnia")
with db.connect() as conn:
    check("insomnia" not in products.get_product(conn, zws, zp["id"])["claims"], "typed claims still have to pass the rules")
asst_q[:] = [[tu2("update_product", {"audience": "People who love cozy evenings at home"}), tu2("set_goal", {"goal": "sales", "platforms": ["meta", "tiktok", "nope"]})],
             [tu2("propose_first_ad", {"kind": "image", "template": "hero"})], [txt("Want me to make it?")]]
r = say(z, "Cozy home people, sales on Meta and TikTok")
fa = [i["card"] for i in r["items"] if i["role"] == "card"][0]
setup = json.loads(wsrow(zws)["setup_json"])
with db.connect() as conn:
    zp = products.get_product(conn, zws, zp["id"])
    nj = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (zws,)).fetchone()[0]
check(zp["audience"] == "People who love cozy evenings at home" and setup == {"goal": "sales", "platforms": ["meta", "tiktok"]},
      "audience, goal and platforms saved")
check(fa["kind"] == "propose_first_ad" and fa["credits"] == catalog.price_credits("ad_image", batch=1) and nj == 0,
      "the first ad is offered on a card with its price; nothing made yet")
bal1 = db.balance(wsrow(zws))
asst_q[:] = [[txt("It's on its way. Want to set up your ad accounts next?"), tu2("show_link", {"target": "ad_accounts", "label": "Ad accounts"})], [txt("")]]
r = jpost(z, "/app/assistant/action", {"card": fa["id"], "choice": "confirm"}).get_json()
with db.connect() as conn:
    fj = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (zws,)).fetchone()
check(fj and fj["tool"] == "ad_image" and fj["template_id"] == "hero" and fj["product_id"] == zp["id"] and "Ember Candle" in fj["brief"]
      and db.balance(wsrow(zws)) == bal1 - fj["credits"], "confirming makes the first ad at the price on the card")
lk = [i["card"] for i in r["items"] if i["role"] == "card" and i["card"]["kind"] == "show_link"][0]
check(lk["url"] == "/app/ad-accounts" and r["progress"][6] == {"step": "first_ad", "done": True}, "links resolve to real screens; progress updates")
asst_q[:] = [[tu2("generate_ads", {"n": 50})], [txt("I can't do that from here.")]]
say(z, "Make me 50 videos now")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (zws,)).fetchone()[0] == 1,
          "the guide has no way to spend credits on its own")
thread = z.get("/app/assistant").get_json()
check([i["role"] for i in thread["items"]].count("card") >= 5 and thread["items"][0]["role"] == "assistant",
      "the conversation is kept and can be resumed")
check(jpost(u, "/app/assistant/action", {"card": fa["id"], "choice": "confirm"}).status_code == 404,
      "another member can't answer this member's cards")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET assistant_free_used=? WHERE id=?", (assistant.free_credits(), zws))
    conn.commit()
n_calls = len(asst_bodies)
r = say(z, "What next?")
check(r.get("need_ok") and len(asst_bodies) == n_calls, "after the free allowance it asks before replies cost credits")
jpost(z, "/app/assistant/ok", {})
bal2 = db.balance(wsrow(zws))
asst_q[:] = [[txt("Next, set up your ad accounts.")]]
r = say(z, "What next?")
check(r["charged"] == bal2 - db.balance(wsrow(zws)) == llm.credits_for(llm.cost_usd(llm.fast_model(), 3000, 150)),
      f"then replies cost what they use ({r['charged']} credit)")
jpost(z, "/app/assistant/reset", {})
check(z.get("/app/assistant").get_json()["items"] == [], "start over begins a fresh conversation (the setup stays)")
check(assistant.typed_by_member("Burns about 50 hours", ["the label says it burns about 50 hours"])
      and not assistant.typed_by_member("Burns about 80 hours", ["burns about 50 hours"]), "claims must match what was typed")
os.environ.pop("ANTHROPIC_API_KEY")
check("data-help-open" not in z.get("/app").get_data(as_text=True) and z.get("/app/setup").status_code == 302,
      "with AI off, the guide is hidden and /app/setup goes to the quick form")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"

print(f"\nall {passed} product checks passed ({TMP})")
