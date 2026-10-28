"""End-to-end checks on a throwaway database with the generation API faked.

    python tests/test_app.py
"""
import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_test_")
import hashlib  # noqa: E402
OWNER_CODE, OWNER2_CODE = "owner-code-koby", "owner-code-travis"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_MARKUP="2.0",
                  RS_OWNER_INVITES=f"koby@example.com:{hashlib.sha256(OWNER_CODE.encode()).hexdigest()},"
                                   f"travis@example.com:{hashlib.sha256(OWNER2_CODE.encode()).hexdigest()}")

import app as appmod  # noqa: E402
import billing  # noqa: E402
import catalog  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client(ip=None):
    """A browser with its own IP (signups are rate-limited per IP)."""
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip or f"10.0.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


# ---- fake generation API
calls = {"submit": [], "upload": 0}
results = {}


def fake_submit(model, inputs, webhook_url=None):
    rid = f"req{len(calls['submit']) + 1}"
    calls["submit"].append((model, inputs))
    results[rid] = {"status": "in_progress"}
    return rid


def fake_status(rid):
    return results[rid]


def fake_upload(data, ctype):
    calls["upload"] += 1
    return f"https://files.example/{calls['upload']}.png"


class FakeResp:
    status_code = 200
    content = b"\x89PNG fake"

    def raise_for_status(self):
        pass


hf.submit, hf.status, hf.upload = fake_submit, fake_status, fake_upload
worker.requests.get = lambda url, timeout=0: FakeResp()

print("== compliance")
check(not compliance.check("Lose weight fast with this coffee")["ok"], "weight-loss blocked")
check(not compliance.check("Get 20% off today")["ok"], "discount blocked")
check(not compliance.check("helps cure anxiety")["ok"], "disease claim blocked")
check(compliance.check("One scoop, hot water, 30 seconds")["ok"], "label language passes")
check(compliance.check("Clinically proven focus")["warnings"], "promise words warn")
for claim in ("Make $500 a week from home", "I made $3,000 last month", "earn extra cash", "financial freedom",
              "Quit your 9-to-5", "six figures this year", "$400 a week in extra income"):
    check(not compliance.check(claim)["ok"], f"income claim blocked: {claim!r}")
for claim in ("Join my team!", "Become an ISR today", "DM me about this business opportunity"):
    check(not compliance.check(claim)["ok"], f"recruiting blocked: {claim!r}")
for ok in ("Makes 30 servings", "$59 a month on Subscribe & Save", "for your work-from-home mornings"):
    check(compliance.check(ok)["ok"], f"product language passes: {ok!r}")
check(all(compliance.check(catalog.fill(t["brief"], {"product": "CoreJava", "name": "TMU"}), t.get("audience", ""))["ok"]
          for t in catalog.TEMPLATES), "every template brief passes the checks")

print("== pricing")
check(catalog.price_credits("ad_image", batch=1) == 22, "image ad: $0.1071 x2 -> 22 credits")
check(catalog.price_credits("photo_to_video", duration=5) == 206, "5s video: $1.03 x2 -> 206 credits")
check(catalog.price_credits("motion_copy", ref_seconds=8.2) == 180, "motion copy bills whole seconds")
STARTER = catalog.starter_credits()
check(STARTER == 2 * catalog.price_credits("ad_image", batch=1) == 44, "starter credits = 2 image ads (44)")
check([p["credits"] for p in catalog.PACKS] == [2900, 8500, 23000] and all(p["price_id"].startswith("price_")
      for p in catalog.PACKS), "three packs with Stripe price ids")
check(all(p["credits"] / (p["usd"] * 100) <= 1.2 for p in catalog.PACKS), "pack bonuses stay inside the markup")

print("== accounts")
x = client()
r = x.get("/")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "The ad studio we use for" in html and "Start free" in html, "logged out: / is the landing page")
for section in ('id="gallery"', 'id="how"', 'id="reps"', 'id="pricing"', 'id="faq"', "A <a href=\"https://rectorandstull.com\">Rector &amp; Stull</a> company",
                "/privacy", "/terms", "$29", "$79", "$199", "Checkout coming soon"):
    check(section in html, f"landing has {section}")
check(not re.search(r"higgsfield", html, re.I), "landing never names the vendor")
check(not re.search(r"\$\s?\d[\d,]*\s*(a|per)\s*(week|month)|financial freedom|passive income|six figures", html, re.I),
      "landing makes no income or earnings claims")
