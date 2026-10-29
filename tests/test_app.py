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
os.environ.update(RS_VIDEO_USD_PER_SECOND="0.206", RS_TALKING_USD_PER_SECOND="0.206")  # fixtures use the old round numbers
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
check(r.status_code == 200 and "The AI marketing studio" in html and "Start free" in html, "logged out: / is the landing page")
for section in ('id="journey"', 'id="gallery"', 'id="reps"', 'id="pricing"', 'id="faq"', "© 2026 Campaignify",
                "/privacy", "/terms", "$29", "$79", "$199", "Checkout coming soon"):
    check(section in html, f"landing has {section}")
check(not re.search(r"higgsfield", html, re.I), "landing never names the vendor")
check(not re.search(r"\$\s?\d[\d,]*\s*(a|per)\s*(week|month)|financial freedom|passive income|six figures", html, re.I),
      "landing makes no income or earnings claims")
check(html.count("previews/") >= 8 and "site/phones.mp4" in html, "gallery shows the example ads")
gal = html.split('id="gallery"', 1)[1].split("</section>", 1)[0]
check(gal.index("site/show-amber.mp4") < gal.index("previews/ad_image.mp4") and gal.count("site/show-") >= 10,
      "the studio showcase clips lead the gallery")
talk = [g for g in appmod.GALLERY if g.get("talk")]
check(len(talk) >= 4 and gal.count('class="lp-snd"') == len(talk) and gal.count('class="lp-g-badge">AI presenter</span>') == len(talk),
      "presenter clips are labeled as AI and get a sound button")
check(all(g["src"].startswith(("site/", "previews/")) and g.get("alt") for g in appmod.GALLERY), "gallery items have alt text")
check(not re.search(r"core\s*java|market\s*place\s*global|weight[- ]control|appetite", html, re.I)
      and not any(re.search(r"corejava|marketplace", g["src"] + g["poster"], re.I) for g in appmod.GALLERY),
      "no CoreJava / MarketPlace Global text or claims on the landing page")
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
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "anyone can sign up; lands on Home (the first-ad form)")
with db.connect() as conn:
    su = conn.execute("SELECT * FROM users WHERE email='stranger@example.com'").fetchone()
    sws = conn.execute("SELECT * FROM workspaces WHERE id=?", (su["workspace_id"],)).fetchone()
    sled = conn.execute("SELECT * FROM ledger WHERE workspace_id=?", (sws["id"],)).fetchall()
check(not su["is_admin"], "self-serve accounts are not admins")
check(sws["credits"] == STARTER and len(sled) == 1 and sled[0]["reason"] == "starter credits",
      "new workspace gets the starter credits")
r = x.get("/")
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "logged in: / goes to the studio")
check(x.get("/app").status_code == 200 and b"See your product in an ad" in x.get("/app").data
      and b"Start a new ad" not in x.get("/app").data and b'name="name"' not in x.get("/signup").data,
      "Home is just the first-ad form for a new account; signup has no name field")
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

r = a.get("/app/new?template=swap", follow_redirects=True)
check(b"CoreJava" in r.data and b"206" in r.data and b"cf-boot" in r.data, "a template opens Create with the product name and the price")

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
check(j2["error"].endswith("Credits refunded.") and j2["error"].count("refunded") == 1,
      "a content-filter failure says the credits were refunded")
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
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "the old first-ad URL opens Home")
r = n.get("/app")
check(r.status_code == 200 and b"Product hero" in r.data and b"Make my first ad" in r.data, "first-ad form renders on Home")
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
check(r.status_code == 302 and "/app/create/batch/" in r.headers["Location"], "first ad queued from one form; opens its batch")
html = n.get(r.headers["Location"]).get_data(as_text=True)
check("While it renders" in html and "Write the caption" in html and "landing page" in html,
      "the first batch shows what to do while it renders")
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
check(b"See your product in an ad" not in n.get("/app").data, "first-ad form goes away after the first ad")

print("== free talking-video preview (phase 2, D2)")
import shutil as _sh  # noqa: E402
import subprocess  # noqa: E402
import preview  # noqa: E402
pv = client()
pv.post("/signup", data={"csrf": csrf(pv), "email": "Pat.Preview+ads@gmail.com", "password": "longpassword"})
home = pv.get("/app").get_data(as_text=True)
check('value="preview"' in home and "Free, once" in home and "More on paid plans" in home and "Needs" not in home,
      "a new free account is offered the free preview; locked nav items fold into one row")
r = pv.post("/app/start", data={"csrf": csrf(pv), "kind": "preview", "product_name": "Glow Drops", "line": "I lost 10 pounds with this",
                                "presenter": "jordan", "photo": (png(), "drops.png")}, content_type="multipart/form-data")
check(r.status_code == 400 and "weight" in r.get_data(as_text=True).lower(), "the preview line is checked against the rules")
r = pv.post("/app/start", data={"csrf": csrf(pv), "kind": "preview", "product_name": "Glow Drops",
                                "line": " ".join(["word"] * 30), "photo": (png(), "drops.png")}, content_type="multipart/form-data")
check(r.status_code == 400 and "8-second" in r.get_data(as_text=True), "a line too long for 8 seconds is refused")
r = pv.post("/app/start", data={"csrf": csrf(pv), "kind": "preview", "product_name": "Glow Drops", "brand_name": "Glow Co",
                                "line": "", "presenter": "jordan", "photo": (png(), "drops.png")}, content_type="multipart/form-data")
check(r.status_code == 302 and "/app/create/batch/" in r.headers["Location"], "the preview starts and opens its batch page")
with db.connect() as conn:
    pws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email='pat.preview+ads@gmail.com'").fetchone()
    pj = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (pws["id"],)).fetchone()
    fp = conn.execute("SELECT * FROM free_previews WHERE workspace_id=?", (pws["id"],)).fetchone()
    pled = conn.execute("SELECT * FROM ledger WHERE job_id=?", (pj["id"],)).fetchall()
pparams = json.loads(pj["params_json"])
check(pj["tool"] == "ugc_video" and pj["credits"] == 0 and abs(pj["cost_usd"] - 8 * 0.2056) < 0.001
      and pparams["resolution"] == "480p" and pparams["preview"] and pparams["duration"] == 8 and pparams["presenter"] == "jordan"
      and pj["brief"].startswith("Meet Glow Drops.") and pws["credits"] == STARTER,
      "free preview: 8 s talking video at 480p, the default line, nothing charged (starter credits kept)")
check(fp and fp["email_norm"] == "patpreview@gmail.com" and fp["job_id"] == pj["id"] and len(pled) == 1 and pled[0]["delta"] == 0
      and "preview" in pled[0]["reason"], "the preview is recorded against the workspace and the normalised email")
html = pv.get(r.headers["Location"]).get_data(as_text=True)
check("This is your free preview" in html and "Make it for real" in html and "While it renders" in html,
      "the batch page explains the preview and offers the real thing")
r = pv.post("/app/start", data={"csrf": csrf(pv), "kind": "preview", "product_name": "Glow Drops", "photo": (png(), "d.png")},
            content_type="multipart/form-data")
with db.connect() as conn:
    nprev = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (pws["id"],)).fetchone()[0]
check(r.status_code == 400 and nprev == 1, "one preview per workspace")
pv2 = client()
pv2.post("/signup", data={"csrf": csrf(pv2), "email": "patpreview@googlemail.com", "password": "longpassword"})
check('value="preview"' not in pv2.get("/app").get_data(as_text=True), "and one per email: a dotted/aliased Gmail doesn't get another")
# the worker renders it at 480p and only keeps a watermarked copy
_est, hf.estimate = hf.estimate, (lambda m, i: 1.6448)
worker.tick()
model, inputs = calls["submit"][-1]
check(model == "bytedance/seedance-2.5/reference-to-video" and inputs["resolution"] == "480p" and inputs["duration"] == 8,
      "the preview is sent at 480p")
if _sh.which("ffmpeg"):
    vid = os.path.join(TMP, "pv.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=480x854:rate=24:duration=1",
                    "-pix_fmt", "yuv420p", vid], check=True)
    _get = worker.requests.get

    class VidResp(FakeResp):
        content = open(vid, "rb").read()
    worker.requests.get = lambda url, timeout=0: VidResp()
    results[f"req{len(calls['submit'])}"] = {"status": "completed", "video": {"url": "https://cdn.example/raw.mp4"}}
    worker.tick()
    worker.requests.get = _get
    with db.connect() as conn:
        pj = conn.execute("SELECT * FROM jobs WHERE id=?", (pj["id"],)).fetchone()
        po = conn.execute("SELECT * FROM outputs WHERE job_id=?", (pj["id"],)).fetchone()
    check(pj["status"] == "done" and po["url"] == "" and os.path.exists(po["path"]) and os.path.getsize(po["path"]) != len(VidResp.content),
          "the finished preview is stored watermarked and the vendor's copy is never linked")
    check(pv.get(f"/media/{po['id']}").status_code == 200, "the watermarked preview plays")
hf.estimate = _est
# a failure on our side lets them try again
with db.connect() as conn:
    conn.execute("UPDATE free_previews SET job_id=NULL, tries=1 WHERE workspace_id=?", (pws["id"],))
    conn.execute("UPDATE jobs SET workspace_id=-1 WHERE workspace_id=?", (pws["id"],))
    conn.commit()
    fake_job = {"id": 999999, "credits": 0, "workspace_id": pws["id"], "params_json": json.dumps({"preview": True})}
    check(preview.status(conn, pws, "pat.preview+ads@gmail.com")["ok"], "after a failure the preview can be tried again")
    conn.execute("UPDATE free_previews SET tries=3 WHERE workspace_id=?", (pws["id"],))
    conn.commit()
    check(not preview.status(conn, pws, "pat.preview+ads@gmail.com")["ok"], "but not forever (3 tries)")
    os.environ["RS_FREE_PREVIEWS_PER_DAY"] = "0"
    conn.execute("UPDATE free_previews SET tries=0 WHERE workspace_id=?", (pws["id"],))
    check(preview.status(conn, pws, "x@example.com")["why"] == "busy", "a daily cap across all accounts")
    os.environ.pop("RS_FREE_PREVIEWS_PER_DAY")
    paid = dict(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws1["id"],)).fetchone(), plan_override="scale")
    check(preview.status(conn, paid, "nobody@example.com")["why"] == "paid", "paid plans don't get the free preview")
    conn.execute("UPDATE jobs SET workspace_id=? WHERE workspace_id=-1", (pws["id"],))
    conn.commit()
check(preview.norm_email("A.B+x@GoogleMail.com") == "ab@gmail.com" and preview.norm_email("a.b+x@work.io") == "a.b@work.io",
      "email normalisation (Gmail dots, +tags)")
