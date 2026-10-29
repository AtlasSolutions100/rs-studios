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
import json  # noqa: E402
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
for section in ('id="journey"', 'id="gallery"', 'id="reps"', 'id="pricing"', 'id="faq"', "© 2026 Campaignify",
                "/privacy", "/terms", "$29", "$79", "$199", "Checkout coming soon"):
    check(section in html, f"landing has {section}")
check(not re.search(r"higgsfield", html, re.I), "landing never names the vendor")
check(not re.search(r"\$\s?\d[\d,]*\s*(a|per)\s*(week|month)|financial freedom|passive income|six figures", html, re.I),
      "landing makes no income or earnings claims")
check(html.count("previews/") >= 8 and "site/phones.mp4" in html, "gallery shows the example ads")
check(all(f'id="step-{s["id"]}"' in html for s in appmod.JOURNEY) and len(appmod.JOURNEY) == 8,
      "landing walks through the eight campaign steps")
check(not re.search(r"corejava|marketplace global|morning upgrade|tmu-|crowdcut", html, re.I),
      "landing shows no real brand or product names")
vids = re.findall(r"<video[^>]*>", html)
check(vids and all("muted" in v and "playsinline" in v and "poster=" in v for v in vids),
      "every landing video is muted, inline and has a poster")
check(sum('preload="none"' in v and "data-poster=" in v for v in vids) >= len(vids) - 3,
      "below-the-fold videos and their posters load lazily")
check(html.count('rel="preload"') == 1, "only the hero poster is preloaded")
check(not re.search(r"auto-?post|posts? your ads|publish(es)? your ads to", html, re.I), "landing never claims to post ads")
for path in sorted(set(re.findall(r'/static/((?:site|previews)/[^"?]+)', html))):
    check(os.path.exists(os.path.join(os.path.dirname(HERE), "static", path)), f"landing asset exists: {path}")
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
stripe_reads = []
STRIPE_GET = {}


def fake_get(path, params=None):
    stripe_reads.append((path, params))
    return STRIPE_GET.get(path, {"data": []})


billing._get = fake_get  # no network in tests; the plan sync reads through this
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

print("== campaign planner")
import planner  # noqa: E402
import pages_routes  # noqa: E402
import forms_routes  # noqa: E402
check(client().get("/app/campaigns").status_code == 302, "planner needs a login")
fz = client()
fz.post("/signup", data={"csrf": csrf(fz), "email": "free2@example.com", "password": "longpassword"})
check(fz.get("/app/campaigns").status_code == 402, "free account: planner shows the upgrade page")
plan_bodies = []


def tu(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}_{name}", "name": name, "input": inp}


def fake_agent(body):
    if not any(t.get("name") == "get_brand_and_catalog" for t in body.get("tools") or []):
        return fake_anthropic(body)
    plan_bodies.append(body)
    turn = sum(1 for m in body["messages"] if m["role"] == "assistant")
    script = [
        [{"type": "text", "text": "Reading the brand kit first."}, tu(0, "get_brand_and_catalog", {})],
        [tu(1, "save_angles", {"angles": [
            {"name": "Seven a.m. skin", "insight": "Mornings are rushed", "audience": "Busy women 30-45", "why": "Specific moment"},
            {"name": "Desk drawer", "insight": "Reapply at work", "audience": "Office workers"},
            {"name": "Gift", "insight": "Easy gift", "audience": "Partners"}]}),
         tu(2, "save_creative", {"angle": "Seven a.m. skin", "format": "UGC talking video",
                                 "hooks": ["Your 7am routine is too long.", "One drop, out the door."],
                                 "script": "Beat 1: alarm. Beat 2: one drop. Beat 3: out the door.",
                                 "on_screen_text": ["One drop", "Done"]}),
         tu(3, "save_creative", {"angle": "Desk drawer", "hooks": ["Make $500 a week sharing this"], "script": "x"})],
        [tu(4, "propose_ad", {"angle": "Seven a.m. skin", "platform": "meta", "tool": "ad_image", "template_id": "hero",
                              "brief": "Glow serum on a bathroom shelf at sunrise, label sharp", "versions": 2}),
         tu(5, "propose_ad", {"angle": "Seven a.m. skin", "platform": "tiktok", "tool": "photo_to_video", "duration": 5,
                              "brief": "Hand picks up the serum, one drop, morning light"}),
         tu(6, "propose_ad", {"angle": "Desk drawer", "platform": "meta", "tool": "ad_image",
                              "brief": "Quit your 9-to-5 with this serum"}),
         tu(7, "write_copy", {"platform": "meta", "kind": "ad", "angle": "Seven a.m. skin", "variants": 3})],
        [tu(8, "finish", {"summary": "Test three morning angles on Meta and TikTok.",
                          "budget_split": [{"platform": "meta", "share_percent": 60, "note": "Image ads"},
                                           {"platform": "tiktok", "share_percent": 40}],
                          "kpis": ["Click-through rate", "Cost per purchase"], "test_plan": "Run 7 days, keep the winner.",
                          "checklist": [{"title": "Install the Meta pixel", "detail": "Events Manager", "platform": "meta"},
                                        {"title": "Upload the ads", "platform": "tiktok"}]})],
    ]
    content = script[min(turn, len(script) - 1)]
    return {"model": body["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 2000, "output_tokens": 800},
            "content": content}


llm._anthropic = fake_agent
os.environ.pop("ANTHROPIC_API_KEY", None)
r = x.post("/app/campaigns", data={"csrf": csrf(x), "offer": "Glow serum", "platforms": ["meta", "tiktok"]})
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM campaigns WHERE workspace_id=?", (xws["id"],)).fetchone()[0] == 0,
          "no AI key: no campaign is planned, nothing charged")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
check(b"Plan my campaign" in x.get("/app/campaigns").data, "planner brief form renders")
r = x.post("/app/campaigns", data={"csrf": csrf(x), "offer": "Join my team and earn", "platforms": ["meta"]})
check(not plan_bodies, "recruiting brief is refused before planning")
bal3 = db.balance(wsrow(xws["id"]))
r = x.post("/app/campaigns", data={"csrf": csrf(x), "offer": "Glow serum", "audience": "Women 30-45", "goal": "sales",
                                   "budget": "$300 for two weeks", "platforms": ["meta", "tiktok", "nope"]})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/campaigns"), "not enough credits for the planning cap: refused")
a.post("/admin/credits", data={"csrf": csrf(a), "workspace_id": xws["id"], "amount": "3000"})
bal3 = db.balance(wsrow(xws["id"]))
r = x.post("/app/campaigns", data={"csrf": csrf(x), "offer": "Glow serum", "audience": "Women 30-45", "goal": "sales",
                                   "budget": "$300 for two weeks", "platforms": ["meta", "tiktok", "nope"]})
check(r.status_code == 302 and "/app/campaigns/" in r.headers["Location"], "planning runs and opens the campaign")
with db.connect() as conn:
    camp = conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
    cads = conn.execute("SELECT * FROM campaign_ads WHERE campaign_id=? ORDER BY id", (camp["id"],)).fetchall()
