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
check(r.status_code == 200 and "The ad studio for" in html and "Start free" in html, "logged out: / is the landing page")
for section in ('id="gallery"', 'id="how"', 'id="reps"', 'id="pricing"', 'id="faq"', "© 2026 Campaignify",
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

print("== plans + subscriptions")
import plans  # noqa: E402
check([p["credits"] for p in plans.PAID] == [2000, 6000, 18000] and [p["usd"] for p in plans.PAID] == [39, 99, 249],
      "three plans: $39/2,000, $99/6,000, $249/18,000 credits")
check(all(p["price_id"].startswith("price_") for p in plans.PAID), "plans carry Stripe price ids")
check(plans.plan_code(nws) == "free" and plans.has(nws, "studio") and not plans.has(nws, "copy"), "new accounts are free: studio only")
check(n.get("/app/team").status_code == 200, "team page renders on free")
subs = []


def fake_sub_session(**kw):
    subs.append(kw)
    return {"id": f"cs_sub_{len(subs)}", "url": f"https://checkout.stripe.test/sub/{len(subs)}"}


billing.create_subscription_session = fake_sub_session
portal_calls = []
billing.create_portal_session = lambda **kw: portal_calls.append(kw) or "https://billing.stripe.test/portal"
html = n.get("/app/billing").get_data(as_text=True)
check("Get Growth" in html and "$99" in html and "Credit packs" in html, "billing page offers the plans and the packs")
check(n.post("/app/billing/subscribe", data={"csrf": csrf(n), "plan": "gold"}).status_code == 400, "unknown plan refused")
r = n.post("/app/billing/subscribe", data={"csrf": csrf(n), "plan": "growth"})
kw = subs[-1]
check(r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.test/sub/"), "plan checkout redirects to Stripe")
check(kw["price_id"] == plans.PLANS["growth"]["price_id"] and kw["metadata"]["app"] == "campaignify"
      and kw["metadata"]["kind"] == "plan" and kw["metadata"]["workspace_id"] == nws["id"]
      and "account_id" not in kw["metadata"] and kw["customer"] is None and kw["email"] == "rep@example.com",
      "subscription session: our plan price, our metadata, the member's email")


def plan_invoice(inv_id, ws_id, plan_code, reason="subscription_create", sub="sub_1", new_shape=False, meta=None,
                 amount=None, lines=None):
    p = plans.PLANS[plan_code]
    meta = meta if meta is not None else {"app": "campaignify", "kind": "plan", "workspace_id": str(ws_id), "plan": plan_code}
    lines = lines or [(p["price_id"], p["usd"] * 100)]
    if new_shape:
        data = [{"amount": a, "pricing": {"price_details": {"price": pr}}, "period": {"end": 1893456000}} for pr, a in lines]
        inv = {"parent": {"subscription_details": {"subscription": sub, "metadata": meta}}}
    else:
        data = [{"amount": a, "price": {"id": pr}, "period": {"end": 1893456000}} for pr, a in lines]
        inv = {"subscription": sub, "subscription_details": {"metadata": meta}}
    inv.update({"id": inv_id, "object": "invoice", "status": "paid", "currency": "usd", "customer": "cus_1",
                "billing_reason": reason, "amount_paid": amount if amount is not None else p["usd"] * 100,
                "lines": {"data": data}})
    return {"id": "evt_" + inv_id, "type": "invoice.paid", "data": {"object": inv}}


def wsrow(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


before = wsrow(nws["id"])
r = deliver({"id": "evt_cs", "type": "checkout.session.completed", "data": {"object": {
    "id": "cs_sub_1", "object": "checkout.session", "mode": "subscription", "customer": "cus_1", "subscription": "sub_1",
    "metadata": {"app": "campaignify", "kind": "plan", "workspace_id": str(nws["id"]), "plan": "growth"}}}})
w = wsrow(nws["id"])
check(r.status_code == 200 and w["stripe_customer_id"] == "cus_1" and w["stripe_subscription_id"] == "sub_1",
      "plan checkout links the Stripe customer and subscription")
check(w["plan_credits"] == 0 and w["credits"] == before["credits"], "no credits until the invoice is paid")
r = deliver(plan_invoice("in_1", nws["id"], "growth"))
w = wsrow(nws["id"])
check(r.status_code == 200 and r.get_json()["credited"] == 6000 and w["plan_credits"] == 6000
      and w["credits"] == before["credits"], "first invoice grants 6,000 plan credits; pack credits untouched")
check(plans.plan_code(w) == "growth" and plans.has(w, "copy") and plans.has(w, "seo") and not plans.has(w, "community"),
      "Growth unlocks Starter + Growth features, not Scale")
r = deliver(plan_invoice("in_1", nws["id"], "growth"))
check(r.get_json().get("duplicate") and wsrow(nws["id"])["plan_credits"] == 6000, "invoice.paid is idempotent by invoice id")
check(deliver(plan_invoice("in_x", nws["id"], "growth", meta={"account_id": "7"}, sub="sub_other")).get_json().get("ignored"),
      "other apps' subscription invoices on the shared account are ignored")
check(deliver(plan_invoice("in_y", nws["id"], "growth", lines=[("price_someone_else", 2900)])).get_json().get("ignored"),
      "invoices for prices that aren't ours are ignored")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM plan_invoices WHERE invoice_id IN ('in_x','in_y')").fetchone()[0] == 0,
          "ignored invoices leave no trace")
# spending: plan credits first
n.post("/app/generate", data={"csrf": csrf(n), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"})
w = wsrow(nws["id"])
check(w["plan_credits"] == 6000 - 22 and w["credits"] == before["credits"], "spending takes plan credits first")
with db.connect() as conn:
    pj = conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id DESC", (nws["id"],)).fetchone()
check(pj["priority"] == 0, "Growth jobs get no priority boost")
worker.tick()
results[pj["request_id"] if pj["request_id"] else f"req{len(calls['submit'])}"] = {"status": "failed"}
worker.tick()
w = wsrow(nws["id"])
check(w["plan_credits"] == 6000 and w["credits"] == before["credits"], "refund goes back to the plan bucket")
# a balance spread over both buckets
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_credits=10 WHERE id=?", (nws["id"],))
    conn.commit()
    ok = db.adjust_credits(conn, nws["id"], -30, "test spend")
    conn.commit()
w = wsrow(nws["id"])
check(ok and w["plan_credits"] == 0 and w["credits"] == before["credits"] - 20, "spend drains plan credits, then pack credits")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_credits=6000 WHERE id=?", (nws["id"],))
    conn.execute("UPDATE workspaces SET credits=? WHERE id=?", (before["credits"], nws["id"]))
    conn.commit()
# renewal: unused plan credits expire, the new month lands
with db.connect() as conn:
    db.adjust_credits(conn, nws["id"], -1000, "test spend")
    conn.commit()
r = deliver(plan_invoice("in_2", nws["id"], "growth", reason="subscription_cycle", new_shape=True))
w = wsrow(nws["id"])
with db.connect() as conn:
    exp = conn.execute("SELECT * FROM ledger WHERE workspace_id=? AND reason='unused plan credits expired' ORDER BY id DESC",
                       (nws["id"],)).fetchone()
check(r.status_code == 200 and w["plan_credits"] == 6000 and exp and exp["delta"] == -5000,
      "renewal: 5,000 unused plan credits expire, a fresh 6,000 lands (newer invoice shape read too)")
check(w["credits"] == before["credits"], "pack credits never expire")
# upgrade mid-month: the difference now
deliver({"id": "evt_up", "type": "customer.subscription.updated", "data": {"object": {
    "id": "sub_1", "object": "subscription", "status": "active", "customer": "cus_1", "cancel_at_period_end": False,
    "metadata": {"app": "campaignify", "workspace_id": str(nws["id"]), "plan": "growth"},
    "items": {"data": [{"price": {"id": plans.PLANS["scale"]["price_id"]}, "current_period_end": 1893456000}]}}}})
r = deliver(plan_invoice("in_3", nws["id"], "scale", reason="subscription_update", amount=15000,
                         lines=[(plans.PLANS["growth"]["price_id"], -9900), (plans.PLANS["scale"]["price_id"], 24900)]))
w = wsrow(nws["id"])
check(r.get_json()["credited"] == 12000 and w["plan_credits"] == 18000 and plans.plan_code(w) == "scale",
      "upgrade to Scale tops plan credits up by the difference (12,000)")
check(plans.has(w, "community") and plans.priority(w) == 1 and w["plan_period_end"].startswith("2030"),
      "Scale unlocks everything, priority queue, period end stored")
r = n.post("/app/billing/subscribe", data={"csrf": csrf(n), "plan": "starter"})
check(r.status_code == 303 and r.headers["Location"] == "https://billing.stripe.test/portal" and len(subs) == 1,
      "already subscribed: plan changes go to the portal, never a second subscription")
r = n.post("/app/billing/portal", data={"csrf": csrf(n)})
check(r.status_code == 303 and portal_calls[-1]["customer"] == "cus_1" and portal_calls[-1]["return_url"].endswith("/app/billing"),
      "Manage plan opens the Stripe customer portal for our customer")
check(b"Manage or cancel plan" in n.get("/app/billing").data, "billing page shows Manage plan when subscribed")
# cancel at period end, then the end
deliver({"id": "evt_c", "type": "customer.subscription.updated", "data": {"object": {
    "id": "sub_1", "object": "subscription", "status": "active", "customer": "cus_1", "cancel_at_period_end": True,
    "metadata": {"app": "campaignify", "workspace_id": str(nws["id"])},
    "items": {"data": [{"price": {"id": plans.PLANS["scale"]["price_id"]}, "current_period_end": 1893456000}]}}}})
w = wsrow(nws["id"])
check(w["plan_cancel_at_period_end"] == 1 and plans.plan_code(w) == "scale", "scheduled cancel keeps the plan until period end")
check(b"cancels 2030" in n.get("/app/billing").data, "billing page shows when the plan ends")
check(deliver({"id": "evt_o", "type": "customer.subscription.deleted", "data": {"object": {
    "id": "sub_crowdcut", "object": "subscription", "status": "canceled", "metadata": {"account_id": "3"}}}}).get_json().get("ignored"),
    "other apps' cancellations are ignored")
r = deliver({"id": "evt_d", "type": "customer.subscription.deleted", "data": {"object": {
    "id": "sub_1", "object": "subscription", "status": "canceled", "customer": "cus_1",
    "metadata": {"app": "campaignify", "workspace_id": str(nws["id"])}}}})
w = wsrow(nws["id"])
check(r.status_code == 200 and plans.plan_code(w) == "free" and w["plan_credits"] == 0 and w["credits"] == before["credits"],
      "subscription ended: back to free, plan credits gone, pack credits kept")
check(n.get("/app/billing").status_code == 200 and b"Get Growth" in n.get("/app/billing").data,
      "after cancel the plans can be bought again")

print("== brands + seats")
r = n.post("/app/brands", data={"csrf": csrf(n), "brand_name": "Second Co"})
with db.connect() as conn:
    nb = conn.execute("SELECT COUNT(*) FROM brands WHERE workspace_id=?", (nws["id"],)).fetchone()[0]
check(nb == 1, "free plan: one brand, a second is refused server-side")
deliver(plan_invoice("in_g", nws["id"], "growth", sub="sub_2"))
check(plans.plan_code(wsrow(nws["id"])) == "growth", "resubscribed to Growth")
for name in ("Second Co", "Third Co", "Fourth Co"):
    n.post("/app/brands", data={"csrf": csrf(n), "brand_name": name})
with db.connect() as conn:
    bs = conn.execute("SELECT * FROM brands WHERE workspace_id=? ORDER BY id", (nws["id"],)).fetchall()
w = wsrow(nws["id"])
check(len(bs) == 3 and w["brand_id"] == bs[-1]["id"] and w["brand_name"] == "Third Co", "Growth: three brands, the new one is active")
n.post("/app/brand", data={"csrf": csrf(n), "brand_name": "Third Co", "product_name": "ThirdThing", "claims": "", "audience": ""})
n.post(f"/app/brands/{bs[0]['id']}/use", data={"csrf": csrf(n)})
w = wsrow(nws["id"])
check(w["product_name"] == "GlowSerum" and w["brand_id"] == bs[0]["id"], "switching brands swaps the brand kit")
with db.connect() as conn:
    check(conn.execute("SELECT product_name FROM brands WHERE id=?", (bs[2]["id"],)).fetchone()[0] == "ThirdThing",
          "each brand keeps its own kit")
check(n.post(f"/app/brands/{bs[0]['id']}/use", data={"csrf": csrf(a)}).status_code == 400, "brand switch needs CSRF")
check(a.post(f"/app/brands/{bs[1]['id']}/use", data={"csrf": csrf(a)}).status_code == 404, "can't switch to another workspace's brand")
codes = []
for i in range(3):
    r = n.post("/app/team/invite", data={"csrf": csrf(n), "note": f"t{i}"})
with db.connect() as conn:
    tinv = conn.execute("SELECT * FROM invites WHERE workspace_id=? AND created_by IS NOT NULL", (nws["id"],)).fetchall()
check(len(tinv) == 2, "Growth: 3 seats = owner + 2 invites; the third invite is refused")
m1 = client()
r = m1.post("/signup", data={"csrf": csrf(m1), "email": "mate1@example.com", "password": "longpassword", "invite": tinv[0]["code"]})
with db.connect() as conn:
    mu = conn.execute("SELECT * FROM users WHERE email='mate1@example.com'").fetchone()
check(r.status_code == 302 and mu["workspace_id"] == nws["id"] and mu["role"] == "member", "teammate joins as a member")
check(m1.post("/app/team/invite", data={"csrf": csrf(m1)}).status_code == 403, "members can't create invites")
check(m1.post("/app/billing/subscribe", data={"csrf": csrf(m1), "plan": "scale"}).status_code == 403, "members can't change the plan")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override=NULL, plan='starter' WHERE id=?", (nws["id"],))
    conn.commit()
m2 = client()
r = m2.post("/signup", data={"csrf": csrf(m2), "email": "mate2@example.com", "password": "longpassword", "invite": tinv[1]["code"]})
check("team is full" in r.get_data(as_text=True), "join refused when the plan's seats are full (downgraded to Starter)")
r = n.post(f"/app/team/{mu['id']}/remove", data={"csrf": csrf(n)})
with db.connect() as conn:
    mu = conn.execute("SELECT * FROM users WHERE email='mate1@example.com'").fetchone()
check(mu["workspace_id"] != nws["id"] and mu["role"] == "owner", "owner removes a member; they keep an empty workspace of their own")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan='growth' WHERE id=?", (nws["id"],))
    conn.commit()

print("== gating + comp plans")
x = client()
x.post("/signup", data={"csrf": csrf(x), "email": "freebie@example.com", "password": "longpassword"})
with db.connect() as conn:
    xws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email='freebie@example.com'").fetchone()
check(xws["plan"] == "free" and not plans.has(xws, "planner"), "free account can't use plan features")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "scale"})
check(plans.plan_code(wsrow(xws["id"])) == "scale", "admin can comp a workspace onto a plan")
check(b.post("/admin/plan", data={"csrf": csrf(b), "workspace_id": xws["id"], "plan": "scale"}).status_code == 403,
      "only admins comp plans")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": ""})
check(plans.plan_code(wsrow(xws["id"])) == "free", "comp cleared")
html = client().get("/").get_data(as_text=True)
for bit in ("$39", "$99", "$249", "2,000 credits every month", "6,000 credits every month", "18,000 credits every month",
            "Credit packs", "expire at each renewal"):
    check(bit in html, f"landing pricing shows {bit!r}")
fh = client().get("/features")
check(fh.status_code == 200 and b"UGC ad studio" in fh.data and b"Compliance checks" in fh.data, "features page renders")
for k, f in plans.FEATURES.items():
    if not f["shipped"]:
        check(f["name"].encode() not in fh.data, f"features page doesn't list unshipped {f['name']!r}")

print("== copy studio + optimizer")
import copywriter  # noqa: E402
import llm  # noqa: E402
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)
check(x.get("/app/copy").status_code == 402 and b"Included in Starter" in x.get("/app/copy").data,
      "free account: copy studio shows the upgrade page")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
r = x.get("/app/copy")
check(r.status_code == 200 and b"AI writing isn't switched on" in r.data, "no AI key: the studio says so")
bal0 = db.balance(wsrow(xws["id"]))
r = x.post("/app/copy", data={"csrf": csrf(x), "kind": "ad", "platform": "meta", "offer": "Glow serum", "n": "3"})
check(r.status_code == 302 and db.balance(wsrow(xws["id"])) == bal0, "no AI key: nothing runs, nothing charged")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
llm_calls = []


def fake_anthropic(body):
    llm_calls.append(body)
    tool = (body.get("tool_choice") or {}).get("name")
    if tool == "submit_copy":
        out = {"variants": [
            {"angle": "Morning moment", "fields": [{"name": "Primary text", "text": "Your 7am skin, sorted. One drop of Glow serum and you're out the door."},
                                                   {"name": "Headline", "text": "Glow serum, 30 ml"}, {"name": "Description", "text": "Shop now"}]},
            {"angle": "Too long", "fields": [{"name": "Primary text", "text": "x" * 140}, {"name": "Headline", "text": "y" * 45},
                                             {"name": "Description", "text": "Shop now"}]},
            {"angle": "Bad", "fields": [{"name": "Primary text", "text": "Make $500 a week from home with this serum"},
                                        {"name": "Headline", "text": "Be your own boss"}, {"name": "Description", "text": "Join"}]}]}
    elif tool == "submit_review":
        out = {"scores": {"hook": 6, "clarity": 7, "cta": 4, "fit": 8}, "summary": "Clear, but no next step.",
               "issues": ["No call to action"], "rewrites": [{"angle": "Direct", "text": "Glow serum: one drop, done. Shop now."}]}
    else:
        out = {}
    return {"model": body["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 2000, "output_tokens": 800},
            "content": [{"type": "tool_use", "id": "tu_1", "name": tool, "input": out}]}


llm._anthropic = fake_anthropic
check(llm.credits_for(llm.cost_usd("claude-sonnet-4-6", 2000, 800)) == 4, "AI pricing: 2,000 in + 800 out on Sonnet = $0.018 x2 = 4 credits")
check(llm.credits_for(0.0001) == 1, "AI calls cost at least 1 credit")
r = x.post("/app/copy", data={"csrf": csrf(x), "kind": "ad", "platform": "meta", "offer": "Glow serum", "goal": "sales",
                              "tone": "Friendly", "audience": "Women 30-50", "n": "3"})
check(r.status_code == 302 and "/app/copy/" in r.headers["Location"], "copy run saved, opens the result")
body = llm_calls[-1]
check(body["tool_choice"]["name"] == "submit_copy" and "Primary text" in body["messages"][0]["content"]
      and "No income" in body["system"], "prompt asks for the platform's fields and carries the compliance rules")
check(db.balance(wsrow(xws["id"])) == bal0 - 4, "charged what the call actually cost (4 credits)")
html = x.get(r.headers["Location"]).get_data(as_text=True)
check("Morning moment" in html and "Checks passed" in html and "Blocked: No income or earnings claims." in html,
      "result page shows variants; the income-claim variant is flagged")
check("140/" in html or ">140" in html or "140 · aim 125" in html, "result page shows character counts against the platform")
with db.connect() as conn:
    run_row = conn.execute("SELECT * FROM copy_runs WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
check(run_row["credits"] == 4 and run_row["kind"] == "ad", "the run records its credits")
check(n.get(f"/app/copy/{run_row['id']}").status_code == 404, "other workspaces can't open your copy")
r = x.post("/app/copy", data={"csrf": csrf(x), "kind": "ad", "platform": "meta", "offer": "Join my team today", "n": "3"})
check(db.balance(wsrow(xws["id"])) == bal0 - 4 and len([c for c in llm_calls if c["tool_choice"]["name"] == "submit_copy"]) == 1,
      "recruiting in the brief is blocked before any AI call")
check(x.get("/app/optimize").status_code == 402, "optimizer is Growth+: Starter sees the upgrade page")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})
bal1 = db.balance(wsrow(xws["id"]))
r = x.post("/app/optimize", data={"csrf": csrf(x), "text": "MAKE $500 A WEEK WITH THIS SERUM #a #b #c #d #e #f #g",
                                  "platform": "meta", "goal": "sales"})
html = x.get(r.headers["Location"]).get_data(as_text=True)
check(db.balance(wsrow(xws["id"])) == bal1 and "No income or earnings claims." in html and "Quick checks" in html,
      "quick checks are free and catch compliance problems")
r = x.post("/app/optimize", data={"csrf": csrf(x), "text": "Glow serum is a nice serum for your skin and it is here.",
                                  "platform": "meta", "goal": "sales", "ai": "1"})
html = x.get(r.headers["Location"]).get_data(as_text=True)
check(db.balance(wsrow(xws["id"])) == bal1 - 4 and "Clear, but no next step." in html and "one drop, done" in html,
      "AI review: scores, fixes and rewrites, charged at cost")
q = copywriter.quick_checks("Why does your 2pm coffee taste like regret? Try GlowJava. Shop now.", "meta")
check(q["scores"]["hook"] >= 8 and q["scores"]["cta"] >= 8 and q["compliance"]["ok"], "quick checks reward a hook and a CTA")


def broken(body):
    raise llm.LLMError("503: overloaded")


llm._anthropic = broken
bal2 = db.balance(wsrow(xws["id"]))
r = x.post("/app/copy", data={"csrf": csrf(x), "kind": "hooks", "platform": "tiktok", "offer": "Glow serum", "n": "3"})
check(r.status_code == 302 and db.balance(wsrow(xws["id"])) == bal2, "writer error: no charge, friendly message")
llm._anthropic = fake_anthropic
check(b"AI writing (planner, copy, SEO): <b>on" in a.get("/admin").data, "admin shows the AI writing status")

print("== pages")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": ws1["id"], "plan": "scale"})
for path in ("/app", "/app/library", "/app/brand", "/admin", "/app/new?tool=motion_copy", "/app/new?template=cost",
             "/app/start", "/app/billing", "/app/new?template=hero", "/app/team", "/app/copy", "/app/optimize",
             "/app/copy?kind=landing&platform=landing"):
    check(a.get(path).status_code == 200, f"{path} renders")
check(a.get("/health").get_json()["generation_configured"], "health reports configured")
for path in ("/app", "/app/start", "/app/billing", "/", "/features"):
    html = (a if path != "/" else client()).get(path).get_data(as_text=True)
    check(not re.search(r"higgsfield", html, re.I), f"vendor never named in the UI ({path})")

print(f"\nall {passed} checks passed ({TMP})")