check(html.count("previews/") >= 8 and "gallery/tmu-" in html, "gallery shows our real ads")
check(x.get("/privacy").status_code == 200 and x.get("/terms").status_code == 200, "privacy and terms pages")
r = x.post("/signup", data={"csrf": csrf(x), "email": "bot@example.com", "password": "longpassword", "website": "spam.example"})
with db.connect() as conn:
    check(r.status_code == 400 and not conn.execute("SELECT 1 FROM users WHERE email='bot@example.com'").fetchone(),
          "honeypot stops bots")
r = x.post("/signup", data={"csrf": csrf(x), "email": "stranger@example.com", "password": "longpassword", "name": "Sam"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/start"), "anyone can sign up; lands on the first-ad flow")
with db.connect() as conn:
    su = conn.execute("SELECT * FROM users WHERE email='stranger@example.com'").fetchone()
    sws = conn.execute("SELECT * FROM workspaces WHERE id=?", (su["workspace_id"],)).fetchone()
    sled = conn.execute("SELECT * FROM ledger WHERE workspace_id=?", (sws["id"],)).fetchall()
check(not su["is_admin"], "self-serve accounts are not admins")
check(sws["credits"] == STARTER and len(sled) == 1 and sled[0]["reason"] == "starter credits",
      "new workspace gets the starter credits")
r = x.get("/")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "logged in: / goes to the studio")
check(x.get("/app").status_code == 200 and b"Make your first ad" in x.get("/app").data, "studio points new accounts at the first ad")
r = x.post("/signup", data={"csrf": csrf(x), "email": "stranger@example.com", "password": "longpassword"})
check(r.status_code == 302, "signup while logged in just goes to the studio")
rl = client(ip="10.9.9.9")
codes = []
for i in range(appmod.SIGNUPS_PER_HOUR + 1):
    rl.post("/logout", data={"csrf": csrf(rl)})
    rl.get("/login")
    codes.append(rl.post("/signup", data={"csrf": csrf(rl), "email": f"burst{i}@example.com",
                                          "password": "longpassword"}).status_code)
check(codes[:-1] == [302] * appmod.SIGNUPS_PER_HOUR and codes[-1] == 429, "signups are rate-limited per IP")
lc = client(ip="10.9.9.10")
for i in range(appmod.LOGIN_FAILS_PER_EMAIL):
    lc.post("/login", data={"csrf": csrf(lc), "email": "stranger@example.com", "password": "wrong"})
r = lc.post("/login", data={"csrf": csrf(lc), "email": "stranger@example.com", "password": "longpassword"})
check(r.status_code == 429, "repeated failed logins are throttled")
x = client()
check(b"koby@example.com" in x.get(f"/signup?invite={OWNER_CODE}").data, "owner link pre-fills its email")
r = x.post("/signup", data={"csrf": csrf(x), "email": "travis@example.com", "password": "longpassword", "invite": OWNER_CODE})
check("different email" in r.get_data(as_text=True), "owner code only works for its own email")
a = client()
r = a.post("/signup", data={"csrf": csrf(a), "email": "koby@example.com", "password": "longpassword", "name": "Koby",
                            "invite": OWNER_CODE})
check(r.status_code == 302 and "/app/brand" in r.headers["Location"], "owner link makes an admin, lands on brand kit")
r = x.post("/signup", data={"csrf": csrf(x), "email": "koby@example.com", "password": "otherpassword", "invite": OWNER_CODE})
check("already has an account" in r.get_data(as_text=True), "owner link can't be used twice")
t2 = client()
r = t2.post("/signup", data={"csrf": csrf(t2), "email": "travis@example.com", "password": "longpassword", "invite": OWNER2_CODE})
with db.connect() as conn:
    us = conn.execute("SELECT * FROM users WHERE email IN ('koby@example.com','travis@example.com')").fetchall()
check(len(us) == 2 and all(u["is_admin"] for u in us) and len({u["workspace_id"] for u in us}) == 1,
      "both owners are admins of one Rector & Stull workspace")
r = a.post("/app/brand", data={"brand_name": "TMU", "product_name": "CoreJava", "claims": "30 servings"})
check(r.status_code == 400, "POST without CSRF refused")
a.post("/app/brand", data={"csrf": csrf(a), "brand_name": "TMU", "product_name": "CoreJava",
                           "claims": "30 servings", "audience": "coffee lovers"})
r = a.post("/app/photos", data={"csrf": csrf(a), "photos": (png(), "bag.png")}, content_type="multipart/form-data")
check(r.status_code == 302, "photo upload")
with db.connect() as conn:
    photo = conn.execute("SELECT * FROM photos").fetchone()
    ws1 = conn.execute("SELECT * FROM workspaces WHERE name='Rector & Stull'").fetchone()
check(photo and ws1["product_name"] == "CoreJava", "brand kit and photo stored")

r = a.get("/app/new?template=swap")
check(b"CoreJava" in r.data and b"206" in r.data, "template fills product name and shows price")

r = a.post("/app/generate", data={"csrf": csrf(a), "tool": "photo_to_video", "template": "swap",
                                  "brief": "pour and stir", "photo": str(photo["id"]), "duration": "5"})
check("credits" in a.get("/app/library").get_data(as_text=True), "library renders")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0, "no credits: nothing queued")