cplan = json.loads(camp["plan_json"])
check(camp["status"] == "ready" and json.loads(camp["brief_json"])["platforms"] == ["meta", "tiktok"], "plan ready; unknown platforms dropped")
check(len(cplan["angles"]) == 3 and len(cplan["creative"]) == 1, "angles saved; the income-claim script was rejected")
check(len(cads) == 2 and {a["tool"] for a in cads} == {"ad_image", "photo_to_video"} and all(a["status"] == "proposed" for a in cads),
      "two ads proposed (the non-compliant one refused), none generated")
check(cads[0]["credits"] == catalog.price_credits("ad_image", batch=2) and cads[0]["template_id"] == "hero",
      "proposal carries its credit price and template")
check(len(cplan["copy"]) == 1 and cplan["copy"][0]["platform"] == "meta" and cplan["final"]["checklist"],
      "copy written through the copy studio; launch checklist saved")
first = plan_bodies[0]
check(first["system"][0]["cache_control"]["type"] == "ephemeral" and first["tools"][0]["name"] == "get_brand_and_catalog"
      and "Glow serum" in first["messages"][0]["content"][0]["text"], "agent gets the brief, the tools and a cached system prompt")
check(any(t["name"] == "audit_page" for t in first["tools"]), "Growth workspaces' planner can audit the landing page")
res_msgs = [m for m in plan_bodies[2]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            and m["content"] and m["content"][0].get("type") == "tool_result"]
check(any(b.get("is_error") and "income" in b["content"] for m in res_msgs for b in m["content"]),
      "rule breaks go back to the agent as tool errors")
spent = bal3 - db.balance(wsrow(xws["id"]))
check(spent == 18 and camp["credits"] == 18, "planning charged its actual AI cost once (5 calls = 18 credits)")
html = x.get(f"/app/campaigns/{camp['id']}").get_data(as_text=True)
for bit in ("Seven a.m. skin", "Your 7am routine is too long.", "Ads to make", "Generate selected ads", "Launch checklist",
            "Install the Meta pixel", "Test three morning angles", "How it was planned", "60%"):
    check(bit in html, f"campaign page shows {bit!r}")
check(x.get(f"/app/campaigns/{camp['id']}/status").get_json()["status"] == "ready", "status endpoint")
check(n.get(f"/app/campaigns/{camp['id']}").status_code == 404, "other workspaces can't open the campaign")
img, vid = cads[0], cads[1]
bal4 = db.balance(wsrow(xws["id"]))
r = x.post(f"/app/campaigns/{camp['id']}/generate", data={"csrf": csrf(x), "ad": [str(img["id"])], "total": "1"})
check(db.balance(wsrow(xws["id"])) == bal4, "a total that doesn't match what's selected is refused")
r = x.post(f"/app/campaigns/{camp['id']}/ads/{img['id']}", data={"csrf": csrf(x), "brief": "Lose 10 pounds with serum"})
with db.connect() as conn:
    check(conn.execute("SELECT brief FROM campaign_ads WHERE id=?", (img["id"],)).fetchone()[0] == img["brief"],
          "editing a brief re-runs the compliance checks")
x.post(f"/app/campaigns/{camp['id']}/ads/{img['id']}", data={"csrf": csrf(x), "brief": "Serum on a sunny shelf"})
tot = img["credits"] + vid["credits"]
r = x.post(f"/app/campaigns/{camp['id']}/generate", data={"csrf": csrf(x), "ad": [str(img["id"]), str(vid["id"])], "total": str(tot)})
with db.connect() as conn:
    cads = conn.execute("SELECT * FROM campaign_ads WHERE campaign_id=? ORDER BY id", (camp["id"],)).fetchall()
    gj = conn.execute("SELECT * FROM jobs WHERE id=?", (cads[0]["job_id"],)).fetchone()
check(cads[0]["status"] == "queued" and gj and gj["brief"] == "Serum on a sunny shelf" and gj["credits"] == img["credits"],
      "confirmed image ad queued as a real job with the edited brief")
check(cads[1]["status"] == "proposed" and db.balance(wsrow(xws["id"])) == bal4 - img["credits"],
      "video without a product photo isn't started or charged")
check(b"couldn&#39;t start" in x.get(f"/app/campaigns/{camp['id']}").data, "skipped ads are explained")
plan_bodies.clear()
r = x.post(f"/app/campaigns/{camp['id']}/revise", data={"csrf": csrf(x), "notes": "TikTok only please"})
with db.connect() as conn:
    camp2 = conn.execute("SELECT * FROM campaigns WHERE id=?", (camp["id"],)).fetchone()
    kept = conn.execute("SELECT COUNT(*) FROM campaign_ads WHERE campaign_id=? AND status='queued'", (camp["id"],)).fetchone()[0]
check(camp2["status"] == "ready" and "TikTok only please" in plan_bodies[0]["messages"][0]["content"][0]["text"]
      and kept == 1 and camp2["credits"] == 36, "revise re-plans with the notes, keeps generated ads, adds its cost")
with db.connect() as conn:
    conn.execute("UPDATE campaigns SET status='planning', updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (camp["id"],))
    conn.commit()
x.get(f"/app/campaigns/{camp['id']}")
with db.connect() as conn:
    check(conn.execute("SELECT status FROM campaigns WHERE id=?", (camp["id"],)).fetchone()[0] == "ready",
          "an interrupted run stops showing as planning")


def agent_down(body):
    raise llm.LLMError("503: overloaded")


llm._anthropic = agent_down
bal5 = db.balance(wsrow(xws["id"]))
x.post("/app/campaigns", data={"csrf": csrf(x), "offer": "Glow serum", "platforms": ["meta"]})
with db.connect() as conn:
    cf = conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
check(cf["status"] == "failed" and db.balance(wsrow(xws["id"])) == bal5, "AI down: plan fails, nothing charged")
check(b"couldn't make this plan" in x.get(f"/app/campaigns/{cf['id']}").data, "failed plan explains itself")
llm._anthropic = fake_anthropic

print("== SEO / AEO / GEO")
import fetcher  # noqa: E402
import seo  # noqa: E402
for bad in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "https://10.1.2.3/",
            "http://localhost:8080/", "ftp://example.com/", "https://u:p@example.com/", "http://0.0.0.0/",
            "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/", "https://example.com:8443/", "http://192.168.1.1/",
            "http://printer.local/", "http://metadata.internal/"):
    try:
        fetcher.fetch(bad)
        check(False, f"SSRF guard blocks {bad}")
    except fetcher.FetchError:
        check(True, f"SSRF guard blocks {bad}")
real_getaddrinfo = fetcher.socket.getaddrinfo
fetcher.socket.getaddrinfo = lambda host, port, **kw: [(2, 1, 6, "", ("10.0.0.5", port))]
try:
    fetcher.fetch("https://rebind.example/")
    check(False, "a public name that resolves to a private IP is blocked")
except fetcher.FetchError:
    check(True, "a public name that resolves to a private IP is blocked")