html = pv.get("/app").get_data(as_text=True)
check("More on paid plans" not in html and "Needs" in html, "after the first ad the nav expands; unaffordable types say what they need")

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
check(x.get(f"/app/export/{eo}").status_code == 200 and b"Export for Meta / TikTok" in x.get("/app/library").data, "export page and library button")
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
for path in ("/app/new?tool=motion_copy", "/app/new?template=cost"):
    check(a.get(path).status_code == 302, f"{path} redirects into Create")
for path in ("/app", "/app/library", "/app/brand", "/admin", "/app/create?template=cost", "/app/create?type=motion",
             "/app/billing", "/app/team", "/app/copy", "/app/optimize",
             "/app/copy?kind=landing&platform=landing", "/app/campaigns", "/app/seo", "/app/ad-accounts",
             "/app/ad-accounts/youtube", "/app/pages", "/app/links", "/app/forms", "/app/contacts", "/app/community"):
    check(a.get(path).status_code == 200, f"{path} renders")
check(a.get("/health").get_json()["generation_configured"], "health reports configured")
for path in ("/app", "/app?preview=1", "/app/billing", "/", "/features"):
    html = (a if path != "/" else client()).get(path).get_data(as_text=True)
    check(not re.search(r"higgsfield", html, re.I), f"vendor never named in the UI ({path})")

print("== results loop: retry, review, delete, filters (phase 3)")
import jobs_routes  # noqa: E402
with db.connect() as conn:
    w1 = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws1["id"],)).fetchone()
    t0 = db.now()
    fj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, error, credits, cost_usd, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (ws1["id"], 1, "ad_image", "Mug on a desk", json.dumps({"batch": 2, "aspect": "4:5"}), "failed",
                       worker.failure_text("Blocked by the content filter."), 43, 0.21, t0, t0)).lastrowid
    dj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                      "updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (ws1["id"], 1, "photo_to_video", "Steam rising", "{}", "done", 463, 2.3, t0, t0, t0)).lastrowid
    rj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                      "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ws1["id"], 1, "ugc_video", "Talking", "{}", "running", 925, 4.6, t0, t0)).lastrowid
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (dj, "video", "https://x/v.mp4", None))
    db.adjust_credits(conn, ws1["id"], 5000, "test top-up")
    conn.commit()
retry_price = catalog.price_credits("ad_image", batch=2)
html = a.get("/app/library?status=failed").get_data(as_text=True)
check("Credits refunded." in html and f"Try again · {retry_price}" in html and "Steam rising" not in html,
      "a failed card says credits were refunded and offers Try again at today's price; filtered to failed")
check("Edit &amp; retry" not in html.split(f'data-job="{fj}"')[1].split("</article>")[0], "edit & retry only for ads made in a batch")
with db.connect() as conn:
    bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws1["id"],)).fetchone())
r = a.post(f"/app/jobs/{fj}/retry", data={"csrf": csrf(a), "total": str(retry_price - 1)}, headers={"Accept": "application/json"})
check(r.status_code == 409 and r.get_json()["total"] == retry_price, "retry refuses a price that changed")
oc = client()
oc.post("/signup", data={"csrf": csrf(oc), "email": "outsider3@example.com", "password": "longpassword"})
check(oc.post(f"/app/jobs/{fj}/retry", data={"csrf": csrf(oc), "total": str(retry_price)}).status_code == 404
      and oc.post(f"/app/jobs/{dj}/review", data={"csrf": csrf(oc), "verdict": "approved"}).status_code == 404
      and oc.post(f"/app/jobs/{dj}/delete", data={"csrf": csrf(oc)}).status_code == 404, "another workspace can't touch these ads")
r = a.post(f"/app/jobs/{fj}/retry", data={"csrf": csrf(a), "total": str(retry_price)}, headers={"Accept": "application/json"})
with db.connect() as conn:
    nj = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
    old = conn.execute("SELECT * FROM jobs WHERE id=?", (fj,)).fetchone()
    nbal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws1["id"],)).fetchone())
check(nj["status"] == "pending" and nj["parent_job_id"] == fj and nj["brief"] == "Mug on a desk" and nj["credits"] == retry_price
      and json.loads(nj["params_json"])["batch"] == 2 and old["deleted_at"] and nbal == bal - retry_price,
      "Try again makes a new linked job with the same settings and charges today's price")
check(a.post(f"/app/jobs/{dj}/retry", data={"csrf": csrf(a), "total": "463"}).status_code == 400, "only failed ads are retried")
r = a.post(f"/app/jobs/{dj}/review", data={"csrf": csrf(a), "verdict": "approved"}, headers={"Accept": "application/json"})
check(r.get_json()["verdict"] == "approved", "approve an ad")
lib = a.get("/app/library?review=approved").get_data(as_text=True)
check(f'data-job="{dj}"' in lib and f'data-job="{rj}"' not in lib and "Approved" in lib, "filter: approved")
check(f'data-job="{dj}"' not in a.get("/app/library?review=rejected").get_data(as_text=True), "filter: rejected")
a.post(f"/app/jobs/{dj}/review", data={"csrf": csrf(a), "verdict": ""})
check(f'data-job="{dj}"' in a.get("/app/library?review=unreviewed").get_data(as_text=True), "un-reviewing puts it back to not reviewed")
lib = a.get("/app/library?type=video&status=done").get_data(as_text=True)
check(f'data-job="{dj}"' in lib and f'data-job="{rj}"' not in lib and 'type=video' in lib and "status=done" in lib,
      "type and status filters combine and stay in the chip links")
check(a.post(f"/app/jobs/{rj}/delete", data={"csrf": csrf(a)}, headers={"Accept": "application/json"}).status_code == 400,
      "an ad still being made can't be deleted")
a.post(f"/app/jobs/{dj}/delete", data={"csrf": csrf(a)})
with db.connect() as conn:
    check(conn.execute("SELECT deleted_at FROM jobs WHERE id=?", (dj,)).fetchone()[0]
          and f'data-job="{dj}"' not in a.get("/app/library").get_data(as_text=True)
          and conn.execute("SELECT COUNT(*) FROM ledger WHERE workspace_id=?", (ws1["id"],)).fetchone()[0] > 0,
          "delete hides the ad (soft) and keeps the credit history")
d = a.get(f"/app/jobs/cards?ctx=library&ids={rj},{nj['id']}").get_json()["cards"]
check(d[str(rj)]["status"] == "running" and f'data-job="{rj}"' in d[str(rj)]["html"] and str(nj["id"]) in d,
      "cards endpoint returns fresh card HTML to patch in place")
check("finished_at" in a.get(f"/api/jobs?ids={rj}").get_json()["jobs"][0], "the jobs API reports when each finished")
with db.connect() as conn:
    from datetime import datetime as _dt, timedelta as _td
    base_t = _dt(2026, 10, 1, 12, 0, 0)
    for mins in (2, 4, 9):
        conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, credits, created_at, updated_at, finished_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (ws1["id"], 1, "motion_copy", "done", 0, base_t.isoformat(), base_t.isoformat(),
                                                  (base_t + _td(minutes=mins)).isoformat()))
    conn.commit()
    with appmod.app.test_request_context():
        check(jobs_routes.eta_minutes(conn).get("motion_copy") == 4, "time to finish comes from the median of recent jobs")
css = open(os.path.join(os.path.dirname(HERE), "static", "app.js")).read()
check("data-live-cards" in css and "navigator.share" in css and "ArrowRight" in css and "location.reload" not in
      a.get("/app/library").get_data(as_text=True), "live cards, share sheet and keyboard review; no page reloads")

print("== 1080p, re-render, upscale flag, presenter stills (phase 5)")
D10 = 1593 - catalog.price_credits("ugc_video", duration=10)
check(catalog.price_credits("ugc_video", duration=10, resolution="1080p") == 1593 and catalog.hd_delta("ugc_video", 10) == D10
      and catalog.price_credits("photo_to_video", duration=5, resolution="1080p") == 797, "1080p at the vendor's $1.1372/s x 1.4")
try:
    catalog.cost_usd("ugc_video", duration=10, resolution="480p")
    check(False, "480p is not a choice")
except KeyError:
    check(True, "480p is never a paid choice (only the free preview)")
# margin check: no credit pack or plan sells 1080p below the vendor's cost after Stripe's fees
worst = 9.0
for pk in catalog.PACKS:
    net = (pk["usd"] - pk["usd"] * 0.029 - 0.30) / pk["credits"]
    worst = min(worst, catalog.HD_MARKUP * 100 * net)
for code in ("starter", "growth", "scale"):
    pl = plans.PLANS[code]
    net = (pl["usd"] - pl["usd"] * 0.036 - 0.30) / pl["credits"]
    worst = min(worst, catalog.HD_MARKUP * 100 * net)
check(worst > 1.1, f"every pack and plan sells 1080p above the vendor's cost (worst: {worst:.2f}x after fees)")
with db.connect() as conn:
    t0 = db.now()
    vj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                      "updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (ws1["id"], 1, "ugc_video", "Hello there.", json.dumps({"duration": 10, "aspect": "9:16", "presenter": "maya",
                                                                              "label": "Maya · 9:16", "photo_ids": [photo["id"]]}),
                       "done", 925, 4.622, t0, t0, t0)).lastrowid
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (vj, "video", "https://cdn.example/v720.mp4", None))
    conn.commit()
lib = a.get("/app/library?type=talking").get_data(as_text=True)
card_ = lib.split(f'data-job="{vj}"')[1].split("</article>")[0]
check("1080p · 1,593" in card_ and f"+{D10:,} credits vs 720p" in card_ and "Upscale" not in card_,
      "a finished 720p video offers Re-render in 1080p with its price and the delta; no upscale while the flag is off")
r = a.post(f"/app/jobs/{vj}/hd", data={"csrf": csrf(a), "how": "rerender", "total": "1593"}, headers={"Accept": "application/json"})
with db.connect() as conn:
    hj = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
hp = json.loads(hj["params_json"])
check(hj["tool"] == "ugc_video" and hp["resolution"] == "1080p" and hj["credits"] == 1593 and hj["parent_job_id"] == vj
      and hp["label"] == "Maya · 9:16 · 1080p" and abs(hj["cost_usd"] - 11.372) < 0.001, "re-render makes a linked 1080p job")
with appmod.app.test_request_context():
    with db.connect() as conn:
        check(jobs_routes.hd_offer(hj) is None, "a 1080p (or unfinished) video isn't offered again")
_n = len(calls["submit"])
with db.connect() as conn:
    conn.execute("UPDATE jobs SET status='done' WHERE status='pending' AND id<>?", (hj["id"],))
    conn.commit()