r = a.post("/admin/credits", data={"csrf": csrf(a), "workspace_id": ws1["id"], "amount": "1000"})
with db.connect() as conn:
    check(conn.execute("SELECT credits FROM workspaces WHERE id=?", (ws1["id"],)).fetchone()[0] == 1000, "admin top-up")

r = a.post("/app/generate", data={"csrf": csrf(a), "tool": "ad_image", "brief": "Lose 10 pounds with every cup",
                                  "photo": str(photo["id"])})
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0, "non-compliant brief blocked before charging")

print("== generation")
r = a.post("/app/generate", data={"csrf": csrf(a), "tool": "photo_to_video", "template": "swap",
                                  "brief": "pour and stir", "photo": str(photo["id"]), "duration": "5"})
check(r.status_code == 302 and "/app/library" in r.headers["Location"], "video job created")
with db.connect() as conn:
    job = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    check(job["status"] == "pending" and job["credits"] == 206, "job pending, 206 credits held")
    check(conn.execute("SELECT credits FROM workspaces WHERE id=?", (ws1["id"],)).fetchone()[0] == 794, "credits deducted")
worker.tick()
model, inputs = calls["submit"][-1]
check(model == "bytedance/seedance-2.5/image-to-video" and inputs["image_url"].startswith("https://files.example/")
      and inputs["duration"] == 5, "submitted to the image-to-video model with the uploaded photo")
results["req1"] = {"status": "completed", "video": {"url": "https://cdn.example/out.mp4"}}
worker.tick()
with db.connect() as conn:
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    out = conn.execute("SELECT * FROM outputs WHERE job_id=?", (job["id"],)).fetchone()
check(job["status"] == "done" and out and out["kind"] == "video" and os.path.exists(out["path"]), "result saved to disk")
r = a.get(f"/media/{out['id']}?download=1")
check(r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", ""), "download works")

a.post("/app/generate", data={"csrf": csrf(a), "tool": "ad_image", "brief": "Iced coffee on a patio",
                              "photo": str(photo["id"]), "batch": "2", "aspect": "4:5"})
worker.tick()
model, inputs = calls["submit"][-1]
check(model == "higgsfield/ads-studio/v1.0" and inputs["batch_size"] == 2 and inputs["aspect_ratio"] == "4:5",
      "image ad inputs")
check(calls["upload"] == 1, "photo uploaded once and reused")
results["req2"] = {"status": "nsfw"}
worker.tick()
with db.connect() as conn:
    j2 = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
    bal = conn.execute("SELECT credits FROM workspaces WHERE id=?", (ws1["id"],)).fetchone()[0]
check(j2["status"] == "failed" and "filter" in j2["error"], "filtered result marked failed")
check(bal == 794, "credits refunded on failure")

print("== invites + isolation")
r = a.post("/admin/invites", data={"csrf": csrf(a), "workspace_id": "", "note": "ISR Jane"})
with db.connect() as conn:
    code = conn.execute("SELECT code FROM invites").fetchone()["code"]
b = client()
r = b.post("/signup", data={"csrf": csrf(b), "email": "jane@example.com", "password": "longpassword2", "invite": "nope"})
check("expired or was already used" in r.get_data(as_text=True), "bad invite refused")
r = b.post("/signup", data={"csrf": csrf(b), "email": "jane@example.com", "password": "longpassword2", "invite": code})
check(r.status_code == 302, "invited ISR signs up into their own workspace")
check(b.get(f"/media/{out['id']}").status_code == 404, "other workspaces can't see your files")
check(b.get("/admin").status_code == 403, "members can't open admin")
b2 = client()
r = b2.post("/signup", data={"csrf": csrf(b2), "email": "x@example.com", "password": "longpassword3", "invite": code})
check("expired or was already used" in r.get_data(as_text=True), "invite works once")

print("== motion copy")
with db.connect() as conn:
    vid = os.path.join(TMP, "ref.mp4")
    open(vid, "wb").write(b"fake")
    conn.execute("INSERT INTO refs (title, path, content_type, seconds, created_at) VALUES (?,?,?,?,?)",
                 ("Kitchen pour", vid, "video/mp4", 6.0, db.now()))
    conn.commit()
    rid = conn.execute("SELECT id FROM refs").fetchone()["id"]
a.post("/app/generate", data={"csrf": csrf(a), "tool": "motion_copy", "brief": "same moves",
                              "photo": str(photo["id"]), "ref": str(rid)})
worker.tick()
model, inputs = calls["submit"][-1]
check(model == "higgsfield/genjutsu-light/motion-transfer/v1.0" and inputs["video_url"].startswith("https://files.example/")
      and inputs["image_urls"], "motion copy sends reference video and product photo")

print("== guided first ad")
n = client()
n.post("/signup", data={"csrf": csrf(n), "email": "rep@example.com", "password": "longpassword", "name": "Riley"})
r = n.get("/app/start")
check(r.status_code == 200 and b"Product hero" in r.data and b"Make my first ad" in r.data, "first-ad flow renders")
r = n.post("/app/start", data={"csrf": csrf(n), "product_name": "GlowSerum", "template": "hero",
                               "brief": "Make $500 a week with GlowSerum", "photo": (png(), "serum.png")},
           content_type="multipart/form-data")
with db.connect() as conn:
    nws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email='rep@example.com'").fetchone()
    njobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (nws["id"],)).fetchone()[0]