fetcher.socket.getaddrinfo = real_getaddrinfo
PAGE = """<!doctype html><html lang="en"><head><title>GlowSerum vitamin C serum, 30 ml | Glow Co</title>
<meta name="description" content="short"><meta name="viewport" content="width=device-width">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"GlowSerum"}</script>
</head><body><h1>GlowSerum</h1><h1>Second</h1><p>A vitamin C serum for mornings. 30 ml lasts about 60 days.</p>
<h2>How do I use it?</h2><p>One drop after cleansing.</p><img src="a.jpg"><a href="/shop">Shop</a></body></html>"""
pg = seo.parse(PAGE, "https://glow.example/serum")
check(pg["title"].startswith("GlowSerum") and len([h for t, h in pg["headings"] if t == "h1"]) == 2
      and pg["jsonld_types"] == ["Product"] and pg["images_no_alt"] == 1, "parser reads title, headings, JSON-LD, images")
rep = seo.audit(pg, "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", None)
flat = {t: lvl for grp in ("seo", "aeo", "geo") for lvl, t, _ in rep[grp]}
check(flat.get("2 H1 headings") == "warn" and flat.get("No canonical link") == "warn"
      and any("Meta description is" in t for t in flat) and flat.get("1 question-style heading") == "pass",
      "audit flags two H1s, missing canonical, short description; credits question headings")
check(flat.get("robots.txt blocks 1 AI crawler") == "warn" and 0 < rep["score"] < 100, "audit reads AI crawler rules from robots.txt")
ld = json.loads(seo.jsonld(pg, {"faq": [{"q": "How long does it last?", "a": "About 60 days."}],
                                "entity": {"name": "Glow Co", "same_as": ["https://instagram.com/glow", "javascript:x"]}},
                           {"name": "Glow Co", "product": "GlowSerum"}))
types = [n["@type"] for n in ld["@graph"]]
check(types == ["Organization", "Product", "FAQPage"] and ld["@graph"][0]["sameAs"] == ["https://instagram.com/glow"],
      "JSON-LD is valid JSON with Organization, the page type and FAQPage (unsafe links dropped)")
check(seo.llms_txt(pg, None, {"name": "Glow Co"}).startswith("# Glow Co"), "llms.txt draft")
check(x.get("/app/seo").status_code == 200, "SEO optimizer open on Growth")
fetched = []


def fake_fetch(url, accept=None):
    fetched.append(url)
    if url.endswith("/robots.txt"):
        return {"url": url, "status": 200, "content_type": "text/plain", "text": "User-agent: *\nAllow: /\n"}
    if url.endswith("/llms.txt"):
        return {"url": url, "status": 404, "content_type": "text/html", "text": "<html>nope</html>"}
    return {"url": url, "status": 200, "content_type": "text/html; charset=utf-8", "text": PAGE}


real_fetch = fetcher.fetch
fetcher.fetch = fake_fetch


def fake_seo(body):
    if (body.get("tool_choice") or {}).get("name") != "submit_seo":
        return fake_anthropic(body)
    return {"model": body["model"], "usage": {"input_tokens": 2000, "output_tokens": 800}, "content": [{"type": "tool_use", "id": "t", "name": "submit_seo", "input": {
        "title": "GlowSerum Vitamin C Serum for Mornings, 30 ml | Glow Co", "description": "d" * 150, "h1": "GlowSerum",
        "summary": "GlowSerum is a 30 ml vitamin C serum.", "faq": [{"q": "How do I use it?", "a": "One drop after cleansing."}],
        "keywords": [{"phrase": "vitamin c serum for mornings", "intent": "commercial"}], "topics": ["Morning routine guide"],
        "entity": {"name": "Glow Co", "kind": "Organization"}, "page_type": "Product", "fixes": ["Use one H1"]}}]}


llm._anthropic = fake_seo
bal6 = db.balance(wsrow(xws["id"]))
r = x.post("/app/seo", data={"csrf": csrf(x), "mode": "url", "url": "https://glow.example/serum", "goal": "", "ai": "1"})
check(r.status_code == 302 and "/app/seo/" in r.headers["Location"], "URL audit runs")
check(fetched[:3] == ["https://glow.example/serum", "https://glow.example/robots.txt", "https://glow.example/llms.txt"],
      "fetches the page, robots.txt and llms.txt")
html = x.get(r.headers["Location"]).get_data(as_text=True)
for bit in ("2 H1 headings", "Fixes, in order", "Use one H1", "vitamin c serum for mornings", "FAQPage", "llms.txt draft",
            "AI crawlers are allowed"):
    check(bit in html, f"audit page shows {bit!r}")
check(db.balance(wsrow(xws["id"])) == bal6 - 4, "AI suggestions charged at cost; the audit itself is free")
r = x.post("/app/seo", data={"csrf": csrf(x), "mode": "text", "text": "# Morning serum guide\nHow do I use vitamin C?\n" + "Words here. " * 40})
check(r.status_code == 302 and db.balance(wsrow(xws["id"])) == bal6 - 4, "pasted draft audit without AI is free")
fetcher.fetch = real_fetch
r = x.post("/app/seo", data={"csrf": csrf(x), "mode": "url", "url": "http://169.254.169.254/"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/seo"), "internal URLs are refused in the app too")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
check(x.get("/app/seo").status_code == 402, "SEO optimizer is Growth+")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})
llm._anthropic = fake_anthropic
pub = client()
rt = pub.get("/robots.txt").get_data(as_text=True)
check("Disallow: /app" in rt and "Sitemap:" in rt, "our own robots.txt keeps the app out and lists the sitemap")
check(pub.get("/llms.txt").status_code == 200 and b"# Campaignify" in pub.get("/llms.txt").data, "our own llms.txt")
own = seo.parse(pub.get("/").get_data(as_text=True), "https://campaignify.ai/")
check("Organization" in own["jsonld_types"] and own["canonical"] and not own["jsonld_invalid"], "our landing page has canonical and valid JSON-LD")

print("== ad-account hub + exports")
import adhub  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402
check(fz.get("/app/ad-accounts").status_code == 402, "free account: ad-account hub shows the upgrade page")
urls = adhub.all_urls()
check(len(urls) > 40 and all(u.startswith("https://") and urlsplit(u).hostname in adhub.OFFICIAL_HOSTS for u in urls),
      "every hub link is https on the platform's own domain")
check(set(adhub.ORDER) == {"meta", "tiktok", "youtube", "x", "linkedin", "pinterest", "snapchat", "reddit"},
      "hub covers Meta, TikTok, YouTube/Google, X, LinkedIn, Pinterest, Snapchat, Reddit")
r = x.get("/app/ad-accounts")
check(r.status_code == 200 and b"Tracking basics" in r.data, "hub overview renders")
for k in adhub.ORDER:
    check(x.get(f"/app/ad-accounts/{k}").status_code == 200, f"{k} guide renders")
