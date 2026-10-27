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
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_MARKUP="2.0")

import app as appmod  # noqa: E402
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


def client():
    c = appmod.app.test_client()
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

print("== pricing")
check(catalog.price_credits("ad_image", batch=1) == 22, "image ad: $0.1071 x2 -> 22 credits")
check(catalog.price_credits("photo_to_video", duration=5) == 206, "5s video: $1.03 x2 -> 206 credits")
check(catalog.price_credits("motion_copy", ref_seconds=8.2) == 180, "motion copy bills whole seconds")

print("== accounts")
a = client()
r = a.post("/signup", data={"csrf": csrf(a), "email": "koby@example.com", "password": "longpassword", "name": "Koby"})
check(r.status_code == 302 and "/app/brand" in r.headers["Location"], "first signup becomes admin, lands on brand kit")
r = a.post("/app/brand", data={"brand_name": "TMU", "product_name": "CoreJava", "claims": "30 servings"})
check(r.status_code == 400, "POST without CSRF refused")
a.post("/app/brand", data={"csrf": csrf(a), "brand_name": "TMU", "product_name": "CoreJava",
                           "claims": "30 servings", "audience": "coffee lovers"})
r = a.post("/app/photos", data={"csrf": csrf(a), "photos": (png(), "bag.png")}, content_type="multipart/form-data")
check(r.status_code == 302, "photo upload")
with db.connect() as conn:
    photo = conn.execute("SELECT * FROM photos").fetchone()
    ws1 = conn.execute("SELECT * FROM workspaces").fetchone()
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
check("invite-only" in r.get_data(as_text=True), "bad invite refused")
r = b.post("/signup", data={"csrf": csrf(b), "email": "jane@example.com", "password": "longpassword2", "invite": code})
check(r.status_code == 302, "invited ISR signs up into their own workspace")
check(b.get(f"/media/{out['id']}").status_code == 404, "other workspaces can't see your files")
check(b.get("/admin").status_code == 403, "members can't open admin")
r = b.post("/signup", data={"csrf": csrf(b), "email": "x@example.com", "password": "longpassword3", "invite": code})
check("invite-only" in r.get_data(as_text=True), "invite works once")

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

print("== pages")
for path in ("/app", "/app/library", "/app/brand", "/admin", "/app/new?tool=motion_copy", "/app/new?template=cost"):
    check(a.get(path).status_code == 200, f"{path} renders")
check(a.get("/health").get_json()["generation_configured"], "health reports configured")
html = a.get("/app").get_data(as_text=True)
check(not re.search(r"higgsfield", html, re.I), "vendor never named in the UI")

print(f"\nall {passed} checks passed ({TMP})")