check(r.status_code == 400 and "income" in r.get_data(as_text=True) and njobs == 0 and nws["credits"] == STARTER,
      "income claim in the first ad is blocked, nothing charged")
r = n.post("/app/start", data={"csrf": csrf(n), "product_name": "GlowSerum", "brand_name": "Glow Co", "template": "hero",
                               "brief": "", "audience": "Women 30-50", "photo": (png(), "serum.png")},
           content_type="multipart/form-data")
check(r.status_code == 302 and "/app/library" in r.headers["Location"], "first ad queued from one form")
with db.connect() as conn:
    nws = conn.execute("SELECT * FROM workspaces WHERE id=?", (nws["id"],)).fetchone()
    nj = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (nws["id"],)).fetchone()
    nph = conn.execute("SELECT COUNT(*) FROM photos WHERE workspace_id=?", (nws["id"],)).fetchone()[0]
check(nws["product_name"] == "GlowSerum" and nws["brand_name"] == "Glow Co" and nws["audience"] == "Women 30-50" and nph == 1,
      "brand kit filled as a side effect (name, brand, audience, photo)")
check(nj["tool"] == "ad_image" and nj["template_id"] == "hero" and "GlowSerum" in nj["brief"] and nj["credits"] == 22,
      "first ad is a 22-credit image ad with the product in the brief")
check(nws["credits"] == STARTER - 22, "starter credits cover it (one more image ad left)")
worker.tick()
model, inputs = calls["submit"][-1]
check(model == "higgsfield/ads-studio/v1.0" and "GlowSerum" in inputs["prompt"] and inputs["aspect_ratio"] == "4:5",
      "first ad submitted with the template's shape")
check(b"Make your first ad" not in n.get("/app").data, "first-ad prompt goes away after the first ad")
r = n.post("/app/brand", data={"csrf": csrf(n), "brand_name": "Glow Co", "product_name": "GlowSerum",
                               "claims": "Be your own boss with GlowSerum", "audience": ""})
with db.connect() as conn:
    check(conn.execute("SELECT claims FROM workspaces WHERE id=?", (nws["id"],)).fetchone()[0] in (None, ""),
          "brand kit refuses recruiting claims")

print("== billing")
r = n.get("/app/billing")
check(r.status_code == 200 and b"Checkout coming soon" in r.data and b"$79" in r.data, "billing page: packs shown, checkout off until keys are set")
r = n.post("/app/billing/checkout", data={"csrf": csrf(n), "pack": "pro"})
with db.connect() as conn:
    check(r.status_code == 302 and conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 0,
          "no Stripe keys: buy button doesn't crash, nothing recorded")
WHSEC = "whsec_test_only"
os.environ.update(STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET=WHSEC)
sessions = []


def fake_session(**kw):
    sessions.append(kw)
    return {"id": f"cs_test_{len(sessions)}", "url": f"https://checkout.stripe.test/{len(sessions)}"}