check(x.get("/app/ad-accounts/myspace").status_code == 404, "unknown platform 404s")
x.post("/app/ad-accounts/meta/steps/0", data={"csrf": csrf(x)})
x.post("/app/ad-accounts/meta/steps/1", data={"csrf": csrf(x)})
x.post("/app/ad-accounts/meta/steps/1", data={"csrf": csrf(x)})
with db.connect() as conn:
    steps_done = [r["step"] for r in conn.execute("SELECT step FROM adhub_progress WHERE workspace_id=? AND platform='meta'", (xws["id"],))]
check(steps_done == [0], "ticking a step toggles it, saved for the workspace")
check(x.post("/app/ad-accounts/meta/steps/99", data={"csrf": csrf(x)}).status_code == 404, "out-of-range step refused")
from PIL import Image  # noqa: E402
img_path = os.path.join(TMP, "ad_wide.png")
Image.new("RGB", (1600, 900), (20, 120, 200)).save(img_path)
with db.connect() as conn:
    ej = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, status, credits, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?)", (xws["id"], 1, "ad_image", "x", "done", 0, db.now(), db.now())).lastrowid
    eo = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (ej, "image", "https://x/y.png", img_path)).lastrowid
    conn.commit()
check(x.get(f"/app/export/{eo}").status_code == 200 and b"Export sizes" in x.get("/app/library").data, "export page and library button")
r = x.get(f"/app/export/{eo}/meta/feed")
check(r.status_code == 200 and "attachment" in r.headers["Content-Disposition"] and "1080x1350" in r.headers["Content-Disposition"],
      "export downloads with the size in the name")
with Image.open(io.BytesIO(r.data)) as im:
    check(im.size == (1080, 1350), "Meta feed export is exactly 1080 x 1350 (center crop)")
with Image.open(io.BytesIO(x.get(f"/app/export/{eo}/pinterest/pin").data)) as im:
    check(im.size == (1000, 1500), "Pinterest pin export is 1000 x 1500")
check(x.get(f"/app/export/{eo}/meta/nope").status_code == 404, "unknown export size 404s")
check(n.get(f"/app/export/{eo}/meta/feed").status_code == 404, "other workspaces can't export your ads")
import shutil as _sh  # noqa: E402
if _sh.which("ffmpeg"):
    vid_path = os.path.join(TMP, "ad.mp4")
    import subprocess  # noqa: E402
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=1",
                    "-pix_fmt", "yuv420p", vid_path], check=True)
    with db.connect() as conn:
        vo = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (ej, "video", "https://x/y.mp4", vid_path)).lastrowid
        conn.commit()
    r = x.get(f"/app/export/{vo}/youtube/instream")
    out = os.path.join(TMP, "exp.mp4")
    open(out, "wb").write(r.data)
    dims = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    check(r.status_code == 200 and dims == "1920,1080", "video export re-frames 9:16 to 1920 x 1080")

print("== landing pages")
import pages as pagesmod  # noqa: E402
check(fz.get("/app/pages").status_code == 402, "free account: pages show the upgrade page")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
r = x.post("/app/pages", data={"csrf": csrf(x), "title": "Glow Launch"})
with db.connect() as conn:
    pg1 = conn.execute("SELECT * FROM pages WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
check(r.status_code == 302 and pg1["slug"] == "glow-launch" and pg1["status"] == "draft"
      and len(json.loads(pg1["blocks_json"])) == 4, "new page: slug from the title, starter layout, draft")
check(client().get("/p/glow-launch").status_code == 404, "drafts aren't public")
check(x.get(f"/app/pages/{pg1['id']}").status_code == 200, "editor renders")
with db.connect() as conn:
    other_out = conn.execute("SELECT o.id FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id!=? LIMIT 1",
                             (xws["id"],)).fetchone()["id"]
form = {"csrf": csrf(x), "title": "Glow Launch", "slug": "glow-launch", "description": "Morning serum", "theme": "dark",
        "accent": "#FF0000", "action": "save",
        "b0_type": "hero", "b0_headline": "<script>alert(1)</script>Glow", "b0_subhead": "One drop",
        "b0_button_text": "Buy", "b0_button_url": "javascript:alert(1)", "b0_media": str(eo),
        "b1_type": "media", "b1_media": str(other_out), "b1_caption": "x",
        "b2_type": "embed", "b2_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "b2_caption": "Watch",
        "b3_type": "embed", "b3_url": "https://evil.example/iframe", "b3_caption": "",
        "b4_type": "cta", "b4_headline": "Ready?", "b4_body": "", "b4_button_text": "Shop", "b4_button_url": "glow.example/shop",
        "b5_type": "faq", "b5_heading": "Questions", "b5_items": "How?\nOne drop.\n\nWhen?\nMornings.",
        "b6_type": "nope"}
x.post(f"/app/pages/{pg1['id']}", data=form)
with db.connect() as conn:
    pg1 = conn.execute("SELECT * FROM pages WHERE id=?", (pg1["id"],)).fetchone()
bl = json.loads(pg1["blocks_json"])
check(len(bl) == 6 and pg1["theme"] == "dark" and pg1["accent"] == "#FF0000", "blocks saved (unknown block types dropped)")
check(bl[0]["button_url"] == "" and bl[4]["button_url"] == "https://glow.example/shop", "javascript: links dropped; bare domains get https")
check(bl[0]["media"] == eo and bl[1]["media"] is None, "only your own library media can be used")
check(bl[2]["src"] == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ" and bl[3]["src"] is None,
      "embeds are rebuilt for allowed providers only")
x.post(f"/app/pages/{pg1['id']}", data=dict(form, action="up:1"))
with db.connect() as conn:
    bl2 = json.loads(conn.execute("SELECT blocks_json FROM pages WHERE id=?", (pg1["id"],)).fetchone()[0])
check(bl2[0]["type"] == "media" and bl2[1]["type"] == "hero", "blocks move up")
x.post(f"/app/pages/{pg1['id']}", data=dict(form, action="add:benefits"))
with db.connect() as conn:
    bl3 = json.loads(conn.execute("SELECT blocks_json FROM pages WHERE id=?", (pg1["id"],)).fetchone()[0])
check(len(bl3) == 7 and bl3[-1]["type"] == "benefits", "blocks can be added")
x.post(f"/app/pages/{pg1['id']}", data=dict(form, action="publish", b5_items="Make $500 a week\nJoin my team"))
with db.connect() as conn:
    check(conn.execute("SELECT status FROM pages WHERE id=?", (pg1["id"],)).fetchone()[0] == "draft",
          "pages with income or recruiting claims can't be published")
x.post(f"/app/pages/{pg1['id']}", data=dict(form, action="publish"))
pub = client()
r = pub.get("/p/glow-launch")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "Glow" in html and "One drop" in html, "published page is public")
check("<script>alert(1)</script>" not in html and "&lt;script&gt;" in html and "<script" not in html.lower(),
      "member text is escaped; no scripts on public pages")