_est, hf.estimate = hf.estimate, (lambda m, i: 11.0)
worker.tick()
hf.estimate = _est
check(calls["submit"][-1][1]["resolution"] == "1080p", "the vendor gets resolution 1080p")
catalog.TOOLS["video_upscale"] = {"preview": "photo_to_video", "name": "Upscale to 1080p", "model": "vendor/upscale", "kind": "video",
                                  "usd_per_second": 0.05, "max_photos": 0, "hidden": True}
lib = a.get("/app/library?type=talking").get_data(as_text=True)
up = catalog.price_credits("video_upscale", duration=10)
check(f"Upscale · {up}" in lib, "with the upscale model set, an Upscale button shows its price")
r = a.post(f"/app/jobs/{vj}/hd", data={"csrf": csrf(a), "how": "upscale", "total": str(up)}, headers={"Accept": "application/json"})
with db.connect() as conn:
    uj = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
    check(uj["tool"] == "video_upscale" and worker.build_inputs(conn, uj) == {"video_url": "https://cdn.example/v720.mp4"},
          "upscale sends the finished video's URL")
    conn.execute("UPDATE jobs SET status='done' WHERE id=?", (uj["id"],))
    conn.commit()
del catalog.TOOLS["video_upscale"]
# presenter stills: capped, estimated first, saved once
import presenter_stills  # noqa: E402
sub_before = len(calls["submit"])
_est, hf.estimate = hf.estimate, (lambda m, i: 0.40)
with db.connect() as conn:
    msg = presenter_stills.start(conn)
    st = presenter_stills.state(conn)
check(len(st["jobs"]) == 7 and abs(st["spent"] - 2.80) < 0.001 and "cap" in msg and len(calls["submit"]) - sub_before == 7
      and "AI-generated" in calls["submit"][-1][1]["prompt"] and "not a real" in calls["submit"][-1][1]["prompt"],
      f"presenter stills stop at the $3 cap and describe fictional people ({msg})")
hf.estimate = _est
with db.connect() as conn:
    for pid, j in st["jobs"].items():
        results[j["rid"]] = {"status": "completed", "images": [{"url": f"https://cdn.example/{pid}.png"}]} if pid != "jordan" \
            else {"status": "failed"}
    saved = presenter_stills.collect(conn, fetch=lambda url: png().getvalue())
    st2 = presenter_stills.state(conn)
check(saved == 6 and abs(st2["spent"] - 2.40) < 0.001 and os.path.exists(os.path.join(presenter_stills.DATA_DIR, "maya.jpg")),
      "finished stills are saved; a failed one isn't counted against the cap")
r = a.get("/presenter/maya.jpg")
check(r.status_code == 200 and r.mimetype == "image/jpeg" and a.get("/presenter/nobody.jpg").status_code == 404,
      "stills are served to signed-in members")
html = a.get("/app/create?type=ugc").get_data(as_text=True)
check('/presenter/maya.jpg' in html and "Presenter samples" in a.get("/admin").get_data(as_text=True),
      "presenter cards carry the stills; the admin page shows the tool")

print("== flows F0: builder, AI draft, timeline, engine, tasks (phase 6)")
import flows  # noqa: E402
import flows_engine  # noqa: E402
from datetime import datetime as _dtm, timedelta as _tdl, timezone as _tzu  # noqa: E402
try:
    flows.clean_tree({"steps": [{"type": "launch_rocket"}]})
    check(False, "unknown step types are refused")
except flows.FlowError:
    check(True, "unknown step types are refused")
check(flows.check_graph({"start": "a", "nodes": {"a": {"type": "wait", "next": "b"}, "b": {"type": "wait", "next": "a"}}})
      and flows.check_graph({"start": "a", "nodes": {"a": {"type": "wait", "next": None}, "z": {"type": "wait", "next": None}}}),
      "stored graphs are checked for cycles and orphans")
for t in flows.TEMPLATES:
    tr = flows.from_template(t["id"])
    msgs = [x for x, _ in flows.walk(tr["steps"]) if x["type"] in ("email", "sms")]
    check(msgs and all(compliance.check(m.get("subject") or "", m["body"])["ok"] for m in msgs)
          and not flows.check_graph(flows.compile_tree(tr)) and set(flows.problems(tr, [{"id": 1}])) <= {"trigger"},
          f"template {t['id']}: {len(msgs)} messages pass the rules; the graph is sound")
check(x.get("/app/flows").status_code == 200 and n.get("/app/flows").status_code in (200, 402), "Flows opens on a paid plan")
fc = client()
fc.post("/signup", data={"csrf": csrf(fc), "email": "flowfree@example.com", "password": "longpassword"})
check(fc.get("/app/flows").status_code == 402, "Flows is a Starter feature")
# AI draft: "ad -> page -> form -> welcome email -> wait 1 day -> if clicked tag hot, else text reminder -> day 3 offer"
draft_out = {"name": "Waitlist follow-up", "trigger": {"kind": "form_submitted"}, "steps": [
    {"type": "email", "subject": "Welcome to the waitlist", "body": "Hi {{first_name}},\n\nThanks for joining. Here's what happens next: {{link:glow-launch}}\n\n{{brand}}", "hint": "Day 0: confirm and set expectations."},
    {"type": "wait", "days": 1, "hint": "Give them a day."},
    {"type": "condition", "test_kind": "clicked", "yes": [{"type": "tag", "op": "add", "tag": "hot"}],
     "no": [{"type": "sms", "body": "{{brand}}: a quick reminder, your spot is saved: {{link:glow-launch}}"}]},
    {"type": "wait", "days": 2},
    {"type": "email", "subject": "It's ready", "body": "Hi {{first_name}},\n\nGlow is ready. Get yours here: {{link:glow-launch}}\n\n{{brand}}"},
    {"type": "email", "subject": "Lose 10 pounds", "body": "Burn fat fast"}]}


def fake_flow_llm(body):
    return {"model": body["model"], "usage": {"input_tokens": 2000, "output_tokens": 1500},
            "content": [{"type": "tool_use", "id": "tf", "name": "submit_flow", "input": draft_out}]}


_llm_prev, llm._anthropic = llm._anthropic, fake_flow_llm
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
bal = db.balance(wsrow(xws["id"]))
r = x.post("/app/flows/draft", data={"csrf": csrf(x), "text": "When someone joins the waitlist form, send a welcome email. Wait a "
                                     "day. If they clicked, tag them hot; if not, send a text reminder. Day 3: the offer."})
os.environ.pop("ANTHROPIC_API_KEY")
llm._anthropic = _llm_prev
check(r.status_code == 302 and "/app/flows/" in r.headers["Location"], "describing a flow drafts it and opens the editor")
fid = int(r.headers["Location"].rstrip("/").split("/")[-1].split("?")[0])
with db.connect() as conn:
    fl = conn.execute("SELECT * FROM flows WHERE id=?", (fid,)).fetchone()
tree = flows.unpack(fl["graph_json"])["tree"]
steps = [st for st, _ in flows.walk(tree["steps"])]
cond = next(st for st in steps if st["type"] == "condition")
check(fl["status"] == "draft" and len(steps) == 7 and [st["type"] for st in steps][:3] == ["email", "wait", "condition"]
      and cond["test"]["node"] == steps[0]["id"] and not any("pounds" in (st.get("subject") or "") for st in steps)
      and tree["trigger"]["form_id"] == fm["id"] and db.balance(wsrow(xws["id"])) < bal,
      "the draft is an editable 7-step flow, off, charged at cost; the rule-breaking email is dropped")
tl = flows.timeline(tree)
check([r_["day"] for r_ in tl] == [0, 1, 1, 3] and tl[1]["branch"].startswith("if they clicked") and tl[0]["hint"],
      "Timeline: Day 0 welcome, Day 1 tag or text (by branch), Day 3 offer")
html = x.get(f"/app/flows/{fid}").get_data(as_text=True)
check('id="fl-boot"' in html and "Turn on" in html and "Day by day" in html and "Recent activity" in html,
      "the editor has Steps, Timeline and Activity")
# saving: the server cleans and checks again
bad = json.loads(json.dumps(tree))
bad["steps"][0]["subject"] = "Re: your results"
bad["steps"][0]["body"] = "You will lose weight"
r = x.post(f"/app/flows/{fid}/save", data=json.dumps({"name": "Waitlist", "tree": bad}), content_type="application/json",
           headers={"X-CSRF": csrf(x)})
pr = r.get_json()["problems"]
check(any("Re:" in m for m in pr[bad["steps"][0]["id"]]) and any("weight" in m.lower() for m in pr[bad["steps"][0]["id"]]),
      "saving flags fake Re: subjects and rule-breaking copy on the step")
r = x.post(f"/app/flows/{fid}/state", data={"csrf": csrf(x), "action": "on"})
with db.connect() as conn:
    check(conn.execute("SELECT status FROM flows WHERE id=?", (fid,)).fetchone()[0] == "draft", "a flow with problems can't be turned on")
r = x.post(f"/app/flows/{fid}/save", data=json.dumps({"name": "Waitlist", "tree": tree}), content_type="application/json",
           headers={"X-CSRF": csrf(x)})
check(r.get_json()["problems"] == {} and [i["ok"] for i in r.get_json()["readiness"]][:2] == [True, True]
      and any("task" in i["detail"] for i in r.get_json()["readiness"] if i["label"] == "Email sending"),
      "fixed: ready to turn on; email steps will be tasks until sending is connected")
x.post(f"/app/flows/{fid}/state", data={"csrf": csrf(x), "action": "on"})
with db.connect() as conn:
    fl = conn.execute("SELECT * FROM flows WHERE id=?", (fid,)).fetchone()
check(fl["status"] == "on" and fl["version"] == 1 and fl["version_id"], "turned on: version 1 is snapshotted")
# a form submission enrols the contact; the engine walks it with a frozen clock
formsmod.post_webhook = lambda url, secret, payload: hooks.append((url, secret, payload)) or "200"
r = pubc.post(f"/f/{fm['code']}", data=dict(good, f_email="jo@flow.example", f_name="Jo Park"))
with db.connect() as conn:
    jo = conn.execute("SELECT * FROM contacts WHERE email='jo@flow.example'").fetchone()
    run = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid, jo["id"])).fetchone()
check(run and run["status"] == "waiting" and run["node_id"] == steps[0]["id"], "a form submission starts the flow")
pubc.post(f"/f/{fm['code']}", data=dict(good, f_email="jo@flow.example", f_name="Jo Park"))
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid, jo["id"])).fetchone()[0] == 1,
          "each person goes through a flow once")
