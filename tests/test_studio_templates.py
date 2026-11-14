"""Studio templates view (studio_v2.shelves, web/v2/_templates.html): rows of template cards by what they make,
templates first until something is made, a Claude presets row while the MCP server is on.

    python tests/test_studio_templates.py
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
TMP = tempfile.mkdtemp(prefix="rs_studiotpl_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
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


submitted = []
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: submitted.append(model) or f"req{len(submitted)}"
hf.upload = lambda data, ctype: "https://files.example/1.png"
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


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def member(email, name="Ember Candle", credits=20000):
    """A signed-up, onboarded, email-confirmed member with one product (with a photo) and credits to spend."""
    import onboarding
    import products
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=? WHERE email=?", (db.now(), email))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": name, "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                         (user(email)["workspace_id"],)).fetchone()
        if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
            path = os.path.join(TMP, f"p-{p['id']}.png")
            open(path, "wb").write(png().getvalue())
            products.add_photo(conn, p["workspace_id"], p["id"], path, "p.png", "image/png")
        conn.execute("UPDATE products SET description=?, offer=?, claims=?, audience=? WHERE id=?",
                     ("A soy wax candle with amber and cedar.", "Free shipping this week",
                      "Burns for 40 hours\nHand poured in Texas", "People who light a candle every evening", p["id"]))
        if credits:
            db.adjust_credits(conn, p["workspace_id"], credits, "test top-up")
        conn.commit()
    return c, p["id"]




def page(c, q=""):
    r = c.get("/app/studio" + q)
    return r.status_code, r.get_data(as_text=True)


os.environ["UI_V2"] = "1"
os.environ["MCP_SERVER"] = "on"
c, pid = member("tpl@example.com")

# ---- shelves: rows, membership, trending
with appmod.app.test_request_context("/"):
    from flask import g as g_
    with db.connect() as conn:
        prod = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        g_.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (prod["workspace_id"],)).fetchone()
    gal = studio_v2.gallery(prod)
    rows = studio_v2.shelves(gal)
ids = [r["id"] for r in rows]
check(ids[0] == "trending" and "start" in ids and "ugc" in ids and "photos" in ids, "shelves: trending first, core rows present")
check(all(len(r["items"]) >= 2 for r in rows), "shelves: no row under two cards")
check([s[0] for s in studio_v2.SHELVES if s[0] in ids] == ids, "shelves: rows keep SHELVES order")
by = {x["id"]: x for x in gal}
check(all(by[i].get("example") for i in rows[0]["items"]), "trending: only cards with a real example")
check(len(rows[0]["items"]) <= studio_v2.SHELF_ROW, "trending: one row at most")
ugc = next(r for r in rows if r["id"] == "ugc")
check(all(by[i]["group"] != "image" for i in ugc["items"]), "ugc row: no still images")
start = next(r for r in rows if r["id"] == "start")
check(all(by[i]["group"] == "type" for i in start["items"]), "start row: the ad types")
known = set(by)
check(all(i in known for r in rows for i in r["items"]), "every shelf card is a gallery card (so pick() knows it)")
check(studio_v2.shelf_ids({"group": "image", "layout": "x", "uses": ["offer"]}) == ["promo"], "text layer with an offer -> promo")
check("apps" in studio_v2.shelf_ids({"group": "video", "mode": "presenter", "uses": ["show-your-app"]}), "show your app -> apps")

# ---- the page: templates first while nothing is made
st, html = page(c)
check(st == 200 and 'data-pane="templates"' in html and 'data-view="templates"' in html, "studio has the Templates view")
check(re.search(r'id="pane-tpl"[^>]*data-pane="templates"[^>]*tabindex="0"\s*>', html) is not None, "templates shown first with nothing made")
check(re.search(r'id="pane-ads"[^>]*hidden', html) is not None, "your ads hidden with nothing made")
check(html.count('class="tl-card"') >= 20, "template cards rendered")
check('data-tp-row="trending"' in html and 'data-tp-row="claude"' in html, "trending row and the Claude presets row")
check("/talking-ugc-review" in html or "/product-launch-kit" in html, "claude row shows preset slash names")
check("launch-on-meta" not in html, "launch preset not offered in Studio")
boot = json.loads(re.search(r"window.CFY_STUDIO = (.*?);</script>", html, re.S).group(1))
check(boot["view"] == "templates" and [r["id"] for r in boot["shelves"]][0] == "trending", "boot carries the view and shelves")
st, html = page(c, "?view=ads")
check(re.search(r'id="pane-tpl"[^>]*hidden', html) is not None, "?view=ads opens Your ads")

# with MCP off there is no Claude row
os.environ.pop("MCP_SERVER")
st, html = page(c)
check('data-tp-row="claude"' not in html, "no Claude row with the MCP server off")
os.environ["MCP_SERVER"] = "on"

# with something made, Your ads comes first
with db.connect() as conn:
    ws = user("tpl@example.com")["workspace_id"]
    bid = conn.execute("INSERT INTO batches (workspace_id, title, ad_type, created_at) VALUES (?,?,?,?)",
                       (ws, "t", "image", db.now())).lastrowid
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    row = {"workspace_id": ws, "batch_id": bid, "status": "done", "tool": "ad_image", "created_at": db.now(),
           "product_id": pid, "credits": 0, "inputs": "{}", "template": "", "model": "x"}
    notnull = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(jobs)") if r["notnull"] and r["dflt_value"] is None and not r["pk"]}
    for k, t in notnull.items():
        row.setdefault(k, user("tpl@example.com")["id"] if k == "user_id" else (0 if "INT" in t.upper() else ""))
    row = {k: v for k, v in row.items() if k in cols}
    conn.execute(f"INSERT INTO jobs ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    conn.commit()
st, html = page(c)
check(st == 200 and re.search(r'id="pane-tpl"[^>]*hidden', html) is not None, "with ads made, Your ads opens first")
check('class="st-n">1' in html, "the Your ads tab counts them")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
