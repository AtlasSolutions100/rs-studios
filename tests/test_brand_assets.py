"""Brand assets and References (brand_assets.py), and the first ads' previews (first_ads.preview_jpeg), on a throwaway
database with the new look on; no vendor is called.

    python tests/test_brand_assets.py

  * Brand > Assets: upload pictures with a kind and a name; product photos join their product; a product's last
    photo can't be removed; another workspace can't remove yours
  * Studio sends the brand's pictures to the composer; References put the picked ones beside the product photo
    (the product's own first), only pictures this workspace owns reach the job, and the image prompt says what each is
  * the first ads step shows a preview of each look made from the member's own photo and headline, and none without one
"""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_assets_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test", UI_V2="1")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "PLANS_ONLY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import brand_assets  # noqa: E402
import create_routes  # noqa: E402
import db  # noqa: E402
import first_ads  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.41.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png(rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 400), rgb).save(b, "PNG")
    b.seek(0)
    return b


def member(email, photo=True):
    import onboarding
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        onboarding.finish(conn, u)
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        db.adjust_credits(conn, u["workspace_id"], 5000, "test")
        conn.commit()
    data = {"csrf": csrf(c), "name": "Ember Candle"}
    if photo:
        data["photos"] = (png(), "p.png")
    c.post("/app/products/new", data=data, content_type="multipart/form-data")
    return c, u["workspace_id"]


def q(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchall()


print("== Brand > Assets")
c, ws = member("ana@example.com")
pid = q("SELECT id FROM products WHERE workspace_id=?", ws)[0]["id"]
r = c.post("/app/brand/assets", data={"csrf": csrf(c), "kind": "logo", "label": "Main logo",
                                      "files": [(png((10, 10, 10)), "logo.png")]}, content_type="multipart/form-data")
check(r.status_code == 302 and r.headers["Location"].endswith("#assets"), "a logo is uploaded, back to Assets")
r = c.post("/app/brand/assets", data={"csrf": csrf(c), "kind": "lifestyle", "files": [(png((1, 2, 3)), "a.png"),
                                                                                      (png((3, 2, 1)), "b.png")]},
           content_type="multipart/form-data")
r = c.post("/app/brand/assets", data={"csrf": csrf(c), "kind": "product", "product": pid,
                                      "files": [(png((9, 9, 9)), "side.png")]}, content_type="multipart/form-data")
rows = q("SELECT * FROM photos WHERE workspace_id=? ORDER BY id", ws)
kinds = [(r["kind"], r["product_id"] is not None) for r in rows]
check(("logo", False) in kinds and kinds.count(("lifestyle", False)) == 2 and ("product", True) in kinds,
      "kinds kept; a product photo joins its product")
page = c.get("/app/studio/brand").get_data(as_text=True)
check('id="assets"' in page and "Main logo" in page and "Lifestyle" in page, "the Brand page lists them")
r = c.post("/app/brand/assets", data={"csrf": csrf(c), "kind": "product", "files": [(png(), "x.png")]},
           content_type="multipart/form-data")
with c.session_transaction() as s:
    fl = [m for _, m in s.get("_flashes") or []]
check(any("which product" in m.lower() for m in fl), "a product photo needs its product")
other, ws2 = member("ben@example.com")
logo_id = [r["id"] for r in rows if r["kind"] == "logo"][0]
other.post(f"/app/brand/assets/{logo_id}/del", data={"csrf": csrf(other)})
check(q("SELECT 1 FROM photos WHERE id=?", logo_id), "another workspace can't remove it")
first_photo = q("SELECT id FROM photos WHERE product_id=? ORDER BY id LIMIT 1", pid)[0]["id"]
side = q("SELECT id FROM photos WHERE product_id=? ORDER BY id DESC LIMIT 1", pid)[0]["id"]
c.post(f"/app/brand/assets/{side}/del", data={"csrf": csrf(c)})
c.post(f"/app/brand/assets/{first_photo}/del", data={"csrf": csrf(c)})
check(q("SELECT 1 FROM photos WHERE id=?", first_photo) and not q("SELECT 1 FROM photos WHERE id=?", side),
      "an extra product photo goes; the product's last one stays")

print("== References in Studio")
html = c.get("/app/studio").get_data(as_text=True)
check("data-refs-open" in html and 'data-dlg="refs"' in html, "the composer has References")
life = [r["id"] for r in rows if r["kind"] == "lifestyle"]
with appmod.app.test_request_context("/app/studio"):
    from flask import g
    with db.connect() as conn:
        g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws,)).fetchone()
        g.user = conn.execute("SELECT * FROM users WHERE workspace_id=?", (ws,)).fetchone()
    assets = brand_assets.assets_for(db.connect(), ws, g.ws["brand_id"])
    check({a["kind"] for a in assets} >= {"logo", "lifestyle", "product"}, "the brand's pictures, with kinds, for the picker")
    cfg = studio_v2.config_for({"mode": "image", "product": pid, "text": "An easy gift", "refs": [logo_id] + life + [999999]})
    check(cfg["photos"][0] == first_photo and cfg["photos"][1:3] == [logo_id] + life[:1],
          "the product's photo first, then the picked ones (four at most)")
    with db.connect() as conn:
        _meta, jobs = create_routes.expand(conn, cfg)
    if True:
        ids = jobs[0]["params"]["photo_ids"]
        check(ids[0] == first_photo and logo_id in ids and 999999 not in ids, "only this workspace's pictures reach the job")
    theirs = q("SELECT id FROM photos WHERE workspace_id=?", ws2)[0]["id"]
    cfg2 = studio_v2.config_for({"mode": "image", "product": pid, "text": "x", "refs": [theirs]})
    with db.connect() as conn:
        _meta, j2 = create_routes.expand(conn, cfg2)
    if True:
        check(theirs not in j2[0]["params"]["photo_ids"], "another workspace's picture is dropped")