billing.create_checkout_session = fake_session
check(b"Buy for $29" in n.get("/app/billing").data, "with keys set the buy buttons work")
check(n.post("/app/billing/checkout", data={"csrf": csrf(n), "pack": "nope"}).status_code == 400, "unknown pack refused")
r = n.post("/app/billing/checkout", data={"csrf": csrf(n), "pack": "starter"})
check(r.status_code == 303 and r.headers["Location"] == "https://checkout.stripe.test/1", "checkout redirects to Stripe")
kw = sessions[-1]
check(kw["price_id"] == catalog.PACK_BY_CODE["starter"]["price_id"] and kw["email"] == "rep@example.com"
      and kw["metadata"]["app"] == "campaignify" and kw["metadata"]["pack"] == "starter"
      and "account_id" not in kw["metadata"] and kw["success_url"].endswith("/app/billing?checkout=success"),
      "session: our price, email, metadata (never Crowdcut's account_id)")
with db.connect() as conn:
    pur = conn.execute("SELECT * FROM purchases ORDER BY id DESC").fetchone()
check(pur["status"] == "open" and pur["stripe_session_id"] == "cs_test_1" and pur["credits"] == 2900
      and str(pur["id"]) == str(kw["metadata"]["purchase_id"]), "purchase recorded as open")


def event(purchase, amount=2900, status="paid", meta=None, typ="checkout.session.completed"):
    return {"id": "evt_1", "type": typ, "data": {"object": {
        "id": purchase["stripe_session_id"], "object": "checkout.session", "amount_total": amount, "currency": "usd",
        "payment_status": status, "payment_intent": "pi_1",
        "metadata": meta if meta is not None else {"app": "campaignify", "purchase_id": str(purchase["id"]),
                                                   "workspace_id": str(purchase["workspace_id"]), "pack": purchase["pack"]}}}}


def deliver(ev, secret=WHSEC, sig=None):
    import json as _json
    body = _json.dumps(ev).encode()
    return client().post("/webhooks/stripe", data=body, content_type="application/json",
                         headers={"Stripe-Signature": sig or billing.sign(body, secret)})


def balance():
    with db.connect() as conn:
        return conn.execute("SELECT credits FROM workspaces WHERE id=?", (nws["id"],)).fetchone()[0]


before = balance()
check(deliver(event(pur), secret="whsec_wrong").status_code == 400, "bad signature rejected")
check(deliver(event(pur), sig="t=1,v1=deadbeef").status_code == 400, "stale or forged signature rejected")
check(balance() == before, "rejected webhooks credit nothing")
check(deliver(event(pur, meta={"account_id": "1"})).status_code == 200 and balance() == before,
      "other apps' checkouts on the shared Stripe account are ignored")
check(deliver(event(pur, amount=100)).status_code == 400 and balance() == before, "amount mismatch refused")
check(deliver(event(pur, status="unpaid")).status_code == 200 and balance() == before, "unpaid session not credited")
r = deliver(event(pur))
check(r.status_code == 200 and r.get_json()["credited"] == 2900 and balance() == before + 2900, "paid checkout adds the pack's credits")
r = deliver(event(pur))
check(r.status_code == 200 and r.get_json().get("duplicate") and balance() == before + 2900, "webhook is idempotent (retry adds nothing)")
with db.connect() as conn:
    pur = conn.execute("SELECT * FROM purchases WHERE id=?", (pur["id"],)).fetchone()
    led = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC", (nws["id"],)).fetchone()
check(pur["status"] == "paid" and pur["stripe_payment_intent"] == "pi_1" and led["reason"] == "Starter pack", "purchase marked paid, ledger says why")
html = n.get("/app/billing").get_data(as_text=True)
check("Starter" in html and "$29.00" in html and "Paid" in html, "billing page shows purchase history")


def failing_session(**kw):
    raise billing.BillingError("Stripe 500")


billing.create_checkout_session = failing_session
r = n.post("/app/billing/checkout", data={"csrf": csrf(n), "pack": "studio"})
with db.connect() as conn:
    st = conn.execute("SELECT status FROM purchases ORDER BY id DESC").fetchone()[0]
check(r.status_code == 302 and st == "failed", "Stripe error: no crash, purchase marked failed")
check(billing.verify_event(b'{"a":1}', billing.sign(b'{"a":1}', "s"), secret="s") == {"a": 1}, "signature helper round-trips")

print("== pages")
for path in ("/app", "/app/library", "/app/brand", "/admin", "/app/new?tool=motion_copy", "/app/new?template=cost",
             "/app/start", "/app/billing", "/app/new?template=hero"):
    check(a.get(path).status_code == 200, f"{path} renders")
check(a.get("/health").get_json()["generation_configured"], "health reports configured")
for path in ("/app", "/app/start", "/app/billing", "/"):
    html = (a if path != "/" else client()).get(path).get_data(as_text=True)
    check(not re.search(r"higgsfield", html, re.I), f"vendor never named in the UI ({path})")

print(f"\nall {passed} checks passed ({TMP})")