t0 = flows_engine.parse(run["wake_at"])
flows_engine.tick(now=t0)
with db.connect() as conn:
    run = conn.execute("SELECT * FROM flow_runs WHERE id=?", (run["id"],)).fetchone()
    tk = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run["id"],)).fetchall()
check(len(tk) == 1 and tk[0]["kind"] == "email" and "Welcome to the waitlist" in tk[0]["title"] and "Hi Jo," in tk[0]["body"]
      and "/p/glow-launch" in tk[0]["body"] and run["node_id"] == steps[1]["id"]
      and flows_engine.parse(run["wake_at"]) == t0 + _tdl(days=1), "no provider: the email becomes a task with the copy; the wait wakes exactly a day later")
flows_engine.tick(now=t0 + _tdl(hours=23, minutes=59))
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM tasks WHERE run_id=?", (run["id"],)).fetchone()[0] == 1, "nothing happens before the wait ends")
    conn.execute("UPDATE flow_runs SET lease_until=? WHERE id=?", (flows_engine.iso(t0 + _tdl(days=1, minutes=1)), run["id"]))
    conn.commit()
flows_engine.tick(now=t0 + _tdl(days=1))
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM tasks WHERE run_id=?", (run["id"],)).fetchone()[0] == 1, "a leased run isn't touched by another pass")
    conn.execute("UPDATE flow_runs SET lease_until=NULL WHERE id=?", (run["id"],))
    conn.commit()
flows_engine.tick(now=t0 + _tdl(days=1, seconds=1))
with db.connect() as conn:
    tk = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run["id"],)).fetchall()
    ev = [e["kind"] for e in conn.execute("SELECT kind FROM flow_events WHERE run_id=? ORDER BY id", (run["id"],))]
check(len(tk) == 2 and tk[1]["kind"] == "sms" and "branch" in ev, "not clicked (no tracking yet): the No branch's text becomes a task")
flows_engine.tick(now=t0 + _tdl(days=3, seconds=2))
with db.connect() as conn:
    run = conn.execute("SELECT * FROM flow_runs WHERE id=?", (run["id"],)).fetchone()
    tk = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run["id"],)).fetchall()
check(len(tk) == 3 and "It's ready" in tk[2]["title"] and run["status"] == "done", "day 3: the offer, then the flow ends")
# tags start other flows; exit_if, stage, notify, webhook, task handlers
t2 = {"trigger": {"kind": "tag_added", "tag": "hot"}, "steps": [
    {"type": "exit_if", "test": {"kind": "has_tag", "tag": "customer"}}, {"type": "stage", "stage": "engaged"},
    {"type": "notify", "message": "{{name}} is hot"}, {"type": "webhook", "url": "https://hooks.glow.example/flow"},
    {"type": "task", "title": "Call {{first_name}}", "body": "Ask about sizes", "due_days": 2},
    {"type": "tag", "op": "remove", "tag": "hot"}]}
with appmod.app.test_request_context():
    with db.connect() as conn:
        t0b = flows_engine.utcnow()
        fid2 = conn.execute("INSERT INTO flows (workspace_id, name, status, graph_json, settings_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)", (xws["id"], "Hot leads", "on", flows.pack(flows.clean_tree(t2)),
                                                       json.dumps({"webhook_secret": "s3"}), db.now(), db.now())).lastrowid
        flows_engine.publish(conn, conn.execute("SELECT * FROM flows WHERE id=?", (fid2,)).fetchone())
        conn.commit()
x.post(f"/app/contacts/{jo['id']}", data={"csrf": csrf(x), "name": "Jo Park", "tags": "waitlist,hot", "action": "save"})
flows_engine.tick(now=flows_engine.utcnow() + _tdl(seconds=5))
with db.connect() as conn:
    jo = conn.execute("SELECT * FROM contacts WHERE id=?", (jo["id"],)).fetchone()
    r2 = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid2, jo["id"])).fetchone()
    t2k = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (r2["id"],)).fetchall()
check(r2["status"] == "done" and jo["stage"] == "engaged" and "hot" not in jo["tags"].split(",")
      and [t_["kind"] for t_ in t2k] == ["notify", "task"] and t2k[0]["title"] == "Jo Park is hot" and t2k[1]["title"] == "Call Jo"
      and flows_engine.parse(t2k[1]["due_at"]) > flows_engine.parse(t2k[1]["created_at"]) + _tdl(days=1, hours=23)
      and hooks[-1][0] == "https://hooks.glow.example/flow" and hooks[-1][1] == "s3" and hooks[-1][2]["contact"]["email"] == "jo@flow.example",
      "adding a tag starts the tag's flow: stage, note, signed webhook, task (due in 2 days), tag removed")
formsmod.post_webhook = real_post
with db.connect() as conn:
    conn.execute("DELETE FROM flow_runs WHERE flow_id=?", (fid2,))
    conn.execute("UPDATE contacts SET tags='hot,customer' WHERE id=?", (jo["id"],))
    flows_engine.enrol(conn, xws["id"], "tag_added", jo["id"], {"tags": ["hot"]})
    conn.commit()
flows_engine.tick(now=flows_engine.utcnow() + _tdl(seconds=5))
with db.connect() as conn:
    r2 = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid2, jo["id"])).fetchone()
check(r2["status"] == "exited" and r2["exit_reason"] == "goal reached", "exit when the goal is met")
# SMS-first flows need consent before anyone enters
with appmod.app.test_request_context():
    with db.connect() as conn:
        fid3 = conn.execute("INSERT INTO flows (workspace_id, name, status, graph_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                            (xws["id"], "Texts", "on", flows.pack(flows.from_template("sms_welcome")), db.now(), db.now())).lastrowid
        flows_engine.publish(conn, conn.execute("SELECT * FROM flows WHERE id=?", (fid3,)).fetchone())
        got = flows_engine.enrol(conn, xws["id"], "form_submitted", jo["id"], {"form_id": fm["id"]})
        skipped = conn.execute("SELECT * FROM flow_events WHERE flow_id=? AND kind='skipped'", (fid3,)).fetchone()
        conn.execute("UPDATE contacts SET sms_consent='form', sms_consent_at=? WHERE id=?", (db.now(), jo["id"]))
        got2 = flows_engine.enrol(conn, xws["id"], "form_submitted", jo["id"], {"form_id": fm["id"]})
        conn.execute("UPDATE flows SET status='paused' WHERE id=?", (fid3,))
        conn.commit()
check(not got and skipped and "agreed to texts" in skipped["detail"] and got2, "a text-first flow only starts for people who agreed to texts")
# versions: editing a live flow doesn't change runs already in it
with db.connect() as conn:
    jo_run = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid, jo["id"])).fetchone()
tree2 = json.loads(json.dumps(tree))
tree2["steps"].append({"type": "notify", "message": "Done"})
x.post(f"/app/flows/{fid}/save", data=json.dumps({"tree": tree2}), content_type="application/json", headers={"X-CSRF": csrf(x)})
with db.connect() as conn:
    fl = conn.execute("SELECT * FROM flows WHERE id=?", (fid,)).fetchone()
check(fl["dirty"] == 1 and fl["version"] == 1, "editing a live flow edits a draft copy")
x.post(f"/app/flows/{fid}/state", data={"csrf": csrf(x), "action": "publish"})
with db.connect() as conn:
    fl = conn.execute("SELECT * FROM flows WHERE id=?", (fid,)).fetchone()
    old_run = conn.execute("SELECT * FROM flow_runs WHERE id=?", (jo_run["id"],)).fetchone()