csp = r.headers.get("Content-Security-Policy", "")
check("default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp, "strict CSP on public pages")
check('src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in html and "evil.example" not in html, "public page embeds only allowed providers")
check(f'/p/glow-launch/m/{eo}' in html and pub.get(f"/p/glow-launch/m/{eo}").status_code == 200, "page media served publicly")
check(pub.get(f"/p/glow-launch/m/{other_out}").status_code == 404, "media not on the page isn't served")
pub.get("/p/glow-launch")
with db.connect() as conn:
    check(conn.execute("SELECT views FROM pages WHERE id=?", (pg1["id"],)).fetchone()[0] == 1, "views counted once per visitor")
r = x.post("/app/pages", data={"csrf": csrf(x), "title": "Second page"})
with db.connect() as conn:
    pg2 = conn.execute("SELECT * FROM pages WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
x.post(f"/app/pages/{pg2['id']}", data={"csrf": csrf(x), "title": "Second page", "slug": "glow-launch", "action": "publish",
                                        "b0_type": "text", "b0_heading": "Hi", "b0_body": "Hello"})
with db.connect() as conn:
    pg2 = conn.execute("SELECT * FROM pages WHERE id=?", (pg2["id"],)).fetchone()
check(pg2["slug"] == "second-page", "taken addresses are refused")
check(pg2["status"] == "draft", "Starter: one published page; the second is refused")
x.post(f"/app/pages/{pg2['id']}", data={"csrf": csrf(x), "title": "Second page", "slug": "admin", "action": "save",
                                        "b0_type": "text", "b0_heading": "Hi", "b0_body": "Hello"})
with db.connect() as conn:
    check(conn.execute("SELECT slug FROM pages WHERE id=?", (pg2["id"],)).fetchone()[0] == "second-page", "reserved addresses refused")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})
x.post(f"/app/pages/{pg2['id']}", data={"csrf": csrf(x), "title": "Second page", "slug": "second-page", "action": "publish",
                                        "b0_type": "text", "b0_heading": "Hi", "b0_body": "Hello"})
with db.connect() as conn:
    check(conn.execute("SELECT status FROM pages WHERE id=?", (pg2["id"],)).fetchone()[0] == "published", "Growth: unlimited published pages")
check(n.get(f"/app/pages/{pg1['id']}").status_code == 404, "other workspaces can't edit your pages")
check(x.get(f"/app/pages/{pg2['id']}/preview").status_code == 200, "owner preview works")
check(pagesmod.safe_url("JaVaScRiPt:alert(1)") == "" and pagesmod.safe_url("data:text/html,x") == ""
      and pagesmod.safe_url("mailto:hi@glow.example") == "mailto:hi@glow.example", "URL sanitizer")

print("== smart links")
check(fz.get("/app/links").status_code == 402, "free account: smart links show the upgrade page")
x.post("/app/links", data={"csrf": csrf(x), "target_url": "javascript:alert(1)"})
x.post("/app/links", data={"csrf": csrf(x), "target_url": "https://glow.example/shop?ref=1", "title": "Bio",
                           "code": "glow-bio", "utm_source": "tiktok", "utm_medium": "social", "utm_campaign": "launch"})
with db.connect() as conn:
    lks = conn.execute("SELECT * FROM links WHERE workspace_id=?", (xws["id"],)).fetchall()
check(len(lks) == 1 and lks[0]["code"] == "glow-bio", "link created with a custom code; javascript: target refused")
check(x.post("/app/links", data={"csrf": csrf(x), "target_url": "https://glow.example", "code": "glow-bio"}).status_code == 302
      and len(lks) == 1, "taken codes refused")
v = client()
r = v.get("/l/glow-bio?utm_source=ig&fbclid=abc&evil=1", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile", "Referer": "https://www.instagram.com/x"})
loc = r.headers["Location"]
check(r.status_code == 302 and loc.startswith("https://glow.example/shop?") and "ref=1" in loc and "utm_source=ig" in loc
      and "utm_medium=social" in loc and "fbclid=abc" in loc and "evil" not in loc,
      "redirect keeps the target's query, adds UTMs, passes through incoming UTMs and click ids only")
v.get("/l/glow-bio", headers={"User-Agent": "facebookexternalhit/1.1"})
with db.connect() as conn:
    ck = conn.execute("SELECT * FROM link_clicks").fetchall()
check(len(ck) == 1 and ck[0]["referrer"] == "www.instagram.com" and ck[0]["device"] == "mobile" and ck[0]["utm_source"] == "ig",
      "clicks recorded with referrer host, device and UTMs; link-preview bots not counted")
with db.connect() as conn:
    cols = [c[1] for c in conn.execute("PRAGMA table_info(link_clicks)")]
check(not any("ip" == c for c in cols), "click rows hold no IP address")
check(client().get("/l/nope-nope").status_code == 404, "unknown link 404s")
html = x.get(f"/app/links/{lks[0]['id']}").get_data(as_text=True)
check("Referrers" in html and "www.instagram.com" in html and "Clicks, last 30 days" in html, "Growth: full link analytics")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
html = x.get(f"/app/links/{lks[0]['id']}").get_data(as_text=True)
check("Referrers" not in html and "Last 7 days" in html and "Full analytics on Growth" in html, "Starter: click counts only")
check(n.get(f"/app/links/{lks[0]['id']}").status_code in (402, 404), "other workspaces can't see your link stats")
rl = client(ip="10.77.0.1")
codes = [rl.get("/l/glow-bio").status_code for _ in range(pages_routes.CLICKS_PER_MIN_PER_IP + 1)]
check(codes[-1] == 429 and codes[0] == 302, "smart links are rate-limited per IP")
x.post(f"/app/links/{lks[0]['id']}/archive", data={"csrf": csrf(x)})
check(client().get("/l/glow-bio").status_code == 404, "archived links stop redirecting")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})

print("== forms + contacts")
import forms as formsmod  # noqa: E402
import time as _time  # noqa: E402
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
check(x.get("/app/forms").status_code == 402 and x.get("/app/contacts").status_code == 402, "forms and contacts are Growth+")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})
x.post("/app/forms", data={"csrf": csrf(x), "name": "Waitlist"})
with db.connect() as conn:
    fm = conn.execute("SELECT * FROM forms WHERE workspace_id=? ORDER BY id DESC", (xws["id"],)).fetchone()
check(fm and len(json.loads(fm["fields_json"])) == 2, "new form starts with name and email")
real_resolve = fetcher.resolve
fetcher.resolve = lambda host, port: "93.184.216.34" if host.endswith("glow.example") else real_resolve(host, port)
base_fields = {"csrf": csrf(x), "name": "Waitlist", "button_text": "Join", "success_message": "You're on the list.",
               "tags": "Waitlist, Glow", "redirect_url": "",
               "f0_key": "name", "f0_label": "Name", "f0_type": "text",
               "f1_key": "email", "f1_label": "Email", "f1_type": "email", "f1_required": "1",
               "f2_label": "Interest", "f2_type": "select", "f2_options": "Skin\nHair", "f2_required": "1",
               "f3_label": "Hair type", "f3_type": "text", "f3_required": "1", "f3_show_if_field": "interest", "f3_show_if_value": "Hair",
               "f4_label": "Send me news", "f4_type": "checkbox"}
x.post(f"/app/forms/{fm['id']}", data=dict(base_fields, webhook_url="http://127.0.0.1/hook", action="save"))
with db.connect() as conn:
    fm = conn.execute("SELECT * FROM forms WHERE id=?", (fm["id"],)).fetchone()