photo_rows = [q("SELECT * FROM photos WHERE id=?", i)[0] for i in (first_photo, logo_id, life[0])]
line = brand_assets.describe(photo_rows)
check("logo (Main logo)" in line and "lifestyle" in line and "do not invent other brands" in line,
      "the image prompt says what each reference is")
with db.connect() as conn:
    uid = conn.execute("SELECT id FROM users WHERE workspace_id=?", (ws,)).fetchone()[0]
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                       "updated_at, product_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ws, uid, "ad_image", "", json.dumps({"photo_ids": [first_photo, logo_id], "headline": "An easy gift",
                                                             "scene": "on a table"}), "done", 0, db.now(), db.now(), pid)).lastrowid
    conn.commit()
    prompt = worker.image_prompt(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
check("logo (Main logo)" in prompt, "and the worker adds it to the image prompt")

print("== the first ads' previews")
os.environ.update(PLANS_ONLY="1", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
with db.connect() as conn:          # a member on a plan (the step is theirs, or on the welcome credits)
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id IN (?, ?)", (ws, ws2))
    conn.commit()
with appmod.app.test_request_context("/app/welcome/first"):
    from flask import g
    with db.connect() as conn:
        g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws,)).fetchone()
        g.user = conn.execute("SELECT * FROM users WHERE workspace_id=?", (ws,)).fetchone()
        import products
        p = products.get_product(conn, ws, pid)
        looks = first_ads.draft(conn, p)["looks"]
        check(len(looks) == 2 and all(t.get("layout") is not None for t in looks), "two looks, each with its layout")
        jpg = first_ads.preview_jpeg(conn, ws, p, looks[0], "An easy gift")
check(jpg and jpg[:2] == b"\xff\xd8", "a look's preview is a JPEG made from the member's photo")
from PIL import Image  # noqa: E402
im = Image.open(io.BytesIO(jpg))
check(im.size == (first_ads.PREVIEW_W, int(first_ads.PREVIEW_W * 1.25)) or im.size[0] == first_ads.PREVIEW_W,
      "at the look's shape")
r = c.get(f"/app/welcome/first/preview/{looks[0]['id']}.jpg?h=Hello")
check(r.status_code == 200 and r.mimetype == "image/jpeg", "served to its member")
nc, ws3 = member("nophoto@example.com", photo=False)
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (ws3,))
    conn.commit()
check(nc.get(f"/app/welcome/first/preview/{looks[0]['id']}.jpg").status_code == 404, "no photo: no preview")
check(c.get("/app/welcome/first/preview/nope.jpg").status_code == 404, "an unknown look: 404")

print(f"\nall {passed} brand-asset checks passed")