check(fl["version"] == 2 and not fl["dirty"] and old_run["version_id"] != fl["version_id"], "publish makes version 2; earlier runs keep theirs")
# date triggers, time-of-day waits
with appmod.app.test_request_context():
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET timezone='America/Chicago' WHERE id=?", (xws["id"],))
        dt_tree = {"trigger": {"kind": "date", "tag": "event", "at": "2026-11-05T09:00"},
                   "steps": [{"type": "wait", "days": 1, "at": "09:00"}, {"type": "notify", "message": "Event follow-up"}]}
        fid4 = conn.execute("INSERT INTO flows (workspace_id, name, status, graph_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                            (xws["id"], "Event", "on", flows.pack(flows.clean_tree(dt_tree)), db.now(), db.now())).lastrowid
        flows_engine.publish(conn, conn.execute("SELECT * FROM flows WHERE id=?", (fid4,)).fetchone())
        conn.execute("UPDATE contacts SET tags='event' WHERE id=?", (jo["id"],))
        conn.commit()
flows_engine.tick(now=_dtm(2026, 11, 5, 14, 59, tzinfo=_tzu.utc))
with db.connect() as conn:
    check(not conn.execute("SELECT 1 FROM flow_runs WHERE flow_id=?", (fid4,)).fetchone(), "a date trigger waits for its time (9:00 Chicago = 15:00 UTC)")
flows_engine.tick(now=_dtm(2026, 11, 5, 15, 0, 30, tzinfo=_tzu.utc))
with db.connect() as conn:
    r4 = conn.execute("SELECT * FROM flow_runs WHERE flow_id=?", (fid4,)).fetchone()
check(r4 and r4["wake_at"] == "2026-11-06T15:00:00+00:00", "then enrols everyone tagged; '1 day, then until 09:00' wakes at 9:00 local the next day")
# failures pause the flow and tell the owner
with db.connect() as conn:
    vbad = conn.execute("INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)",
                        (fid4, 9, json.dumps({"tree": dt_tree, "start": "ghost", "nodes": {}}), db.now())).lastrowid
    cids = []
    for i in range(flows_engine.FAIL_PAUSE):
        cids.append(conn.execute("INSERT INTO contacts (workspace_id, email, tags, first_seen, last_seen) VALUES (?,?,?,?,?)",
                                 (xws["id"], f"f{i}@fail.example", "", db.now(), db.now())).lastrowid)
        conn.execute("INSERT INTO flow_runs (flow_id, version_id, workspace_id, contact_id, status, node_id, wake_at, started_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (fid4, vbad, xws["id"], cids[-1], "waiting", "ghost", "2026-01-01T00:00:00+00:00", db.now()))
    conn.commit()
flows_engine.tick(now=_dtm(2026, 11, 5, 16, 0, tzinfo=_tzu.utc))
with db.connect() as conn:
    f4 = conn.execute("SELECT * FROM flows WHERE id=?", (fid4,)).fetchone()
    note = conn.execute("SELECT * FROM tasks WHERE flow_id=? AND kind='notify' AND title LIKE '%paused%'", (fid4,)).fetchone()
check(f4["status"] == "paused" and note, f"{flows_engine.FAIL_PAUSE} failures in a row pause the flow and leave the owner a note")
# tasks inbox and the contact page
html = x.get("/app/tasks").get_data(as_text=True)
check("Welcome to the waitlist" in html and "Copy text" in html and "Call Jo" in html, "the tasks inbox lists what flows left")
with db.connect() as conn:
    tid = conn.execute("SELECT id FROM tasks WHERE workspace_id=? AND done_at IS NULL ORDER BY id LIMIT 1", (xws["id"],)).fetchone()[0]
x.post(f"/app/tasks/{tid}/done", data={"csrf": csrf(x)})
with db.connect() as conn:
    check(conn.execute("SELECT done_at FROM tasks WHERE id=?", (tid,)).fetchone()[0], "tasks can be marked done")
check(n.post(f"/app/tasks/{tid}/done", data={"csrf": csrf(n), "undo": "1"}).status_code in (302, 200), "")
with db.connect() as conn:
    check(conn.execute("SELECT done_at FROM tasks WHERE id=?", (tid,)).fetchone()[0], "another workspace can't touch your tasks")
html = x.get(f"/app/contacts/{jo['id']}").get_data(as_text=True)
check("Waitlist" in html and "Flows" in html, "the contact page shows their flows")
with db.connect() as conn:
    r4 = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (fid4, jo["id"])).fetchone()
x.post(f"/app/flows/runs/{r4['id']}/exit", data={"csrf": csrf(x)})
with db.connect() as conn:
    check(conn.execute("SELECT status FROM flow_runs WHERE id=?", (r4["id"],)).fetchone()[0] == "exited", "take someone out of a flow")
check(n.get(f"/app/flows/{fid}").status_code in (402, 404), "other workspaces can't open the flow")
lst = x.get("/app/flows").get_data(as_text=True)
check("Waitlist" in lst and "Lead magnet" in lst and "Describe it" in lst, "the flows list: yours, templates and Describe it")
r = x.post("/app/flows/new", data={"csrf": csrf(x), "kind": "template", "template": "welcome"})
check(r.status_code == 302 and x.get(r.headers["Location"]).status_code == 200, "start from a template")
nav = x.get("/app").get_data(as_text=True)
check('href="/app/flows"' in nav, "Flows is in the nav (Grow)")

print("== flows F1: email via Resend (phase 7)")
import base64 as _b64  # noqa: E402
import mailer  # noqa: E402
import messaging  # noqa: E402
_sub_ip = [0]


def form_sub(**kw):
    _sub_ip[0] += 1
    return client(ip=f"10.88.{_sub_ip[0] // 200}.{_sub_ip[0] % 200 + 1}").post(f"/f/{fm['code']}", data=dict(good, **kw))


with db.connect() as conn:
    conn.execute("UPDATE flows SET status='paused' WHERE workspace_id=?", (xws["id"],))
    conn.commit()
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
check(any("connected" in m for m in messaging.email_ready(w)) and any("postal" in m for m in messaging.email_ready(w)),
      "without keys, an address and the policy, email doesn't send (steps stay tasks)")
html = x.get("/app/settings/sending").get_data(as_text=True)
check("isn't connected" in html and "Messaging Policy" in html, "Settings → Sending shows the not-connected state")
SECRET = "whsec_" + _b64.b64encode(b"campaignify-test-webhook-secret!").decode()
os.environ.update(RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RESEND_WEBHOOK_SECRET=SECRET)
sent_mail = []


class _MailResp:
    status_code = 200
    text = "{}"

    def __init__(self, i):
        self._i = i

    def json(self):
        return {"id": f"re_{self._i}"}


def fake_resend(url, json=None, headers=None, timeout=0):
    sent_mail.append({"url": url, "body": json, "headers": headers})
    return _MailResp(len(sent_mail))


mailer.requests.post = fake_resend
x.post("/app/settings/sending", data={"csrf": csrf(x), "sender_name": "Glow Co", "reply_to": "team@glow.example",
                                      "postal_address": "1 Main St, Austin, TX 78701", "accept": "1"})
with db.connect() as conn:
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
check(w["messaging_accepted_at"] and w["postal_address"].startswith("1 Main") and not messaging.email_ready(w),
      "with keys, an address and the policy accepted, the workspace can send")
x.post(f"/app/forms/{fm['id']}", data=dict(base_fields, action="save", consent_email="1",
                                           consent_text="Yes, email me about {brand}. I can unsubscribe anytime."))
with db.connect() as conn:
    fm = conn.execute("SELECT * FROM forms WHERE id=?", (fm["id"],)).fetchone()
check(fm["consent_email"] == 1, "the form asks for email consent")
check('name="_consent_email"' in pubc.get(f"/f/{fm['code']}").get_data(as_text=True), "the public form shows the consent box")
etree = {"trigger": {"kind": "form_submitted", "form_id": fm["id"]}, "steps": [
    {"type": "email", "subject": "Welcome, {{first_name}}", "body": "Hi {{first_name}},\n\nHere it is: https://campaignify.ai/p/glow-launch\n\n{{brand}}"},
    {"type": "wait", "days": 1},
    {"type": "condition", "test": {"kind": "clicked", "node": ""}, "yes": [{"type": "notify", "message": "{{name}} clicked"}],
     "no": [{"type": "notify", "message": "{{name}} did not click"}]}]}
etree = flows.clean_tree(etree)
etree["steps"][2]["test"]["node"] = etree["steps"][0]["id"]
r = x.post("/app/flows/new", data={"csrf": csrf(x), "kind": "blank", "name": "Email test"})
efid = int(r.headers["Location"].rstrip("/").split("/")[-1])
x.post(f"/app/flows/{efid}/save", data=json.dumps({"tree": etree}), content_type="application/json", headers={"X-CSRF": csrf(x)})
x.post(f"/app/flows/{efid}/state", data={"csrf": csrf(x), "action": "on"})
form_sub(f_email="mia@flow.example", f_name="Mia Chen", _consent_email="yes")
form_sub(f_email="noconsent@flow.example", f_name="Ned")
with db.connect() as conn:
    mia = conn.execute("SELECT * FROM contacts WHERE email='mia@flow.example'").fetchone()
    ned = conn.execute("SELECT * FROM contacts WHERE email='noconsent@flow.example'").fetchone()
det = json.loads(mia["email_consent_detail"])
check(mia["email_consent"] == "form" and det["form"] == fm["id"] and len(det["text_sha256"]) == 32 and "ip_sha256" in det
      and ned["email_consent"] == "none", "a ticked box records consent with the form, wording hash and IP hash")
bal = db.balance(wsrow(xws["id"]))
T0 = flows_engine.utcnow() + _tdl(seconds=2)
flows_engine.tick(now=T0)
with db.connect() as conn:
    mm = conn.execute("SELECT * FROM messages WHERE contact_id=?", (mia["id"],)).fetchone()
    nm = conn.execute("SELECT * FROM messages WHERE contact_id=?", (ned["id"],)).fetchone()
mail = sent_mail[-1]
b_ = mail["body"]
check(mm["status"] == "sent" and mm["provider_id"] == f"re_{len(sent_mail)}" and b_["to"] == ["mia@flow.example"]
      and b_["from"] == f'"Glow Co via Campaignify" <ws-{xws["id"]}@mail.campaignify.ai>' and b_["reply_to"] == "team@glow.example"
      and b_["subject"] == "Welcome, Mia", "the email goes out as “Glow Co via Campaignify” with Reply-To the customer")
check("1 Main St, Austin, TX 78701" in b_["html"] and f"/u/{mm['token']}" in b_["html"] and "Unsubscribe" in b_["text"]
      and b_["headers"]["List-Unsubscribe"] == f"<https://campaignify.ai/u/{mm['token']}>"
      and b_["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
      and mail["headers"]["Idempotency-Key"] == f"cfy-msg-{mm['id']}", "footer with postal address + unsubscribe; List-Unsubscribe headers; idempotency key")
check("https://campaignify.ai/t/" in b_["text"] and "glow-launch" not in b_["text"].split("Unsubscribe")[0],
      "links are rewritten through tracked redirects")
check(db.balance(wsrow(xws["id"])) == bal - messaging.EMAIL_CREDITS and nm["status"] == "skipped" and "consent" in nm["error"],
      "1 credit per email sent; a contact without consent is skipped with a logged reason")
with db.connect() as conn:
    ln = conn.execute("SELECT * FROM message_links WHERE message_id=?", (mm["id"],)).fetchone()
r = client().get(f"/t/{ln['token']}")
check(r.status_code == 302 and r.headers["Location"].startswith("https://campaignify.ai/p/glow-launch?cfy_c="), "a tracked click redirects (and tags our pages)")
check(client().get(f"/t/o/{mm['token']}.gif").mimetype == "image/gif", "open pixel")
flows_engine.tick(now=T0 + _tdl(days=1, seconds=1))
with db.connect() as conn:
    notes = [t_["title"] for t_ in conn.execute("SELECT title FROM tasks WHERE flow_id=? ORDER BY id", (efid,))]
check("Mia Chen clicked" in notes and "Ned did not click" in notes, "If clicked uses the tracked click")
# webhooks
raw = json.dumps({"type": "email.delivered", "data": {"email_id": mm["provider_id"]}}).encode()
ts = str(int(_time.time()))
hdr = {"svix-id": "msg_1", "svix-timestamp": ts, "svix-signature": mailer.sign_webhook(SECRET, "msg_1", ts, raw)}
check(client().post("/webhooks/resend", data=raw, headers=dict(hdr, **{"svix-signature": "v1,AAAA"}),
                    content_type="application/json").status_code == 400, "a webhook with a bad signature is refused")
r = client().post("/webhooks/resend", data=raw, headers=hdr, content_type="application/json")
r2 = client().post("/webhooks/resend", data=raw, headers=hdr, content_type="application/json")
with db.connect() as conn:
    mm = conn.execute("SELECT * FROM messages WHERE id=?", (mm["id"],)).fetchone()
check(r.status_code == 200 and mm["status"] == "delivered" and r2.get_data(as_text=True) == "already seen", "delivered recorded; a replayed event is ignored")
form_sub(f_email="bounce@flow.example", f_name="Bo", _consent_email="yes")
flows_engine.tick(now=T0 + _tdl(days=1, seconds=5))
with db.connect() as conn:
    bm = conn.execute("SELECT m.* FROM messages m JOIN contacts c ON c.id=m.contact_id WHERE c.email='bounce@flow.example'").fetchone()
raw = json.dumps({"type": "email.bounced", "data": {"email_id": bm["provider_id"], "bounce": {"type": "Permanent"}}}).encode()
hdr = {"svix-id": "msg_2", "svix-timestamp": ts, "svix-signature": mailer.sign_webhook(SECRET, "msg_2", ts, raw)}
client().post("/webhooks/resend", data=raw, headers=hdr, content_type="application/json")
with db.connect() as conn:
    bo = conn.execute("SELECT * FROM contacts WHERE email='bounce@flow.example'").fetchone()
    sup = conn.execute("SELECT * FROM suppressions WHERE workspace_id=? AND address='bounce@flow.example'", (xws["id"],)).fetchone()
check(sup and sup["reason"] == "bounce" and bo["email_consent"] == "none", "a hard bounce suppresses the address")
# one-click unsubscribe
uc = client()
check("Unsubscribe from Glow Co?" in uc.get(f"/u/{mm['token']}").get_data(as_text=True), "unsubscribe page, no login")
r = uc.post(f"/u/{mm['token']}")
with db.connect() as conn:
    mia = conn.execute("SELECT * FROM contacts WHERE id=?", (mia["id"],)).fetchone()
    s_ = conn.execute("SELECT * FROM suppressions WHERE workspace_id=? AND address='mia@flow.example'", (xws["id"],)).fetchone()
check(r.status_code == 200 and mia["unsubscribed_at"] and s_["reason"] == "unsubscribe", "one-click unsubscribe (mail-app POST) works without a login")
with db.connect() as conn:
    run_m = conn.execute("SELECT * FROM flow_runs WHERE flow_id=? AND contact_id=?", (efid, mia["id"])).fetchone()
    vtree = flows.unpack(conn.execute("SELECT graph_json FROM flow_versions WHERE id=?", (run_m["version_id"],)).fetchone()[0])
    conn.execute("INSERT INTO messages (workspace_id, contact_id, run_id, flow_id, node_id, channel, to_addr, subject, body_text, "
                 "status, token, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (xws["id"], mia["id"], run_m["id"], efid, "late1", "email", mia["email"], "Hi", "Hello", "queued", "tok-late", db.now()))
    conn.commit()
messaging.send_queued()
with db.connect() as conn:
    check(conn.execute("SELECT status FROM messages WHERE token='tok-late'").fetchone()[0] == "suppressed", "nothing more goes to someone who unsubscribed")
# daily cap: the warm-up limit pauses the flow and tells the owner
with db.connect() as conn:
    for i in range(messaging.WARMUP_CAP):
        conn.execute("INSERT INTO messages (workspace_id, contact_id, flow_id, channel, status, token, created_at, sent_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (xws["id"], ned["id"], efid, "email", "sent", f"cap{i}", db.now(), db.now()))
    conn.commit()
form_sub(f_email="capped@flow.example", f_name="Cap", _consent_email="yes")
flows_engine.tick(now=flows_engine.utcnow() + _tdl(seconds=3))
with db.connect() as conn:
    fe = conn.execute("SELECT status FROM flows WHERE id=?", (efid,)).fetchone()[0]
    capm = conn.execute("SELECT m.status FROM messages m JOIN contacts c ON c.id=m.contact_id WHERE c.email='capped@flow.example'").fetchone()
    capnote = conn.execute("SELECT 1 FROM tasks WHERE flow_id=? AND title LIKE '%daily email limit%'", (efid,)).fetchone()
check(fe == "paused" and capm["status"] == "queued" and capnote, "the daily cap pauses the flow, keeps the email queued and tells the owner")
# CSV import with a consent column
csvdata = "email,name,tags,email_consent\nimp1@flow.example,Ivy,vip,yes\nimp2@flow.example,Ian,,no\nnot-an-email,,,yes\n"
x.post("/app/contacts/import", data={"csrf": csrf(x), "source": "Old newsletter (double opt-in)",
                                     "file": (io.BytesIO(csvdata.encode()), "list.csv")}, content_type="multipart/form-data")
with db.connect() as conn:
    i1 = conn.execute("SELECT * FROM contacts WHERE email='imp1@flow.example'").fetchone()
    i2 = conn.execute("SELECT * FROM contacts WHERE email='imp2@flow.example'").fetchone()
check(i1["email_consent"] == "import" and "Old newsletter" in i1["email_consent_detail"] and i1["tags"] == "vip"
      and i2["email_consent"] == "none", "CSV import records consent only where the column says yes")
html = x.get(f"/app/contacts/{i1['id']}").get_data(as_text=True)
check("Consent" in html and "in an import" in html, "the contact page shows consent")
pol = client().get("/messaging-policy").get_data(as_text=True)
check("STOP" in pol and "postal address" in pol and "Messaging Policy" in client().get("/terms").get_data(as_text=True)
      and "Resend" in client().get("/privacy").get_data(as_text=True) and "/messaging-policy" in client().get("/sitemap.xml").get_data(as_text=True),
      "Messaging Policy page; Terms and Privacy updated")
print("== custom sending domains (phase 8)")
domain_calls = []


class _DomResp:
    def __init__(self, data):
        self.status_code, self._d, self.text = 200, data, "{}"

    def json(self):
        return self._d


RECS = [{"record": "SPF", "type": "MX", "name": "send", "value": "feedback-smtp.us-east-1.amazonses.com", "priority": 10,
         "status": "not_started"}, {"record": "DKIM", "type": "TXT", "name": "resend._domainkey", "value": "p=MIGf", "status": "not_started"}]
dom_status = ["pending"]


def fake_dom_post(url, json=None, headers=None, timeout=0):
    domain_calls.append(("post", url, json))
    if url.endswith("/domains"):
        return _DomResp({"id": "dom_1", "name": json["name"], "status": "not_started", "records": RECS})
    if url.endswith("/emails"):
        return fake_resend(url, json=json, headers=headers, timeout=timeout)
    return _DomResp({"object": "domain", "id": "dom_1"})


mailer.requests.post = fake_dom_post
mailer.requests.get = lambda url, headers=None, timeout=0: (domain_calls.append(("get", url, None)) or
                                                            _DomResp({"id": "dom_1", "status": dom_status[0], "records": RECS}))
r = x.post("/app/settings/sending/domain", data={"csrf": csrf(x), "action": "add", "domain": "evil.campaignify.ai"})
check(not any(c[1].endswith("/domains") for c in domain_calls), "our own domain can't be claimed")
x.post("/app/settings/sending/domain", data={"csrf": csrf(x), "action": "add", "domain": "glow.example", "local_part": "Hi"})
html = x.get("/app/settings/sending").get_data(as_text=True)
check("resend._domainkey" in html and "Waiting for DNS" in html and "hi@glow.example" in html, "adding a domain shows its DNS records")
x.post("/app/settings/sending/domain", data={"csrf": csrf(x), "action": "check"})
with db.connect() as conn:
    d_ = conn.execute("SELECT * FROM sender_domains WHERE workspace_id=?", (xws["id"],)).fetchone()
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
    m_ = conn.execute("SELECT * FROM messages WHERE workspace_id=? AND token IS NOT NULL ORDER BY id DESC", (xws["id"],)).fetchone()
    check(d_["status"] == "pending" and messaging.build(conn, w, m_)["from"].endswith("@mail.campaignify.ai>"),
          "until it's verified, mail falls back to the shared domain")
dom_status[0] = "verified"
x.post("/app/settings/sending/domain", data={"csrf": csrf(x), "action": "check"})
with db.connect() as conn:
    d_ = conn.execute("SELECT * FROM sender_domains WHERE workspace_id=?", (xws["id"],)).fetchone()
    check(d_["status"] == "verified" and d_["verified_at"] and messaging.build(conn, w, m_)["from"] == '"Glow Co" <hi@glow.example>',
          "once verified, the next email uses the custom From")
with db.connect() as conn:
    keep = conn.execute("SELECT plan, plan_override FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
    conn.execute("UPDATE workspaces SET plan_override=NULL, plan='free' WHERE id=?", (xws["id"],))
    w2 = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
    check(messaging.build(conn, w2, m_)["from"].endswith("@mail.campaignify.ai>"), "below Growth, the custom domain isn't used")
    conn.execute("UPDATE workspaces SET plan=?, plan_override=? WHERE id=?", (keep["plan"], keep["plan_override"], xws["id"]))
    conn.commit()
x.post("/app/settings/sending/domain", data={"csrf": csrf(x), "action": "remove"})
with db.connect() as conn:
    check(not conn.execute("SELECT 1 FROM sender_domains WHERE workspace_id=?", (xws["id"],)).fetchone(), "remove the domain")
print("== flows F3: texts via Twilio, registration (phase 9)")
import sms  # noqa: E402
import smser  # noqa: E402
check(flows.sms_segments("a" * 160) == (1, "GSM-7") and flows.sms_segments("a" * 180) == (2, "GSM-7")
      and flows.sms_segments("Hi 😀" + "a" * 60)[0] == 1 and flows.sms_segments("😀" * 40)[0] == 2
      and flows.sms_segments("é" * 70) == (1, "GSM-7") and flows.sms_segments("ł" * 71) == (2, "UCS-2"),
      "segments: GSM-7 160/153, Unicode (emoji) 70/67")
check(sms.credits_per_segment() == 2 and sms.fees("tollfree")["now"] == 430 and sms.fees("10dlc", 500)["once"]
      == {"Brand registration (low volume)": 400, "Campaign vetting": 1500}, "prices: segment and number at 2x, registration at cost")
a.post("/admin/plan", data={"csrf": csrf(a), "workspace_id": xws["id"], "plan": "scale"})
html = x.get("/app/settings/sms").get_data(as_text=True)
check("isn't connected" in html and "Toll-free number" in html and "430 credits" in html and "Recommended" in html,
      "the wizard shows routes, prices before anything is charged, and the not-connected state")
os.environ.update(TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="tw-secret")
tw_calls = []
tw_state = {"tf": "PENDING_REVIEW", "profile": "pending-review", "brand": "PENDING", "campaign": "IN_PROGRESS"}


class _TW:
    def __init__(self, d):
        self.status_code, self._d = 200, d
        self.text = json.dumps(d)

    def json(self):
        return self._d


def tw_post(url, data=None, auth=None, timeout=0):
    tw_calls.append(("post", url, data))
    if url.endswith("/Services"):
        return _TW({"sid": "MG1"})
    if url.endswith("IncomingPhoneNumbers.json"):
        return _TW({"sid": "PN1", "phone_number": data["PhoneNumber"]})
    if url.endswith("/Tollfree/Verifications"):
        return _TW({"sid": "HH1"})
    if url.endswith("/Messages.json"):
        return _TW({"sid": f"SM{len(tw_calls)}"})
    if url.endswith("/CustomerProfiles"):
        return _TW({"sid": "BU2"})
    if url.endswith("/BrandRegistrations"):
        return _TW({"sid": "BN1"})
    if url.endswith("/Usa2p"):
        return _TW({"sid": "QE1"})
    return _TW({"sid": "XX"})


def tw_get(url, params=None, auth=None, timeout=0):
    tw_calls.append(("get", url, params))
    if "AvailablePhoneNumbers" in url:
        return _TW({"available_phone_numbers": [{"phone_number": "+18005550100" if "TollFree" in url else "+15125550100"}]})
    if "/Tollfree/Verifications/" in url:
        return _TW({"status": tw_state["tf"]})
    if "/CustomerProfiles/" in url:
        return _TW({"status": tw_state["profile"]})
    if "/BrandRegistrations/" in url:
        return _TW({"status": tw_state["brand"]})
    if "/Usa2p/" in url:
        return _TW({"campaign_status": tw_state["campaign"]})
    return _TW({})


smser.requests.post, smser.requests.get = tw_post, tw_get
biz = {"csrf": csrf(x), "path": "tollfree", "volume": "400", "legal_name": "Glow Co LLC", "business_type": "llc",
       "website": "https://glow.example", "street": "1 Main St", "city": "Austin", "state": "TX", "zip": "78701",
       "first_name": "Gia", "last_name": "Lo", "email": "gia@glow.example", "phone": "+15125550199",
       "use_case": "Marketing texts to opted-in customers.", "message_flow": "Unchecked box on our web form."}
r = x.post("/app/settings/sms", data=dict(biz, legal_name="", action="register"), follow_redirects=True)
check("Fill in: legal name" in r.get_data(as_text=True), "missing details are listed before anything is charged")
bal = db.balance(wsrow(xws["id"]))
r = x.post("/app/settings/sms", data=dict(biz, action="register"), follow_redirects=True)
with db.connect() as conn:
    sp = conn.execute("SELECT * FROM sms_profiles WHERE workspace_id=?", (xws["id"],)).fetchone()
check(sp["status"] == "submitted" and sp["phone_number"] == "+18005550100" and sp["messaging_service_sid"] == "MG1"
      and sp["verification_sid"] == "HH1" and db.balance(wsrow(xws["id"])) == bal - 430
      and any(c[1].endswith("/Services") and c[2]["InboundRequestUrl"].endswith("/webhooks/twilio/sms") for c in tw_calls),
      "toll-free: number bought, Messaging Service with our webhooks, verification submitted, first month charged")
with db.connect() as conn:
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
check(any("Finish registering" in m for m in sms.sms_ready(w)), "texts wait for approval (steps stay tasks meanwhile)")
sms.advance_profiles()
tw_state["tf"] = "TWILIO_APPROVED"
sms.advance_profiles(flows_engine.utcnow() + _tdl(minutes=31))
with db.connect() as conn:
    sp = conn.execute("SELECT * FROM sms_profiles WHERE workspace_id=?", (xws["id"],)).fetchone()
    w = conn.execute("SELECT * FROM workspaces WHERE id=?", (xws["id"],)).fetchone()
    note = conn.execute("SELECT 1 FROM tasks WHERE workspace_id=? AND title LIKE '%texting number is approved%'", (xws["id"],)).fetchone()
check(sp["status"] == "active" and note and not sms.sms_ready(w), "approval is picked up by polling; the owner gets a note")
# 10DLC: each step taken when the last one is approved; state lives in the database (restart-safe)
with db.connect() as conn:
    conn.execute("INSERT INTO sms_profiles (workspace_id, path, status, fields_json, customer_profile_sid, brand_type, "
                 "messaging_service_sid, phone_number, created_at, updated_at, number_fee_month) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (ws1["id"], "10dlc", "submitted", json.dumps(biz), "BU9", "low_volume", "MG9", "+15125550111", db.now(), db.now(),
                  flows_engine.utcnow().strftime("%Y-%m")))
    conn.commit()
seq = []
for st_, now_ in (({"profile": "twilio-approved"}, 62), ({"brand": "APPROVED"}, 124), ({"campaign": "VERIFIED"}, 186)):
    tw_state.update(st_)
    sms.advance_profiles(flows_engine.utcnow() + _tdl(minutes=now_))
    with db.connect() as conn:
        seq.append(conn.execute("SELECT status FROM sms_profiles WHERE workspace_id=?", (ws1["id"],)).fetchone()[0])
check(seq == ["brand_pending", "campaign_pending", "active"], f"10DLC: profile → brand → campaign → active ({seq})")
# sending: consent, quiet hours, opt-out line, segments and credits
with db.connect() as conn:
    conn.execute("UPDATE flows SET status='paused' WHERE workspace_id=?", (xws["id"],))
    conn.execute("UPDATE workspaces SET timezone='America/Chicago' WHERE id=?", (xws["id"],))
    tx = conn.execute("INSERT INTO contacts (workspace_id, email, name, phone, phone_e164, sms_consent, tags, first_seen, last_seen) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (xws["id"], "tex@flow.example", "Tex", "512-555-0123", "+15125550123", "form",
                                                     "", db.now(), db.now())).lastrowid
    nx = conn.execute("INSERT INTO contacts (workspace_id, email, name, phone_e164, tags, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                      (xws["id"], "nox@flow.example", "Nox", "+15125550124", "", db.now(), db.now())).lastrowid
    conn.commit()
stree = flows.clean_tree({"trigger": {"kind": "tag_added", "tag": "textme"}, "steps": [
    {"type": "condition", "test": {"kind": "has_consent", "channel": "email"}, "yes": [], "no": []},
    {"type": "sms", "body": "{{brand}}: your spot is saved. " + "Details at the link. " * 6}]})
r = x.post("/app/flows/new", data={"csrf": csrf(x), "kind": "blank", "name": "Texts"})
sfid = int(r.headers["Location"].rstrip("/").split("/")[-1])
x.post(f"/app/flows/{sfid}/save", data=json.dumps({"tree": stree}), content_type="application/json", headers={"X-CSRF": csrf(x)})
x.post(f"/app/flows/{sfid}/state", data={"csrf": csrf(x), "action": "on"})
with db.connect() as conn:
    for cid_ in (tx, nx):
        flows_engine.enrol(conn, xws["id"], "tag_added", cid_, {"tags": ["textme"]})
    conn.commit()
night = _dtm(2026, 11, 10, 3, 30, tzinfo=_tzu.utc)       # 21:30 in Chicago
flows_engine.tick(now=night)
with db.connect() as conn:
    tm = conn.execute("SELECT * FROM messages WHERE contact_id=? AND channel='sms'", (tx,)).fetchone()
    nm_ = conn.execute("SELECT * FROM messages WHERE contact_id=? AND channel='sms'", (nx,)).fetchone()
check(tm["status"] == "queued" and tm["scheduled_at"] == "2026-11-10T15:00:00+00:00", "a text due at 21:30 local waits for 9:00")
check(nm_["status"] == "skipped" and "consent" in nm_["error"], "no text consent: never sent")
bal = db.balance(wsrow(xws["id"]))
n_before = len([c for c in tw_calls if c[1].endswith("/Messages.json")])
flows_engine.tick(now=_dtm(2026, 11, 10, 15, 0, 5, tzinfo=_tzu.utc))
with db.connect() as conn:
    tm = conn.execute("SELECT * FROM messages WHERE id=?", (tm["id"],)).fetchone()
sent_body = [c for c in tw_calls if c[1].endswith("/Messages.json")][-1][2]
check(tm["status"] == "sent" and sent_body["Body"].endswith(sms.OPT_OUT_LINE) and sent_body["To"] == "+15125550123"
      and sent_body["MessagingServiceSid"] == "MG1" and tm["segments"] == flows.sms_segments(sent_body["Body"])[0] == 2
      and db.balance(wsrow(xws["id"])) == bal - 2 * sms.credits_per_segment()
      and len([c for c in tw_calls if c[1].endswith("/Messages.json")]) == n_before + 1,
      "at 9:00 it goes, with the opt-out line on the first text; 2 segments x 2 credits")
# inbound: STOP, HELP, signatures
url_in = flows_engine.PUBLIC_URL + "/webhooks/twilio/sms"
def tw_in(body, sid):
    p_ = {"From": "+15125550123", "To": "+18005550100", "Body": body, "MessageSid": sid}
    return client().post("/webhooks/twilio/sms", data=p_, headers={"X-Twilio-Signature": smser.sign(url_in, p_)})
check(client().post("/webhooks/twilio/sms", data={"From": "+1", "To": "+18005550100", "Body": "STOP"},
                    headers={"X-Twilio-Signature": "nope"}).status_code == 403, "unsigned inbound texts are refused")
h = tw_in("help", "SMh1").get_data(as_text=True)
check("<Message>" in h and "Reply STOP to opt out" in h, "HELP gets the brand, contact and STOP line")
with db.connect() as conn:
    conn.execute("INSERT INTO messages (workspace_id, contact_id, flow_id, channel, to_addr, body_text, status, token, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (xws["id"], tx, sfid, "sms", "+15125550123", "Glow Co: one more", "queued", "tq1", db.now()))
    conn.commit()
tw_in("Stop", "SMs1")
tw_in("Stop", "SMs1")
with db.connect() as conn:
    txc = conn.execute("SELECT * FROM contacts WHERE id=?", (tx,)).fetchone()
    q_ = conn.execute("SELECT status FROM messages WHERE token='tq1'").fetchone()[0]
    sup = conn.execute("SELECT * FROM suppressions WHERE workspace_id=? AND channel='sms'", (xws["id"],)).fetchall()
check(txc["sms_stopped_at"] and q_ == "suppressed" and len(sup) == 1 and sup[0]["reason"] == "stop",
      "STOP suppresses at once (queued texts dropped); a replayed webhook is ignored")
tw_in("START", "SMs2")
with db.connect() as conn:
    check(not conn.execute("SELECT sms_stopped_at FROM contacts WHERE id=?", (tx,)).fetchone()[0], "START opts back in")
st_url = flows_engine.PUBLIC_URL + "/webhooks/twilio/status"
pp = {"MessageSid": tm["provider_id"], "MessageStatus": "delivered"}
client().post("/webhooks/twilio/status", data=pp, headers={"X-Twilio-Signature": smser.sign(st_url, pp)})
with db.connect() as conn:
    check(conn.execute("SELECT status FROM messages WHERE id=?", (tm["id"],)).fetchone()[0] == "delivered", "delivery status callbacks")
# monthly number fee on the 1st
with db.connect() as conn:
    conn.execute("UPDATE sms_profiles SET number_fee_month='2026-11' WHERE workspace_id=?", (xws["id"],))
    conn.commit()
bal = db.balance(wsrow(xws["id"]))
sms.advance_profiles(_dtm(2026, 12, 1, 6, 0, tzinfo=_tzu.utc))
with db.connect() as conn:
    led = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (xws["id"],)).fetchone()
check(led["reason"] == "SMS number · December" and led["delta"] == -430 and db.balance(wsrow(xws["id"])) == bal - 430,
      "the number's rent is charged on the 1st (ledger: SMS number · December)")
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
    os.environ.pop(k)

print("== flows F4: A/B split, re-entry, moving runs, analytics (phase 10)")
ab = flows.clean_tree({"trigger": {"kind": "tag_added", "tag": "abtest"}, "steps": [
    {"type": "split", "pct": 140, "yes": [{"type": "notify", "message": "A path"}], "no": [{"type": "wait", "days": 2},
                                                                                       {"type": "notify", "message": "B path"}]},
    {"type": "tag", "op": "add", "tag": "abdone"}]})
check(ab["steps"][0]["pct"] == 99 and [r_["branch"] for r_ in flows.timeline(ab)][:2] == ["A (99%)", "B (1%)"]
      and not flows.check_graph(flows.compile_tree(ab)), "A/B split: share clamped, both paths on the timeline, they rejoin")
ab["steps"][0]["pct"] = 50
r = x.post("/app/flows/new", data={"csrf": csrf(x), "kind": "blank", "name": "AB"})
abid = int(r.headers["Location"].rstrip("/").split("/")[-1])
x.post(f"/app/flows/{abid}/save", data=json.dumps({"tree": ab}), content_type="application/json", headers={"X-CSRF": csrf(x)})
x.post(f"/app/flows/{abid}/state", data={"csrf": csrf(x), "action": "on"})
abc = []
with db.connect() as conn:
    for i in range(40):
        cid_ = conn.execute("INSERT INTO contacts (workspace_id, email, tags, first_seen, last_seen) VALUES (?,?,?,?,?)",
                            (xws["id"], f"ab{i}@flow.example", "abtest", db.now(), db.now())).lastrowid
        abc.append(cid_)
        flows_engine.enrol(conn, xws["id"], "tag_added", cid_, {"tags": ["abtest"]})
    conn.commit()
TA = flows_engine.utcnow() + _tdl(seconds=2)
flows_engine.tick(now=TA)
with db.connect() as conn:
    sides = [e["detail"] for e in conn.execute("SELECT detail FROM flow_events WHERE flow_id=? AND kind='branch'", (abid,))]
check(len(sides) == 40 and 8 <= sides.count("A") <= 32, f"each person is assigned A or B ({sides.count('A')} A / {sides.count('B')} B)")
flows_engine.tick(now=TA + _tdl(days=2, seconds=1))
with db.connect() as conn:
    done = conn.execute("SELECT COUNT(*) FROM flow_runs WHERE flow_id=? AND status='done'", (abid,)).fetchone()[0]
    c0 = conn.execute("SELECT tags FROM contacts WHERE id=?", (abc[0],)).fetchone()[0]
check(done == 40 and "abdone" in c0, "both paths rejoin and finish")
html = x.get(f"/app/flows/{abid}?tab=activity").get_data(as_text=True)
check("Funnel" in html and "Path A" in html and "Path B" in html and "Started" in html, "Activity: funnel and A/B side by side")
csvr = x.get(f"/app/flows/{abid}/events.csv")
check(csvr.mimetype == "text/csv" and "ab0@flow.example" in csvr.get_data(as_text=True) and "branch" in csvr.get_data(as_text=True),
      "events export as CSV")
# re-entry
with db.connect() as conn:
    flows_engine.enrol(conn, xws["id"], "tag_added", abc[0], {"tags": ["abtest"]})
    n1 = conn.execute("SELECT COUNT(*) FROM flow_runs WHERE flow_id=? AND contact_id=?", (abid, abc[0])).fetchone()[0]
    conn.commit()
x.post(f"/app/flows/{abid}/settings", data={"csrf": csrf(x), "reenter": "after_finish", "cooldown_days": "3"})
with db.connect() as conn:
    ended = flows_engine.parse(conn.execute("SELECT ended_at FROM flow_runs WHERE flow_id=? AND contact_id=?",
                                            (abid, abc[0])).fetchone()[0])
    got_early = flows_engine.enrol(conn, xws["id"], "tag_added", abc[0], {"tags": ["abtest"]}, now=ended + _tdl(days=2))
    got = flows_engine.enrol(conn, xws["id"], "tag_added", abc[0], {"tags": ["abtest"]}, now=ended + _tdl(days=3, seconds=1))
    arch = conn.execute("SELECT COUNT(*) FROM flow_runs_archive WHERE flow_id=? AND contact_id=?", (abid, abc[0])).fetchone()[0]
    conn.commit()
check(n1 == 1 and not got_early and got and arch == 1, "re-entry: once by default; after finishing plus a cool-down when allowed (history kept)")
# move everyone to the new version
with db.connect() as conn:
    conn.execute("UPDATE flow_runs SET status='waiting', node_id=?, wake_at=? WHERE flow_id=? AND contact_id IN (?,?)",
                 (ab["steps"][1]["id"], "2030-01-01T00:00:00+00:00", abid, abc[1], abc[2]))
    conn.execute("UPDATE flow_runs SET node_id='gone' WHERE flow_id=? AND contact_id=?", (abid, abc[2]))
    v1 = conn.execute("SELECT version_id FROM flows WHERE id=?", (abid,)).fetchone()[0]
    conn.commit()
ab2 = json.loads(json.dumps(ab))
ab2["steps"].append({"type": "notify", "message": "v2"})
x.post(f"/app/flows/{abid}/save", data=json.dumps({"tree": ab2}), content_type="application/json", headers={"X-CSRF": csrf(x)})
r = x.post(f"/app/flows/{abid}/state", data={"csrf": csrf(x), "action": "publish", "move": "1"}, follow_redirects=True)
with db.connect() as conn:
    v2 = conn.execute("SELECT version_id FROM flows WHERE id=?", (abid,)).fetchone()[0]
    r1 = conn.execute("SELECT version_id FROM flow_runs WHERE flow_id=? AND contact_id=?", (abid, abc[1])).fetchone()[0]
    r2 = conn.execute("SELECT version_id FROM flow_runs WHERE flow_id=? AND contact_id=?", (abid, abc[2])).fetchone()[0]
check(v2 != v1 and r1 == v2 and r2 == v1 and "Moved 2 people" in r.get_data(as_text=True) and "1 stay" in r.get_data(as_text=True),
      "publish and move: people whose next step still exists move to the new version; others stay")

for k in ("RESEND_API_KEY", "RS_MAIL_DOMAIN", "RESEND_WEBHOOK_SECRET"):
    os.environ.pop(k)
dy = open(os.path.join(os.path.dirname(HERE), ".github", "workflows", "deploy.yml")).read()
check(all(k in dy for k in ("RESEND_API_KEY", "RESEND_WEBHOOK_SECRET", "RS_MAIL_DOMAIN")), "deploy passes the email settings")

print("== phase 0: accessibility, copy, shell helpers")
for t in catalog.TEMPLATES:
    for name in ("", "Sunrise Blend"):
        filled = catalog.fill(t["brief"], {"product": name, "name": ""})
        check("{product}" not in filled and not re.search(r"\bthe the\b", filled, re.I), f"fill() reads cleanly: {t['id']} / {name or 'no name'}")
check("the Sunrise Blend" in catalog.fill(catalog.TEMPLATE_BY_ID["hero"]["brief"], {"product": "Sunrise Blend"}),
      "fill() puts the product name in")
for raw, want in (("Blocked by the content filter. Try a different brief or photo.", "Blocked by the content filter. Try a different brief or photo. Credits refunded."),
                  ("The generation failed. Your credits were refunded.", "The generation failed. Credits refunded."),
                  ("Couldn't start: boom", "Couldn't start: boom. Credits refunded.")):
    check(worker.failure_text(raw) == want, f"failure copy: {want!r}")
with appmod.app.test_request_context():
    check(appmod.credits_usd(925) == "$9.25" and appmod.credits_usd(11100) == "$111" and appmod.credits_usd(0) == "$0",
          "credits_usd shows dollars")
    check(appmod.first_upper("AI angle ideas") == "AI angle ideas" and appmod.first_upper("generation") == "Generation",
          "first_upper keeps acronyms")
    lt = str(appmod.localtime("2026-10-29T05:03:00+00:00"))
    check('<time datetime="2026-10-29T05:03:00+00:00" data-local="datetime">2026-10-29 05:03 UTC</time>' == lt,
          "localtime renders a <time> with a UTC fallback")
css = open(os.path.join(os.path.dirname(HERE), "static", "style.css")).read()
check(not re.search(r"\.choice(\.chk)? input\{display:none\}|\.cf-refs \.choice input\{display:none\}", css),
      "chip radios and checkboxes stay in the tab order (no display:none)")
check(".choice:has(input:focus-visible)" in css, "chips show a keyboard focus ring")


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


dims = re.findall(r"--dim:(#[0-9A-Fa-f]{6})", css)
check(len(dims) >= 3 and _ratio(dims[0], "#1A1C21") >= 4.5 and all(_ratio(d, "#FFFFFF") >= 4.5 and _ratio(d, "#F6F7F4") >= 4.5
                                                                for d in dims[1:]), f"--dim text meets 4.5:1 ({dims})")
for name in os.listdir(os.path.join(os.path.dirname(HERE), "web")):
    if name.endswith(".html") and not name.startswith("c_"):
        src = open(os.path.join(os.path.dirname(HERE), "web", name)).read()
        check("confirm(" not in src, f"{name}: no native confirm()")
html = a.get("/app/billing").get_data(as_text=True)
check("app.js" in html and "<time datetime=" in html and "|capitalize" not in html, "app shell loads app.js; dates are <time>")
r = a.post("/app/brand", data={"csrf": csrf(a), "brand_name": "Aurora", "product_name": "Glow", "claims": "", "audience": ""},
           follow_redirects=True)
check('data-cat="ok"' in r.get_data(as_text=True), "success flashes are marked so they can fade")

import subprocess as _sp, sys as _sys
_out = _sp.run([_sys.executable, "-c", "import catalog; print(catalog.price_credits('photo_to_video', duration=5), "
               "catalog.price_credits('ugc_video', duration=10))"], capture_output=True, text=True,
               env={k: v for k, v in os.environ.items() if k not in ("RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND")},
               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
check(_out.stdout.split() == ["463", "925"], "default video prices follow the vendor's 720p rate (2x margin)")
print(f"\nall {passed} checks passed ({TMP})")

if __name__ == "__main__":
    # Brands & products, the creation flow and the setup assistant run in their
    # own process (their own throwaway database).
    import subprocess
    subprocess.run([sys.executable, os.path.join(HERE, "test_products.py")], check=True)