ff = json.loads(fm["fields_json"])
check([f["key"] for f in ff] == ["name", "email", "interest", "hair_type", "send_me_news"] and ff[2]["options"] == ["Skin", "Hair"]
      and ff[3]["show_if"] == {"field": "interest", "equals": "Hair"}, "fields saved with keys, options and show-if logic")
check(not fm["webhook_url"] and fm["tags"] == "waitlist,glow", "internal webhook URLs refused; tags normalized")
x.post(f"/app/forms/{fm['id']}", data=dict(base_fields, webhook_url="https://hooks.glow.example/in", action="save"))
with db.connect() as conn:
    fm = conn.execute("SELECT * FROM forms WHERE id=?", (fm["id"],)).fetchone()
check(fm["webhook_url"] == "https://hooks.glow.example/in" and fm["webhook_secret"], "public https webhook saved with a signing secret")
hooks = []
real_post = formsmod.post_webhook
formsmod.post_webhook = lambda url, secret, payload: hooks.append((url, secret, payload)) or "200"
pubc = client(ip="10.55.0.1")
r = pubc.get(f"/f/{fm['code']}")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "Hair type" in html and ":has(" in html and "default-src 'none'" in r.headers["Content-Security-Policy"],
      "hosted form renders with CSS show-if logic and a strict CSP")
old_stamp = formsmod.stamp(appmod.app.secret_key, fm["code"], _time.time() - 30)
good = {"_t": old_stamp, "f_name": "Ana", "f_email": "Ana@Glow.example", "f_interest": "Skin", "f_send_me_news": "yes"}
check(pubc.post(f"/f/{fm['code']}", data=dict(good, _t="")).status_code == 400, "missing form token refused")
check(pubc.post(f"/f/{fm['code']}", data=dict(good, _t=formsmod.stamp(appmod.app.secret_key, fm["code"]))).status_code == 400,
      "submissions faster than a person can type are refused")
check(pubc.post(f"/f/{fm['code']}", data=dict(good, website="spam")).status_code == 400, "honeypot stops bots")
r = pubc.post(f"/f/{fm['code']}", data=dict(good, f_interest="Hair"))
check(r.status_code == 200 and "Hair type is required." in r.get_data(as_text=True), "show-if field is required once it's shown")
r = pubc.post(f"/f/{fm['code']}", data=dict(good, f_hair_type="curly"))
check(r.status_code == 303 and "sent=1" in r.headers["Location"], "valid submission accepted")
with db.connect() as conn:
    ct = conn.execute("SELECT * FROM contacts WHERE workspace_id=?", (xws["id"],)).fetchone()
    sub = conn.execute("SELECT * FROM form_submissions WHERE form_id=? ORDER BY id DESC", (fm["id"],)).fetchone()
check(ct and ct["email"] == "ana@glow.example" and ct["name"] == "Ana" and ct["tags"] == "waitlist,glow",
      "contact created from the email field, with the form's tags")
check("hair_type" not in json.loads(sub["data_json"]) and json.loads(sub["data_json"])["send_me_news"] == "yes",
      "hidden fields' values are dropped")
url, secret, payload = hooks[-1]
check(url == fm["webhook_url"] and payload["event"] == "form.submitted" and payload["data"]["email"] == "ana@glow.example",
      "webhook sent with the submission")
body = json.dumps(payload, separators=(",", ":")).encode()
check(formsmod.sign(secret, body).startswith("sha256=") and formsmod.sign(secret, body) != formsmod.sign("x", body), "webhook signature")
with db.connect() as conn:
    check(conn.execute("SELECT webhook_status FROM form_submissions WHERE id=?", (sub["id"],)).fetchone()[0] == "200",
          "webhook result recorded on the submission")
# on a page, credited to a smart link
x.post("/app/links", data={"csrf": csrf(x), "target_url": "http://localhost/p/glow-launch", "code": "glow-ig"})
r = client().get("/l/glow-ig")
check("cfy_l=glow-ig" in r.headers["Location"], "smart links to our own pages carry the link code")
with db.connect() as conn:
    pgrow = conn.execute("SELECT * FROM pages WHERE slug='glow-launch'").fetchone()
blocks = json.loads(pgrow["blocks_json"])
pform = {"csrf": csrf(x), "title": pgrow["title"], "slug": "glow-launch", "theme": "light", "accent": "#BDFA25", "action": "save"}
for i, b in enumerate(blocks):
    pform[f"b{i}_type"] = b["type"]
    for k, v in b.items():
        if k not in ("type", "src") and v is not None:
            pform[f"b{i}_{k}"] = str(v)
n_b = len(blocks)
pform[f"b{n_b}_type"] = "form"
pform[f"b{n_b}_heading"] = "Join the waitlist"
pform[f"b{n_b}_form"] = str(fm["id"])
x.post(f"/app/pages/{pgrow['id']}", data=pform)
html = client().get("/p/glow-launch?cfy_l=glow-ig&utm_source=ig").get_data(as_text=True)
check("Join the waitlist" in html and f'action="/f/{fm["code"]}"' in html and 'name="_l" value="glow-ig"' in html
      and 'name="utm_source" value="ig"' in html and 'name="_page" value="glow-launch"' in html,
      "form block renders on the page with page, link and UTM context")
pc2 = client(ip="10.55.0.2")
r = pc2.post(f"/f/{fm['code']}", data={"_t": old_stamp, "_page": "glow-launch", "_l": "glow-ig", "utm_source": "ig",
                                       "f_email": "ana@glow.example", "f_interest": "Skin"})
check(r.status_code == 303 and "/p/glow-launch?sent=" in r.headers["Location"], "page submission returns to the page")
check("You&#39;re on the list." in pc2.get(r.headers["Location"]).get_data(as_text=True), "page shows the success message")
with db.connect() as conn:
    ct = conn.execute("SELECT * FROM contacts WHERE id=?", (ct["id"],)).fetchone()
    kinds = [e["kind"] for e in conn.execute("SELECT kind FROM contact_events WHERE contact_id=? ORDER BY id", (ct["id"],))]
check(ct["submissions"] == 2 and kinds == ["form", "link", "page", "form"], "same email updates the contact; engagement records form, link and page")
rl = client(ip="10.55.0.9")
codes = [rl.post(f"/f/{fm['code']}", data=dict(good, f_email=f"r{i}@glow.example")).status_code for i in range(forms_routes.SUBMITS_PER_IP_FORM_10MIN + 1)]
check(codes[-1] == 429 and codes[0] == 303, "public form submissions are rate-limited per IP")
html = x.get("/app/contacts").get_data(as_text=True)
check("ana@glow.example" in html and "waitlist" in html, "contacts list")
check("ana@" in x.get("/app/contacts?q=ana").get_data(as_text=True) and "ana@" not in x.get("/app/contacts?tag=nope").get_data(as_text=True),
      "search and tag filter")
pc3 = client(ip="10.55.0.3")
pc3.post(f"/f/{fm['code']}", data=dict(good, f_email="evil@glow.example", f_name="=HYPERLINK(1)"))
csvtxt = x.get(f"/app/forms/{fm['id']}/submissions?format=csv").get_data(as_text=True)
check("'=HYPERLINK(1)" in csvtxt and x.get("/app/contacts?format=csv").status_code == 200, "CSV exports neutralize formulas")
check(x.get(f"/app/contacts/{ct['id']}").status_code == 200 and n.get(f"/app/contacts/{ct['id']}").status_code in (402, 404),
      "contact page; other workspaces can't see it")
x.post(f"/app/contacts/{ct['id']}", data={"csrf": csrf(x), "action": "save", "name": "Ana B", "tags": "vip, waitlist", "notes": "Called"})
with db.connect() as conn:
    check(conn.execute("SELECT tags FROM contacts WHERE id=?", (ct["id"],)).fetchone()[0] == "vip,waitlist", "contact tags edited")
x.post(f"/app/contacts/{ct['id']}", data={"csrf": csrf(x), "action": "delete"})
with db.connect() as conn:
    gone = conn.execute("SELECT COUNT(*) FROM contacts WHERE id=?", (ct["id"],)).fetchone()[0] == 0
    left = conn.execute("SELECT data_json FROM form_submissions WHERE contact_id IS NULL AND data_json LIKE '%ana@%'").fetchall()
check(gone and not left, "deleting a contact deletes their form answers")
formsmod.post_webhook = real_post
fetcher.resolve = real_resolve
check(x.get("/app/forms").status_code == 200 and x.get(f"/app/forms/{fm['id']}").status_code == 200
      and x.get(f"/app/forms/{fm['id']}/submissions").status_code == 200, "forms pages render")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "starter"})
check(client().get(f"/f/{fm['code']}").status_code == 404, "forms go offline when the plan no longer includes them")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})

print("== community + courses")
check(x.get("/app/community").status_code == 402, "community is Scale-only (Growth sees the upgrade page)")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "scale"})
check(b"Create community" in x.get("/app/community").data, "setup form")
x.post("/app/community", data={"csrf": csrf(x), "name": "Glow Club", "slug": "glow-club", "about": "For Glow customers"})
with db.connect() as conn:
    cm = conn.execute("SELECT * FROM communities WHERE workspace_id=?", (xws["id"],)).fetchone()
    sp = conn.execute("SELECT * FROM c_spaces WHERE workspace_id=?", (xws["id"],)).fetchone()
check(cm and cm["slug"] == "glow-club" and sp["name"] == "General", "community created with a General space")
x.post("/app/community/spaces", data={"csrf": csrf(x), "name": "Wins", "about": "Share your routine"})
r = x.post("/app/community/courses", data={"csrf": csrf(x), "title": "Morning routine 101"})
with db.connect() as conn:
    co = conn.execute("SELECT * FROM c_courses WHERE workspace_id=?", (xws["id"],)).fetchone()
    mod = conn.execute("SELECT * FROM c_modules WHERE course_id=?", (co["id"],)).fetchone()
check(r.headers["Location"].endswith(f"/app/community/courses/{co['id']}") and mod, "new course opens its editor with a first module")
r = x.post(f"/app/community/courses/{co['id']}", data={"csrf": csrf(x), "action": f"add_lesson:{mod['id']}"})
with db.connect() as conn:
    ls = conn.execute("SELECT * FROM c_lessons WHERE module_id=?", (mod["id"],)).fetchone()
x.post(f"/app/community/lessons/{ls['id']}", data={"csrf": csrf(x), "action": "save", "title": "Step one", "body": "Cleanse.\n\nThen one drop.",
                                                   "embed_url": "https://youtu.be/dQw4w9WgXcQ", "media_id": str(other_out)})
with db.connect() as conn:
    ls = conn.execute("SELECT * FROM c_lessons WHERE id=?", (ls["id"],)).fetchone()
check(ls["title"] == "Step one" and ls["embed_url"] and ls["media_id"] is None, "lesson saved; another workspace's media refused")
x.post("/app/community/events", data={"csrf": csrf(x), "title": "Live Q&A", "starts_at": "2030-01-15T18:00", "tz": "America/Chicago",
                                      "duration": "45", "link": "https://meet.example.com/abc", "about": "Ask anything"})
x.post("/app/community/members", data={"csrf": csrf(x), "email": "mia@glow.example", "name": "Mia", "role": "member"})
with db.connect() as conn:
    inv = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND email='mia@glow.example'", (xws["id"],)).fetchone()
check(inv["status"] == "invited" and inv["invite_code"], "member invited with a one-time link")
anon = client()
check(anon.get("/c/glow-club").status_code == 200 and b"Log in" in anon.get("/c/glow-club").data, "logged-out visitors see the welcome page")
check(anon.get(f"/c/glow-club/s/{sp['id']}").status_code == 302, "spaces need a member login")
check(anon.get("/c/glow-club/join/badcode").status_code == 404, "bad invite codes refused")
mia = client()
code0 = inv["invite_code"]
mia.get(f"/c/glow-club/join/{inv['invite_code']}")
r = mia.post(f"/c/glow-club/join/{inv['invite_code']}", data={"csrf": csrf(mia), "name": "Mia", "email": "other@x.com", "password": "longpassword"})
with db.connect() as conn:
    inv = conn.execute("SELECT * FROM c_members WHERE id=?", (inv["id"],)).fetchone()
check(r.status_code == 302 and inv["status"] == "active" and inv["email"] == "mia@glow.example" and not inv["invite_code"],
      "invite accepted (email fixed to the invite; link used up)")
check(client().get(f"/c/glow-club/join/{code0}").status_code == 404, "used invite link no longer works")
html = mia.get("/c/glow-club").get_data(as_text=True)
check("General" in html and "Wins" in html and "Live Q&amp;A" in html and "Morning routine 101" not in html,
      "member home: spaces and events; draft courses hidden")
r = mia.post(f"/c/glow-club/s/{sp['id']}", data={"csrf": csrf(mia), "title": "<script>x</script>Hello", "body": "First post"})
pid = int(r.headers["Location"].rsplit("/", 1)[1])
html = mia.get(f"/c/glow-club/posts/{pid}").get_data(as_text=True)
check("&lt;script&gt;" in html and "<script>x" not in html, "posts are escaped")
check("default-src 'none'" in mia.get(f"/c/glow-club/posts/{pid}").headers["Content-Security-Policy"], "member pages send the strict CSP")
mia.post(f"/c/glow-club/posts/{pid}", data={"csrf": csrf(mia), "action": "comment", "body": "Me too"})
x.get("/c/glow-club")
x.post(f"/c/glow-club/posts/{pid}", data={"csrf": csrf(x), "action": "comment", "body": "Welcome!"})
with db.connect() as conn:
    coms = conn.execute("SELECT * FROM c_comments WHERE post_id=? ORDER BY id", (pid,)).fetchall()
    owner_m = conn.execute("SELECT * FROM c_members WHERE workspace_id=? AND user_id IS NOT NULL", (xws["id"],)).fetchone()
check(len(coms) == 2 and owner_m["role"] == "owner", "members comment; the workspace owner joins the conversation as owner")
check(mia.post(f"/c/glow-club/posts/{pid}", data={"csrf": csrf(mia), "action": "pin"}).status_code == 403, "members can't pin")
mia.post(f"/c/glow-club/posts/{pid}", data={"csrf": csrf(mia), "action": f"delc:{coms[1]['id']}"})
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM c_comments WHERE post_id=?", (pid,)).fetchone()[0] == 2, "members can't delete others' comments")
x.post(f"/c/glow-club/posts/{pid}", data={"csrf": csrf(x), "action": "pin"})
with db.connect() as conn:
    check(conn.execute("SELECT pinned FROM c_posts WHERE id=?", (pid,)).fetchone()[0] == 1, "owner pins posts")
check(mia.get(f"/c/glow-club/courses/{co['id']}").status_code == 404, "unpublished course hidden from members")
x.post(f"/app/community/courses/{co['id']}", data={"csrf": csrf(x), "action": "save", "title": "Morning routine 101", "about": "", "published": "1"})
html = mia.get(f"/c/glow-club/courses/{co['id']}").get_data(as_text=True)
check("Step one" in html and "0 of 1" in html, "published course with progress")
mia.post(f"/c/glow-club/lessons/{ls['id']}", data={"csrf": csrf(mia)})
check("1 of 1" in mia.get(f"/c/glow-club/courses/{co['id']}").get_data(as_text=True), "lesson marked done")
lh = mia.get(f"/c/glow-club/lessons/{ls['id']}").get_data(as_text=True)
check("youtube-nocookie.com/embed/dQw4w9WgXcQ" in lh and "Then one drop." in lh, "lesson shows video embed and text")
with db.connect() as conn:
    ev = conn.execute("SELECT * FROM c_events WHERE workspace_id=?", (xws["id"],)).fetchone()
ics = mia.get(f"/c/glow-club/events/{ev['id']}.ics").get_data(as_text=True)
check("DTSTART:20300116T000000Z" in ics and "SUMMARY:Live Q&A" in ics, "event .ics converts the time zone to UTC")
check(mia.get(f"/c/glow-club/m/{eo}").status_code == 404, "library media not used in a lesson isn't served")
check(client().get(f"/c/glow-club/join/{cm['join_code']}").status_code == 404, "join link is off by default")
x.post("/app/community", data={"csrf": csrf(x), "name": "Glow Club", "slug": "glow-club", "about": "x", "open_join": "1"})
j = client()
j.get(f"/c/glow-club/join/{cm['join_code']}")
r = j.post(f"/c/glow-club/join/{cm['join_code']}", data={"csrf": csrf(j), "name": "Jo", "email": "jo@glow.example", "password": "longpassword"})
check(r.status_code == 302 and j.get(f"/c/glow-club/s/{sp['id']}").status_code == 200, "open join link lets people join")
with db.connect() as conn:
    jo = conn.execute("SELECT * FROM c_members WHERE email='jo@glow.example'").fetchone()
x.post(f"/app/community/members/{jo['id']}/remove", data={"csrf": csrf(x)})
check(j.get(f"/c/glow-club/s/{sp['id']}").status_code == 302, "removed members lose access right away")
lc = client(ip="10.66.0.1")
for _ in range(10):
    lc.post("/c/glow-club/login", data={"csrf": csrf(lc), "email": "mia@glow.example", "password": "wrong"})
check(lc.post("/c/glow-club/login", data={"csrf": csrf(lc), "email": "mia@glow.example", "password": "longpassword"}).status_code == 429,
      "member logins are throttled")
ml = client(ip="10.66.0.2")
r = ml.post("/c/glow-club/login", data={"csrf": csrf(ml), "email": "mia@glow.example", "password": "longpassword"})
check(r.status_code == 302 and ml.get(f"/c/glow-club/s/{sp['id']}").status_code == 200, "members log in with their own password")
check(n.get("/app/community").status_code in (402, 200) and b"Glow Club" not in n.get("/app/community").data,
      "other workspaces don't see this community's admin")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "growth"})
check(mia.get("/c/glow-club").status_code == 404, "community goes offline when the plan no longer includes it")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "scale"})

print("== plan sync (webhook missed)")
with db.connect() as conn:
    cur = conn.execute("INSERT INTO workspaces (name, created_at) VALUES ('Sync test', ?)", (db.now(),))
    sws = cur.lastrowid
    conn.commit()
sub_obj = {"id": "sub_sync", "object": "subscription", "status": "active", "created": 1, "customer": "cus_sync",
           "metadata": {"app": "campaignify", "kind": "plan", "workspace_id": str(sws), "plan": "scale"},
           "items": {"data": [{"price": {"id": plans.PLANS["scale"]["price_id"]}, "current_period_end": 1893456000}]}}
free_inv = plan_invoice("in_sync", sws, "scale", sub="sub_sync", new_shape=True, amount=0)["data"]["object"]
STRIPE_GET.update({"/subscriptions/search": {"data": [sub_obj]}, "/invoices": {"data": [free_inv]}})
with appmod.app.test_request_context():
    got = appmod._reconcile_plan(sws)
w = wsrow(sws)
check(got == 18000 and w["plan_credits"] == 18000 and plans.plan_code(w) == "scale"
      and w["stripe_subscription_id"] == "sub_sync", "sync finds the subscription by metadata and grants a missed $0 invoice")
with appmod.app.test_request_context():
    check(appmod._reconcile_plan(sws) == 0 and wsrow(sws)["plan_credits"] == 18000, "sync again: nothing granted twice")
check(any(pth == "/subscriptions/sub_sync" for pth, _ in stripe_reads[-3:]), "known subscription is read by id")
STRIPE_GET.update({"/subscriptions/sub_sync": dict(sub_obj, metadata={"app": "other", "workspace_id": str(sws)})})
with appmod.app.test_request_context():
    check(appmod._reconcile_plan(sws) == 0, "a subscription that isn't ours is ignored")
STRIPE_GET.clear()

print("== pages")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": ws1["id"], "plan": "scale"})
for path in ("/app", "/app/library", "/app/brand", "/admin", "/app/new?tool=motion_copy", "/app/new?template=cost",
             "/app/start", "/app/billing", "/app/new?template=hero", "/app/team", "/app/copy", "/app/optimize",
             "/app/copy?kind=landing&platform=landing", "/app/campaigns", "/app/seo", "/app/ad-accounts",
             "/app/ad-accounts/youtube", "/app/pages", "/app/links", "/app/forms", "/app/contacts", "/app/community"):
    check(a.get(path).status_code == 200, f"{path} renders")
check(a.get("/health").get_json()["generation_configured"], "health reports configured")
for path in ("/app", "/app/start", "/app/billing", "/", "/features"):
    html = (a if path != "/" else client()).get(path).get_data(as_text=True)
    check(not re.search(r"higgsfield", html, re.I), f"vendor never named in the UI ({path})")

print(f"\nall {passed} checks passed ({TMP})")
